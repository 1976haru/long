"""Single-item, explicitly confirmed YouTube workflow for Japanese comments.

There is deliberately no search, queue, scheduler, iterator, or background posting API.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .jp_language_quality import inspect_candidate
from .settings import settings_dir
from .youtube_accounts import ChannelProfile, ProfileStore, build_profile_api, verify_channel
from .youtube_usage import record_api_calls

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
PROMPT_VERSION = "jp-v2"


def parse_youtube_video_id(url: str) -> str:
    raw = (url or "").strip()
    try:
        p = urlparse(raw)
    except ValueError:
        p = None
    video_id = ""
    if p and p.scheme == "https" and (p.hostname or "").lower() in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        if p.path == "/watch":
            video_id = (parse_qs(p.query).get("v") or [""])[0]
        elif p.path.startswith("/shorts/"):
            video_id = p.path.split("/")[2] if len(p.path.split("/")) > 2 else ""
    elif p and p.scheme == "https" and (p.hostname or "").lower() == "youtu.be":
        video_id = p.path.strip("/").split("/")[0]
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError("올바른 YouTube 영상 주소를 붙여넣으세요. watch, youtu.be, shorts 주소를 사용할 수 있습니다.")
    return video_id


@dataclass(frozen=True)
class ExternalVideo:
    video_id: str
    title: str
    channel_title: str
    channel_id: str
    description: str = ""


@dataclass
class JapaneseCommentRecord:
    record_id: str
    video_id: str
    video_url: str
    video_title: str
    target_channel: str
    author_profile_id: str
    author_channel_title: str
    comment_hash: str
    comment_id: str
    posted_at: float
    status: str = "POSTED"
    text: str = ""
    error: str = ""


class DuplicateCommentError(ValueError):
    pass


class JapaneseCommentHistory:
    def __init__(self, path: Path | None = None, *, store_text: bool = True):
        self.path = path or settings_dir() / "jp_youtube_comment_history.json"
        self.store_text = store_text

    @staticmethod
    def digest(text: str) -> str:
        return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()

    def all(self) -> list[JapaneseCommentRecord]:
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
            return [JapaneseCommentRecord(**x) for x in rows if isinstance(x, dict)]
        except (OSError, ValueError, TypeError):
            return []

    def save(self, records: list[JapaneseCommentRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(x) for x in records[-500:]], ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def append(self, record: JapaneseCommentRecord) -> None:
        if not self.store_text: record.text = ""
        self.save(self.all() + [record])

    def exact_duplicate(self, video_id: str, profile_id: str, text: str) -> bool:
        key = self.digest(text)
        return any(r.video_id == video_id and r.author_profile_id == profile_id and
                   r.comment_hash == key for r in self.all())

    def previous_on_video(self, video_id: str, profile_id: str) -> bool:
        return any(r.video_id == video_id and r.author_profile_id == profile_id for r in self.all())

    def daily_count(self, profile_id: str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        day = time.localtime(now)[:3]
        return sum(r.author_profile_id == profile_id and r.status == "POSTED" and
                   time.localtime(r.posted_at)[:3] == day for r in self.all())

    def mark_deleted(self, comment_id: str) -> JapaneseCommentRecord:
        rows = self.all()
        rec = next((x for x in rows if x.comment_id == comment_id), None)
        if rec is None: raise ValueError("이 프로그램에서 작성한 댓글 기록이 아닙니다.")
        rec.status = "DELETED"
        self.save(rows)
        return rec


class TranslationCache:
    def __init__(self, path: Path | None = None):
        self.path = path or settings_dir() / "jp_translation_cache.json"

    @staticmethod
    def key(comment_id: str, model: str, text: str, prompt_version: str = PROMPT_VERSION) -> str:
        raw = "\0".join((comment_id, model, prompt_version, hashlib.sha256(text.encode("utf-8")).hexdigest()))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError): return {}

    def get(self, comment_id: str, model: str, text: str) -> dict | None:
        value = self._load().get(self.key(comment_id, model, text))
        return value if isinstance(value, dict) else None

    def put(self, comment_id: str, model: str, text: str, translation: str, nuance: str) -> None:
        values = self._load(); values[self.key(comment_id, model, text)] = {"translation_ko": translation, "nuance_ko": nuance}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp"); tmp.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8"); tmp.replace(self.path)


class ExternalJapaneseCommentService:
    def __init__(self, profiles: ProfileStore, *, api_factory=build_profile_api,
                 history: JapaneseCommentHistory | None = None, clock=time.time):
        self.profiles, self.api_factory = profiles, api_factory
        self.history, self.clock = history or JapaneseCommentHistory(), clock

    def japanese_profiles(self) -> list[ChannelProfile]:
        rows = [p for p in self.profiles.all() if p.channel_id]
        return sorted(rows, key=lambda p: (p.language != "ja", p.alias.lower()))

    def _profile(self, profile_id: str) -> ChannelProfile:
        p = self.profiles.get(profile_id)
        if p is None or not p.channel_id: raise ValueError("댓글을 작성할 YouTube 채널을 선택하세요.")
        return p

    def video(self, profile_id: str, url: str) -> ExternalVideo:
        p, video_id = self._profile(profile_id), parse_youtube_video_id(url)
        api = self.api_factory(p, self.profiles)
        try: sn = api.get_video_snippet(video_id)
        finally: record_api_calls(api.calls, self.clock)
        return ExternalVideo(video_id, str(sn.get("title", "")), str(sn.get("channelTitle", "")),
                             str(sn.get("channelId", "")), str(sn.get("description", ""))[:1000])

    def preview(self, profile_id: str, video: ExternalVideo, text: str) -> dict:
        p = self._profile(profile_id); body = text.strip()
        if not body: raise ValueError("게시할 일본어 댓글을 선택하거나 직접 입력하세요.")
        flags = inspect_candidate(body)
        if flags: raise ValueError("조금 더 자연스럽게 다듬는 것을 권장합니다: " + ", ".join(flags))
        if self.history.exact_duplicate(video.video_id, profile_id, body):
            raise DuplicateCommentError("이 영상에 같은 댓글을 이미 작성했습니다.")
        return {"author": p.channel_title or p.alias, "target_video": video.title,
                "target_channel": video.channel_title, "comment": body,
                "previous_warning": self.history.previous_on_video(video.video_id, profile_id),
                "daily_count": self.history.daily_count(profile_id)}

    def post_confirmed(self, profile_id: str, video: ExternalVideo, text: str, *, confirmed: bool = False) -> JapaneseCommentRecord:
        if not confirmed: raise ValueError("최종 확인 후에만 댓글을 게시할 수 있습니다.")
        self.preview(profile_id, video, text)
        p = self._profile(profile_id); api = self.api_factory(p, self.profiles)
        try:
            verify_channel(api, p.channel_id)
        finally:
            record_api_calls(api.calls, self.clock)
        api.calls.clear()
        try:
            thread = api.insert_top_level_comment(p.channel_id, video.video_id, text.strip())
            comment_id = thread.top.id
            rec = JapaneseCommentRecord(hashlib.sha256(f"{video.video_id}:{comment_id}".encode()).hexdigest()[:16],
                video.video_id, f"https://www.youtube.com/watch?v={video.video_id}", video.title, video.channel_title,
                p.profile_id, p.channel_title or p.alias, self.history.digest(text), comment_id, self.clock(), text=text.strip())
        except Exception as e:
            rec = JapaneseCommentRecord(hashlib.sha256(f"{video.video_id}:{self.clock()}".encode()).hexdigest()[:16],
                video.video_id, f"https://www.youtube.com/watch?v={video.video_id}", video.title, video.channel_title,
                p.profile_id, p.channel_title or p.alias, self.history.digest(text), "", self.clock(), status="FAILED", text=text.strip(), error=type(e).__name__)
            self.history.append(rec)
            raise
        finally:
            record_api_calls(api.calls, self.clock)
        self.history.append(rec)
        return rec

    def delete_confirmed(self, profile_id: str, comment_id: str, *, confirmed: bool = False) -> JapaneseCommentRecord:
        if not confirmed: raise ValueError("삭제 확인이 필요합니다.")
        rec = next((x for x in self.history.all() if x.comment_id == comment_id), None)
        if rec is None or rec.author_profile_id != profile_id or rec.status != "POSTED":
            raise ValueError("이 프로그램에서 해당 채널로 작성한 게시 중인 댓글만 삭제할 수 있습니다.")
        p = self._profile(profile_id); api = self.api_factory(p, self.profiles)
        try:
            verify_channel(api, p.channel_id)
            api.delete_comment(comment_id)
        finally:
            record_api_calls(api.calls, self.clock)
        return self.history.mark_deleted(comment_id)
