"""YouTube Live Streaming API (REST, Python 표준 라이브러리) — Google 대형 SDK 없이 필요한 호출만.

모든 호출: timeout, transient 오류 재시도(지수 backoff), 응답 검증, 토큰 없는 로그.
- 재시도: HTTP 429, 5xx, 403 rateLimitExceeded/userRateLimitExceeded/backendError, 네트워크 오류
- 재시도 안 함(설정 오류): insufficientPermissions, liveStreamingNotEnabled, forbidden, quotaExceeded, invalid_grant 등
- 401은 access token을 한 번 새로 받아 다시 시도한다.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable

from .youtube_oauth import MASK, OAuthError, TransportError, urllib_transport

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"
STREAM_MARKER = "Created by Playlist Long Video Maker (reusable)"
TITLE_MAX = 100
PRIVACY_VALUES = ("public", "unlisted", "private")
RETRY_BACKOFF = (1.0, 2.0, 4.0, 8.0)
TRANSIENT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError", "internalError",
                     "uploadRateLimitExceeded"}

# 공식 quota 비용 (units). 기본 한도 10,000 units/day.
QUOTA_COSTS = {
    "liveBroadcasts.insert": 50, "liveBroadcasts.bind": 50, "liveBroadcasts.transition": 50,
    "liveBroadcasts.list": 1, "liveStreams.insert": 50, "liveStreams.list": 1, "channels.list": 1,
    "videos.list": 1, "videos.update": 50, "thumbnails.set": 50, "liveBroadcasts.update": 50,
    "liveBroadcasts.delete": 50, "videoCategories.list": 1, "videos.insert": 1600,
}
# videos.update(part=snippet): 요청에 없는 기존 snippet 값은 삭제된다(공식 문서) → 읽은 값을 모두 다시 보낸다.
SNIPPET_MUTABLE = ("title", "description", "categoryId", "tags", "defaultLanguage")
DAILY_QUOTA = 10_000

CONFIG_MESSAGES = {
    "insufficientPermissions": "YouTube 권한이 부족합니다. Google 계정 연결을 다시 진행하세요.",
    "insufficientLivePermissions": "이 계정은 YouTube LIVE 권한이 없습니다.",
    "liveStreamingNotEnabled": "이 YouTube 채널은 LIVE 스트리밍이 활성화되어 있지 않습니다 (YouTube Studio에서 활성화 필요).",
    "quotaExceeded": "오늘 YouTube API 사용량 한도에 도달했습니다. 내일 다시 시도하세요.",
    "forbidden": "YouTube가 요청을 거부했습니다 (권한/채널 상태 확인).",
    "notFound": "YouTube에서 해당 방송/스트림을 찾을 수 없습니다.",
    "liveBroadcastNotFound": "YouTube에서 해당 방송을 찾을 수 없습니다.",
    "liveStreamNotFound": "YouTube에서 해당 스트림을 찾을 수 없습니다.",
    "videoNotFound": "YouTube에서 해당 영상(방송)을 찾을 수 없습니다.",
    "invalidImage": "썸네일 이미지가 올바르지 않습니다.",
    "mediaBodyTooLarge": "썸네일 파일이 너무 큽니다 (50MB 이하).",
    "userBroadcastsExceedLimit": "YouTube 예약 방송 개수 한도에 도달했습니다. 지난 예약을 정리하세요.",
}


class YouTubeApiError(RuntimeError):
    """kind: transient | config | auth | not_found | transition"""

    def __init__(self, message: str, *, kind: str, reason: str = "", status: int = 0):
        super().__init__(message)
        self.kind = kind
        self.reason = reason
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.kind == "transient"


@dataclass(frozen=True)
class YouTubeChannelInfo:
    id: str
    title: str


@dataclass(frozen=True)
class YouTubeStreamInfo:
    id: str
    title: str
    stream_status: str  # active | inactive | ready | created | error
    health: str
    ingestion_address: str
    rtmps_ingestion_address: str
    stream_name: str = field(repr=False)  # = Stream Key. 화면/로그에 표시하지 않는다.
    is_reusable: bool = True
    description: str = ""

    @property
    def rtmps_url(self) -> str:
        return self.rtmps_ingestion_address or self.ingestion_address


@dataclass(frozen=True)
class YouTubeBroadcastInfo:
    id: str
    title: str
    life_cycle_status: str  # created | ready | testing | liveStarting | live | complete | revoked ...
    privacy_status: str
    bound_stream_id: str = ""
    scheduled_start: str = ""


def estimate_daily_quota(*, rollovers_per_day: float = 24 / 11.8333, health_poll_seconds: float = 300.0,
                         transition_polls_per_rollover: int = 20) -> int:
    """기본 운영 기준 하루 사용량 추정. 상시 polling은 5분 간격(1 unit)만, 교체 때만 짧게 확인한다."""
    per_rollover = (QUOTA_COSTS["liveBroadcasts.insert"] + QUOTA_COSTS["liveBroadcasts.bind"]
                    + 2 * QUOTA_COSTS["liveBroadcasts.transition"]
                    + transition_polls_per_rollover * QUOTA_COSTS["liveBroadcasts.list"]
                    + 3 * QUOTA_COSTS["liveStreams.list"])
    health = (86400 / health_poll_seconds) * (QUOTA_COSTS["liveBroadcasts.list"] + QUOTA_COSTS["liveStreams.list"])
    return int(round(rollovers_per_day * per_rollover + health))


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


class YouTubeApiClient:
    def __init__(self, token_provider: Callable[..., str], *, transport=urllib_transport, base_url: str = API_BASE,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 30.0, backoff=RETRY_BACKOFF,
                 clock: Callable[[], float] = time.time):
        self._token = token_provider
        self._transport = transport
        self.base_url = base_url.rstrip("/")
        self._sleep = sleep
        self.timeout = timeout
        self.backoff = tuple(backoff)
        self.clock = clock
        self.calls: list[str] = []  # 호출 이름만 기록 (quota 확인/테스트용, 비밀값 없음)

    def __repr__(self) -> str:
        return f"YouTubeApiClient(base={self.base_url})"

    # ---------- core ----------
    def access_token(self, force_refresh: bool = False) -> str:
        """resumable upload(youtube_upload)처럼 _request를 쓰지 않는 호출용. 값은 로그에 남기지 않는다."""
        try:
            return self._token(force_refresh=True) if force_refresh else self._token()
        except OAuthError as e:
            raise YouTubeApiError(str(e), kind="auth", reason=e.kind) from None

    @property
    def upload_base_url(self) -> str:
        return self.base_url.replace("/youtube/v3", "/upload/youtube/v3")

    def _request(self, method: str, path: str, params: dict, body: dict | None, op: str, *,
                 raw: bytes | None = None, content_type: str = "", upload: bool = False) -> dict:
        base = self.upload_base_url if upload else self.base_url
        url = f"{base}/{path}?{urllib.parse.urlencode(params)}"
        data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        refreshed = False
        attempt = 0
        while True:
            try:
                token = self._token(force_refresh=refreshed) if refreshed else self._token()
            except OAuthError as e:
                raise YouTubeApiError(str(e), kind="auth", reason=e.kind) from None
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = content_type or "application/json; charset=utf-8"
            self.calls.append(op)
            try:
                status, raw = self._transport(method, url, headers, data, self.timeout)
            except TransportError:
                err = YouTubeApiError("YouTube API에 연결할 수 없습니다 (네트워크).", kind="transient", reason="network")
            else:
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    payload = {}
                if 200 <= status < 300:
                    if not isinstance(payload, dict):
                        raise YouTubeApiError("YouTube API 응답 형식이 올바르지 않습니다.", kind="transient", status=status)
                    log.info("youtube %s %s -> %s", method, op, status)  # 토큰/쿼리 없이 기록
                    return payload
                err = self._error(status, payload)
                if err.kind == "auth" and not refreshed:
                    refreshed = True  # 401: 토큰 1회 갱신 후 재시도 (attempt 소모 안 함)
                    continue
            log.warning("youtube %s %s failed: %s %s", method, op, err.status, err.reason)
            if not err.retryable or attempt >= len(self.backoff):
                raise err
            self._sleep(self.backoff[attempt])
            attempt += 1

    @staticmethod
    def _error(status: int, payload: dict) -> YouTubeApiError:
        if status == 413:
            return YouTubeApiError(CONFIG_MESSAGES["mediaBodyTooLarge"], kind="config", reason="mediaBodyTooLarge", status=status)
        e = payload.get("error") if isinstance(payload, dict) else None
        reasons = [x.get("reason", "") for x in (e or {}).get("errors", []) if isinstance(x, dict)] if isinstance(e, dict) else []
        reason = reasons[0] if reasons else (e.get("status", "") if isinstance(e, dict) else "")
        if status == 401:
            return YouTubeApiError("YouTube 인증이 만료되었습니다.", kind="auth", reason=reason or "unauthorized", status=status)
        if status == 429 or status >= 500 or reason in TRANSIENT_REASONS:
            return YouTubeApiError("YouTube API가 일시적으로 응답하지 않습니다. 자동으로 다시 시도합니다.",
                                   kind="transient", reason=reason or str(status), status=status)
        if reason in ("errorStreamInactive", "invalidTransition", "redundantTransition"):
            return YouTubeApiError(f"YouTube 방송 상태 전환 불가 ({reason}).", kind="transition", reason=reason, status=status)
        if status == 404 or reason.endswith("NotFound"):
            return YouTubeApiError(CONFIG_MESSAGES.get(reason, CONFIG_MESSAGES["notFound"]), kind="not_found",
                                   reason=reason, status=status)
        return YouTubeApiError(CONFIG_MESSAGES.get(reason, f"YouTube API 오류 ({reason or status})."),
                               kind="config", reason=reason, status=status)

    # ---------- channel ----------
    def get_channel(self) -> YouTubeChannelInfo:
        d = self._request("GET", "channels", {"part": "snippet", "mine": "true"}, None, "channels.list")
        items = d.get("items") or []
        if not items:
            raise YouTubeApiError("이 Google 계정에 YouTube 채널이 없습니다.", kind="config", reason="noChannel")
        return YouTubeChannelInfo(str(items[0].get("id", "")), str(items[0].get("snippet", {}).get("title", "")))

    # ---------- streams ----------
    @staticmethod
    def _stream(item: dict) -> YouTubeStreamInfo:
        cdn = item.get("cdn", {}) or {}
        ing = cdn.get("ingestionInfo", {}) or {}
        st = item.get("status", {}) or {}
        return YouTubeStreamInfo(
            id=str(item.get("id", "")), title=str(item.get("snippet", {}).get("title", "")),
            stream_status=str(st.get("streamStatus", "")), health=str((st.get("healthStatus") or {}).get("status", "")),
            ingestion_address=str(ing.get("ingestionAddress", "")),
            rtmps_ingestion_address=str(ing.get("rtmpsIngestionAddress", "")),
            stream_name=str(ing.get("streamName", "")),
            is_reusable=bool((item.get("contentDetails") or {}).get("isReusable", False)),
            description=str(item.get("snippet", {}).get("description", "")))

    def get_stream(self, stream_id: str) -> YouTubeStreamInfo:
        d = self._request("GET", "liveStreams", {"part": "id,snippet,cdn,status,contentDetails", "id": stream_id},
                          None, "liveStreams.list")
        items = d.get("items") or []
        if not items:
            raise YouTubeApiError(CONFIG_MESSAGES["liveStreamNotFound"], kind="not_found", reason="liveStreamNotFound")
        return self._stream(items[0])

    def list_my_streams(self) -> list[YouTubeStreamInfo]:
        d = self._request("GET", "liveStreams", {"part": "id,snippet,cdn,status,contentDetails", "mine": "true",
                                                 "maxResults": "50"}, None, "liveStreams.list")
        return [self._stream(i) for i in d.get("items") or []]

    def insert_stream(self, title: str = "PLVM 24H LIVE (1080p30)") -> YouTubeStreamInfo:
        body = {"snippet": {"title": title[:TITLE_MAX], "description": STREAM_MARKER},
                "cdn": {"ingestionType": "rtmp", "resolution": "1080p", "frameRate": "30fps"},
                "contentDetails": {"isReusable": True}}
        return self._stream(self._request("POST", "liveStreams", {"part": "snippet,cdn,contentDetails,status"}, body,
                                          "liveStreams.insert"))

    def ensure_reusable_stream(self, saved_id: str | None = None) -> YouTubeStreamInfo:
        """저장된 stream → 이 프로그램이 만든 reusable stream → 없으면 새로 만든다 (매 방송마다 만들지 않음)."""
        if saved_id:
            try:
                s = self.get_stream(saved_id)
                if s.is_reusable:
                    return s
            except YouTubeApiError as e:
                if e.kind != "not_found":
                    raise
        for s in self.list_my_streams():
            if s.is_reusable and s.description == STREAM_MARKER:
                return s
        return self.insert_stream()

    # ---------- broadcasts ----------
    @staticmethod
    def _broadcast(item: dict) -> YouTubeBroadcastInfo:
        sn = item.get("snippet", {}) or {}
        st = item.get("status", {}) or {}
        return YouTubeBroadcastInfo(
            id=str(item.get("id", "")), title=str(sn.get("title", "")),
            life_cycle_status=str(st.get("lifeCycleStatus", "")), privacy_status=str(st.get("privacyStatus", "")),
            bound_stream_id=str((item.get("contentDetails") or {}).get("boundStreamId", "")),
            scheduled_start=str(sn.get("scheduledStartTime", "")))

    def insert_broadcast(self, *, title: str, description: str = "", privacy: str = "unlisted",
                         made_for_kids: bool = False, scheduled_start: float | None = None,
                         scheduled_end: float | None = None) -> YouTubeBroadcastInfo:
        """liveBroadcast에는 categoryId/tags가 없다 → 생성 후 videos.update로 적용 (update_video_metadata)."""
        validate_title(title)
        if privacy not in PRIVACY_VALUES:
            raise YouTubeApiError("공개 상태가 올바르지 않습니다.", kind="config", reason="invalidPrivacy")
        snippet = {"title": title, "description": description[:5000],
                   "scheduledStartTime": _iso(scheduled_start if scheduled_start is not None else self.clock() + 60)}
        if scheduled_end is not None:
            snippet["scheduledEndTime"] = _iso(scheduled_end)
        body = {
            "snippet": snippet,
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": bool(made_for_kids)},
            "contentDetails": {
                # 프로그램이 전환을 직접 제어: 자동 시작/종료 끔 (같은 active stream에 미리 bind해도 조기 시작 없음)
                "enableAutoStart": False, "enableAutoStop": False,
                "recordFromStart": True, "enableDvr": True,
                # monitor stream 끔 → testing 단계 없이 ready → live 전환
                "monitorStream": {"enableMonitorStream": False},
            },
        }
        return self._broadcast(self._request("POST", "liveBroadcasts", {"part": "snippet,status,contentDetails"}, body,
                                             "liveBroadcasts.insert"))

    def bind_broadcast(self, broadcast_id: str, stream_id: str) -> YouTubeBroadcastInfo:
        return self._broadcast(self._request("POST", "liveBroadcasts/bind",
                                             {"id": broadcast_id, "streamId": stream_id, "part": "id,snippet,status,contentDetails"},
                                             None, "liveBroadcasts.bind"))

    def get_broadcast(self, broadcast_id: str) -> YouTubeBroadcastInfo:
        d = self._request("GET", "liveBroadcasts", {"part": "id,snippet,status,contentDetails", "id": broadcast_id},
                          None, "liveBroadcasts.list")
        items = d.get("items") or []
        if not items:
            raise YouTubeApiError(CONFIG_MESSAGES["liveBroadcastNotFound"], kind="not_found", reason="liveBroadcastNotFound")
        return self._broadcast(items[0])

    def transition_broadcast(self, broadcast_id: str, status: str) -> YouTubeBroadcastInfo:
        if status not in ("testing", "live", "complete"):
            raise ValueError(status)
        return self._broadcast(self._request("POST", "liveBroadcasts/transition",
                                             {"id": broadcast_id, "broadcastStatus": status, "part": "id,snippet,status,contentDetails"},
                                             None, "liveBroadcasts.transition"))

    def complete_broadcast(self, broadcast_id: str) -> YouTubeBroadcastInfo:
        return self.transition_broadcast(broadcast_id, "complete")

    def list_upcoming_broadcasts(self) -> list[YouTubeBroadcastInfo]:
        d = self._request("GET", "liveBroadcasts", {"part": "id,snippet,status,contentDetails", "broadcastStatus": "upcoming",
                                                    "broadcastType": "all", "maxResults": "50"}, None, "liveBroadcasts.list")
        return [self._broadcast(i) for i in d.get("items") or []]

    def update_broadcast(self, broadcast_id: str, *, title: str, description: str, scheduled_start: float,
                         scheduled_end: float | None, privacy: str, made_for_kids: bool) -> YouTubeBroadcastInfo:
        """예약 수정: snippet/status 전체를 다시 보낸다 (빠진 값이 지워지지 않게)."""
        validate_title(title)
        snippet = {"title": title, "description": description[:5000], "scheduledStartTime": _iso(scheduled_start)}
        if scheduled_end is not None:
            snippet["scheduledEndTime"] = _iso(scheduled_end)
        body = {"id": broadcast_id, "snippet": snippet,
                "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": bool(made_for_kids)}}
        return self._broadcast(self._request("PUT", "liveBroadcasts", {"part": "snippet,status"}, body,
                                             "liveBroadcasts.update"))

    def delete_broadcast(self, broadcast_id: str) -> None:
        self._request("DELETE", "liveBroadcasts", {"id": broadcast_id}, None, "liveBroadcasts.delete")

    # ---------- videos (태그/카테고리/언어) ----------
    def get_video_snippet(self, video_id: str) -> dict:
        d = self._request("GET", "videos", {"part": "snippet", "id": video_id}, None, "videos.list")
        items = d.get("items") or []
        if not items:
            raise YouTubeApiError(CONFIG_MESSAGES["videoNotFound"], kind="not_found", reason="videoNotFound")
        sn = items[0].get("snippet")
        if not isinstance(sn, dict):
            raise YouTubeApiError("YouTube 영상 정보 형식이 올바르지 않습니다.", kind="transient", reason="badSnippet")
        return sn

    def update_video_metadata(self, video_id: str, *, tags: list[str] | None = None, category_id: str | None = None,
                              default_language: str | None = None, title: str | None = None,
                              description: str | None = None) -> dict:
        """videos.list로 기존 snippet을 읽고, 바꿀 값만 바꿔 snippet 전체(title/description/categoryId/tags/defaultLanguage)를
        다시 보낸다. tags만 보내면 다른 값이 지워지기 때문 (공식 문서)."""
        cur = self.get_video_snippet(video_id)
        new = {k: cur[k] for k in SNIPPET_MUTABLE if k in cur and cur[k] not in (None, "")}
        if title is not None:
            new["title"] = validate_title(title)
        if description is not None:
            new["description"] = description[:5000]
        if category_id:
            new["categoryId"] = str(category_id)
        if tags is not None:
            new["tags"] = list(tags)
        if default_language:
            new["defaultLanguage"] = default_language
        if not new.get("title") or not new.get("categoryId"):
            new.setdefault("categoryId", "10")
            if not new.get("title"):
                raise YouTubeApiError("영상 제목을 읽을 수 없어 메타데이터를 적용하지 않았습니다.", kind="config", reason="noTitle")
        body = {"id": video_id, "snippet": new}
        d = self._request("PUT", "videos", {"part": "snippet"}, body, "videos.update")
        return d.get("snippet", new) if isinstance(d, dict) else new

    def get_video_status(self, video_id: str) -> dict:
        d = self._request("GET", "videos", {"part": "status", "id": video_id}, None, "videos.list")
        items = d.get("items") or []
        if not items:
            raise YouTubeApiError(CONFIG_MESSAGES["videoNotFound"], kind="not_found", reason="videoNotFound")
        return items[0].get("status") or {}

    def update_video_status(self, video_id: str, *, privacy: str, made_for_kids: bool,
                            publish_at: str | None = None) -> dict:
        """videos.update(part=status). 예약 공개는 privacyStatus=private + publishAt(UTC ISO)."""
        if privacy not in PRIVACY_VALUES:
            raise YouTubeApiError("공개 상태가 올바르지 않습니다.", kind="config", reason="invalidPrivacy")
        status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": bool(made_for_kids)}
        if publish_at:
            status["publishAt"] = publish_at
        d = self._request("PUT", "videos", {"part": "status"}, {"id": video_id, "status": status}, "videos.update")
        return d.get("status", status) if isinstance(d, dict) else status

    def list_video_categories(self, region: str = "KR", hl: str = "ko") -> list[tuple[str, str]]:
        d = self._request("GET", "videoCategories", {"part": "snippet", "regionCode": region, "hl": hl}, None,
                          "videoCategories.list")
        return [(str(i.get("id")), str(i.get("snippet", {}).get("title", ""))) for i in d.get("items") or []
                if i.get("snippet", {}).get("assignable")]

    def set_thumbnail(self, video_id: str, image: bytes, mime: str) -> None:
        """thumbnails.set (media upload, 최대 50MB). 이미지 bytes는 로그에 남기지 않는다."""
        if mime not in ("image/jpeg", "image/png"):
            raise YouTubeApiError("썸네일은 JPG 또는 PNG만 올릴 수 있습니다.", kind="config", reason="invalidImage")
        self._request("POST", "thumbnails/set", {"videoId": video_id, "uploadType": "media"}, None, "thumbnails.set",
                      raw=image, content_type=mime, upload=True)


def validate_title(title: str) -> str:
    t = (title or "").strip()
    if not t:
        raise YouTubeApiError("LIVE 제목을 입력하세요.", kind="config", reason="emptyTitle")
    if len(t) > TITLE_MAX:
        raise YouTubeApiError(f"LIVE 제목은 {TITLE_MAX}자 이하여야 합니다 (현재 {len(t)}자).", kind="config", reason="titleTooLong")
    if "<" in t or ">" in t:
        raise YouTubeApiError("LIVE 제목에 < > 문자는 쓸 수 없습니다.", kind="config", reason="invalidTitle")
    return t


def redact_headers(headers: dict) -> dict:
    return {k: (MASK if k.lower() == "authorization" else v) for k, v in headers.items()}
