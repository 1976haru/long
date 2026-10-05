"""YouTube 영상 resumable upload (videos.insert, uploadType=resumable) — Python 표준 라이브러리만.

- 영상은 CHUNK_SIZE(8MB, 256KB 배수)씩 파일에서 읽어 보낸다. 영상 전체를 메모리에 올리지 않고 프레임도 디코딩하지 않는다.
- 업로드 세션 URL을 콜백(on_session)으로 넘겨 저장하면, 프로그램이 꺼졌다 켜져도 '보낸 곳부터' 이어 올린다.
- 일시 오류(5xx/429/네트워크)는 backoff 후 서버에 받은 위치를 물어(Content-Range: bytes */N) 그 위치부터 다시 보낸다.
- 세션이 만료되면(404/410) SessionExpired → 호출자가 새 세션으로 처음부터 다시 올린다.
- 예약 공개: status.privacyStatus=private + status.publishAt(UTC). 업로드 후 verify_publish_at으로 확인한다.
- access token, 세션 URL은 로그/repr/오류 메시지에 남기지 않는다.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .youtube_api import RETRY_BACKOFF, YouTubeApiClient, YouTubeApiError
from .youtube_metadata import BroadcastMetadata

CHUNK_SIZE = 8 * 1024 * 1024
assert CHUNK_SIZE % (256 * 1024) == 0
VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".mkv")
PUBLISH_MIN_LEAD_SECONDS = 5 * 60  # 예약 시각은 지금보다 최소 5분 뒤


class UploadCancelled(RuntimeError):
    pass


class SessionExpired(RuntimeError):
    """업로드 세션 URL이 만료/무효 → 새 세션으로 처음부터."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # 308 Resume Incomplete를 redirect로 처리하지 않는다
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> tuple[int, dict, bytes]:
    """(status, 소문자 header dict, body). 4xx/5xx/308도 예외 없이 돌려준다. 네트워크 오류만 OSError 계열로 올린다."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ConnectionError(type(e).__name__) from None


def utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("시간대 없는 날짜는 사용할 수 없습니다.")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc)


def validate_video_file(path) -> Path:
    p = Path(str(path or "").strip().strip('"'))
    if not str(path or "").strip() or not p.is_file():
        raise YouTubeApiError("업로드할 영상 파일을 찾을 수 없습니다.", kind="config", reason="noVideoFile")
    if p.suffix.lower() not in VIDEO_EXTENSIONS:
        raise YouTubeApiError("MP4 영상 파일을 선택하세요.", kind="config", reason="badVideoFile")
    if p.name.lower().endswith(".part.mp4"):
        raise YouTubeApiError("아직 제작 중인 임시 파일(.part.mp4)은 올릴 수 없습니다.", kind="config", reason="partFile")
    if p.stat().st_size == 0:
        raise YouTubeApiError("영상 파일이 비어 있습니다.", kind="config", reason="emptyVideo")
    return p


def validate_publish_at(publish_at: datetime | None, now: datetime) -> None:
    if publish_at is None:
        return
    if publish_at.tzinfo is None:
        raise YouTubeApiError("시간대 없는 예약 시간은 사용할 수 없습니다.", kind="config", reason="naivePublishAt")
    if (publish_at - now).total_seconds() < PUBLISH_MIN_LEAD_SECONDS:
        raise YouTubeApiError("예약 공개 시간이 이미 지났거나 너무 가깝습니다 (지금보다 5분 이상 뒤로 설정).",
                              kind="config", reason="publishAtPast")


def build_video_body(md: BroadcastMetadata, publish_at: datetime | None) -> dict:
    """videos.insert body. 예약이면 공개 상태와 무관하게 private + publishAt (YouTube 규칙)."""
    md.validate("영상 제목")
    snippet = {"title": md.title, "description": md.description, "categoryId": str(md.category_id)}
    if md.tags:
        snippet["tags"] = list(md.tags)
    if md.default_language:
        snippet["defaultLanguage"] = md.default_language
        snippet["defaultAudioLanguage"] = md.default_language
    status = {"privacyStatus": "private" if publish_at else md.privacy_status,
              "selfDeclaredMadeForKids": bool(md.made_for_kids)}
    if publish_at:
        status["publishAt"] = utc_iso(publish_at)
    return {"snippet": snippet, "status": status}


@dataclass
class UploadProgress:
    sent: int
    total: int

    @property
    def fraction(self) -> float:
        return self.sent / self.total if self.total else 0.0


class ResumableUploader:
    def __init__(self, api: YouTubeApiClient, *, transport=http_transport, chunk_size: int = CHUNK_SIZE,
                 sleep: Callable[[float], None] = time.sleep, backoff=RETRY_BACKOFF, timeout: float = 120.0,
                 cancel: threading.Event | None = None, max_failures: int = 6):
        if chunk_size <= 0 or chunk_size % (256 * 1024):
            raise ValueError("chunk_size must be a multiple of 256 KiB")
        self.api = api
        self.transport = transport
        self.chunk_size = chunk_size
        self.sleep = sleep
        self.backoff = tuple(backoff)
        self.timeout = timeout
        self.cancel = cancel or threading.Event()
        self.max_failures = max_failures

    def __repr__(self) -> str:
        return f"ResumableUploader(chunk={self.chunk_size})"

    # ---------- HTTP (401이면 token 1회 갱신) ----------
    def _send(self, method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict, bytes]:
        for attempt in (0, 1):
            h = dict(headers, Authorization=f"Bearer {self.api.access_token(force_refresh=attempt == 1)}")
            try:
                status, rh, raw = self.transport(method, url, h, body, self.timeout)
            except ConnectionError:
                raise YouTubeApiError("네트워크 오류로 업로드가 잠시 끊겼습니다.", kind="transient", reason="network") from None
            if status != 401:
                return status, rh, raw
        return status, rh, raw

    @staticmethod
    def _json(raw: bytes) -> dict:
        try:
            d = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return {}
        return d if isinstance(d, dict) else {}

    def _raise(self, status: int, raw: bytes) -> None:
        raise YouTubeApiClient._error(status, self._json(raw))

    def start_session(self, total: int, body: dict, mime: str = "video/mp4") -> str:
        url = (f"{self.api.upload_base_url}/videos?"
               + urllib.parse.urlencode({"uploadType": "resumable", "part": "snippet,status"}))
        headers = {"Content-Type": "application/json; charset=UTF-8", "X-Upload-Content-Length": str(total),
                   "X-Upload-Content-Type": mime}
        self.api.calls.append("videos.insert")
        status, rh, raw = self._send("POST", url, headers, json.dumps(body).encode("utf-8"))
        if status != 200 or not rh.get("location"):
            self._raise(status, raw)
        return self._check_location(rh["location"])

    def _check_location(self, url: str) -> str:
        """세션 URL이 API와 같은 서버인지 확인한 뒤에만 token을 보낸다."""
        origin = self.api.upload_base_url.split("/upload/")[0] + "/upload/"
        if not str(url).startswith(origin):
            raise YouTubeApiError("업로드 세션 주소가 올바르지 않습니다.", kind="config", reason="badLocation")
        return url

    @staticmethod
    def _range_end(rh: dict) -> int:
        """Range: bytes=0-N → 다음에 보낼 위치 N+1. 헤더가 없으면 0 (서버가 아직 받은 것이 없음)."""
        r = rh.get("range", "")
        if not r.startswith("bytes="):
            return 0
        try:
            return int(r.split("-", 1)[1]) + 1
        except (ValueError, IndexError):
            return 0

    def query_offset(self, session_url: str, total: int) -> tuple[int, dict | None]:
        """(이어서 보낼 위치, 이미 끝났으면 video resource)."""
        self._check_location(session_url)
        status, rh, raw = self._send("PUT", session_url, {"Content-Range": f"bytes */{total}", "Content-Length": "0"}, b"")
        if status in (200, 201):
            return total, self._json(raw)
        if status == 308:
            return self._range_end(rh), None
        if status in (404, 410):
            raise SessionExpired()
        self._raise(status, raw)
        return 0, None  # pragma: no cover

    def upload(self, path, body: dict, *, session_url: str = "", on_session: Callable[[str], None] | None = None,
               on_progress: Callable[[UploadProgress], None] | None = None) -> dict:
        """영상 업로드 → video resource(dict, id 포함). session_url이 있으면 그 세션에서 이어 올린다."""
        p = validate_video_file(path)
        total = p.stat().st_size
        offset, done = 0, None
        if session_url:
            try:
                offset, done = self.query_offset(session_url, total)
            except SessionExpired:
                session_url = ""
        if not session_url:
            session_url = self.start_session(total, body)
            offset = 0
            if on_session:
                on_session(session_url)
        failures, need_query = 0, False
        with open(p, "rb") as f:
            while done is None:
                if self.cancel.is_set():
                    raise UploadCancelled()
                try:
                    if need_query:  # 끊긴 뒤: 서버가 실제로 받은 위치부터
                        offset, done = self.query_offset(session_url, total)
                        need_query = False
                        if done is not None:
                            break
                    if on_progress:
                        on_progress(UploadProgress(offset, total))
                    f.seek(offset)
                    chunk = f.read(self.chunk_size)
                    end = offset + len(chunk) - 1
                    status, rh, raw = self._send("PUT", session_url, {
                        "Content-Type": "video/mp4", "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{total}"}, chunk)
                    if status in (200, 201):
                        done = self._json(raw)
                    elif status == 308:
                        offset = self._range_end(rh)
                        failures = 0
                    elif status in (404, 410):
                        raise SessionExpired()
                    else:
                        self._raise(status, raw)
                except YouTubeApiError as e:
                    if not e.retryable:
                        raise
                    failures += 1
                    if failures > self.max_failures:
                        raise
                    self.sleep(self.backoff[min(failures - 1, len(self.backoff) - 1)])
                    need_query = True
        if not done.get("id"):
            raise YouTubeApiError("업로드 응답에 영상 ID가 없습니다.", kind="transient", reason="noVideoId")
        if on_progress:
            on_progress(UploadProgress(total, total))
        return done


API_RESTRICTED_MESSAGE = ("영상 업로드는 완료됐지만 Google API 프로젝트 제한 때문에 예약 공개가 적용되지 않았습니다.\n"
                          "(검수되지 않은 API 프로젝트로 올린 영상은 비공개로만 고정됩니다. Google API 감사(audit) 승인 후 "
                          "YouTube Studio에서 공개 설정을 바꾸세요. 같은 영상을 다시 올리지 않습니다.)")


class ApiRestrictedError(YouTubeApiError):
    """업로드는 됐지만 API 프로젝트 제한으로 private에 고정됨 → 다시 올리지 말고 사용자 확인 필요."""

    def __init__(self):
        super().__init__(API_RESTRICTED_MESSAGE, kind="config", reason="apiProjectPrivateOnly")


def verify_publish_at(api: YouTubeApiClient, video_id: str, *, publish_at: datetime | None, privacy: str,
                      made_for_kids: bool) -> dict:
    """업로드 후 videos.list로 실제 privacyStatus/publishAt 확인. 다르면 한 번 바로잡고 다시 확인한다.

    - 예약: privacyStatus=private + publishAt(요청과 1초 이내)여야 통과.
    - 요청은 예약/공개인데 실제로는 publishAt 없는 private로만 남으면 API 프로젝트 제한 → ApiRestrictedError.
    - 그 밖에 다르면 YouTubeApiError(publishAtMismatch). 어느 경우도 '완료'로 표시하지 않는다.
    """
    want_private_only = publish_at is None and privacy == "private"

    def matches(st: dict) -> bool:
        if publish_at is None:
            return st.get("privacyStatus") == privacy
        if st.get("privacyStatus") != "private" or not st.get("publishAt"):
            return False
        try:
            return abs((parse_utc(st["publishAt"]) - publish_at).total_seconds()) < 1
        except ValueError:
            return False

    st = api.get_video_status(video_id)
    if matches(st):
        return st
    api.update_video_status(video_id, privacy="private" if publish_at else privacy, made_for_kids=made_for_kids,
                            publish_at=utc_iso(publish_at) if publish_at else None)
    st = api.get_video_status(video_id)
    if matches(st):
        return st
    if not want_private_only and st.get("privacyStatus") == "private" and not st.get("publishAt"):
        raise ApiRestrictedError()
    raise YouTubeApiError("YouTube에 저장된 예약 공개 시간/공개 상태가 요청과 다릅니다. YouTube Studio에서 확인하세요.",
                          kind="config", reason="publishAtMismatch")


def file_signature(path) -> str:
    """업로드 재개 전 파일이 바뀌지 않았는지 확인용 (크기 + 수정 시각)."""
    st = os.stat(path)
    return f"{st.st_size}:{int(st.st_mtime)}"
