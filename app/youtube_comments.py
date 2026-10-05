"""YouTube 댓글 자동화 (Tk 없음) — 첫 댓글, 채널 전체 새 댓글 확인, 검토/안전형 자동답글.

중요한 YouTube 동작:
- 예약 업로드 영상은 공개 전까지 private → private 영상에는 댓글을 달 수 없다.
  업로드 완료 → CommentTask(WAITING_PUBLIC) → publishAt 2분 전부터 videos.list 확인 → 공개 확인 후 약 60초 뒤 첫 댓글.
- 즉시 public/unlisted: 처리 확인 후 첫 댓글. private + publishAt 없음: 자동으로 시도하지 않음(WAITING_PRIVACY_CHANGE).
- 프로그램이 꺼져 있으면 댓글을 달 수 없다. 다시 켜면 미완료 작업을 확인해 이미 공개된 영상에 첫 댓글을 단다(catch-up).
- 새 댓글은 영상마다 polling하지 않고 채널 전체(commentThreads.list allThreadsRelatedToChannelId) 1번 조회.
- 자동답글은 외부 AI 없이 규칙+템플릿. 애매하면 '검토 필요'. 채널당 24시간 최대 10/20/30개, 답글 사이 60초 이상.
- 같은 댓글에 두 번 답하지 않는다 (reply_id 저장 + 채널 주인 직접 답글 확인). 첫 댓글은 first_comment_id가 있으면 다시 쓰지 않는다.
- 저장: settings.json (원자적 저장 + 잠금). 댓글 내용/작성자 이름 외 비밀값(token 등)은 저장하지 않는다.
"""
from __future__ import annotations

import hashlib
import queue
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .settings import SETTINGS_LOCK, load_settings, save_settings
from .youtube_accounts import ChannelMismatchError, ChannelProfile, ProfileStore, build_profile_api, verify_channel
from .youtube_api import YouTubeApiClient, YouTubeApiError
from .youtube_metadata import MetadataError, render_template, validate_comment_text
from .youtube_oauth import OAuthError
from .youtube_usage import record_api_calls

# ---------------- 첫 댓글 작업 상태 ----------------
WAITING_PUBLIC = "WAITING_PUBLIC"
WAITING_PRIVACY_CHANGE = "WAITING_PRIVACY_CHANGE"
READY = "READY"
POSTING = "POSTING"
POSTED = "POSTED"
COMMENTS_DISABLED = "COMMENTS_DISABLED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
TASK_TERMINAL = (POSTED, COMMENTS_DISABLED, FAILED, CANCELLED)
TASK_LABELS = {WAITING_PUBLIC: "공개 후 자동등록 대기", WAITING_PRIVACY_CHANGE: "공개 상태 변경 대기 (비공개 영상)",
               READY: "등록 준비 중", POSTING: "등록 중", POSTED: "첫 댓글 등록 완료",
               COMMENTS_DISABLED: "댓글 사용 안 함", FAILED: "실패", CANCELLED: "취소됨"}

# ---------------- 오류 종류 (401/403을 한데 뭉치지 않는다) ----------------
E_COMMENTS_DISABLED = "COMMENTS_DISABLED"
E_INSUFFICIENT_PERMISSION = "INSUFFICIENT_PERMISSION"
E_QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
E_RATE_LIMITED = "RATE_LIMITED"
E_VIDEO_PRIVATE = "VIDEO_PRIVATE"
E_VIDEO_NOT_FOUND = "VIDEO_NOT_FOUND"
E_NETWORK = "NETWORK_ERROR"
E_AUTH = "AUTH_EXPIRED"
E_FORBIDDEN = "FORBIDDEN"
E_CHANNEL = "CHANNEL_MISMATCH"
E_OTHER = "OTHER"
REAUTH_MESSAGE = "댓글 기능 사용을 위해 이 채널의 Google 연결을 다시 승인하세요. ([YouTube 채널 관리] → Google 계정 연결)"
ERROR_LABELS = {E_COMMENTS_DISABLED: "댓글 사용 안 함", E_INSUFFICIENT_PERMISSION: "댓글 권한 부족 (다시 승인 필요)",
                E_QUOTA_EXCEEDED: "오늘 API 사용량 한도", E_RATE_LIMITED: "요청이 많아 잠시 대기",
                E_VIDEO_PRIVATE: "영상이 비공개", E_VIDEO_NOT_FOUND: "영상을 찾을 수 없음", E_NETWORK: "네트워크 오류",
                E_AUTH: "Google 연결 만료", E_FORBIDDEN: "YouTube가 거부함", E_CHANNEL: "채널 불일치", E_OTHER: "오류"}
PERMISSION_REASONS = {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientScopes",
                      "authorizationRequired"}
MAX_ATTEMPTS = 8
PUBLISH_LEAD = 120  # publishAt 2분 전부터 확인
POST_DELAY = 60  # 공개 확인 후 첫 댓글까지 (30~90초 권장 범위)
LATE_PUBLISH_GIVE_UP = 6 * 3600  # publishAt 6시간 뒤에도 비공개면 자동 확인을 멈춘다 (무한 polling 금지)


def classify_error(e: Exception) -> str:
    if isinstance(e, ChannelMismatchError):
        return E_CHANNEL
    if isinstance(e, OAuthError):
        return E_AUTH
    if not isinstance(e, YouTubeApiError):
        return E_OTHER
    r = e.reason or ""
    if r == "commentsDisabled":
        return E_COMMENTS_DISABLED
    if r in PERMISSION_REASONS:
        return E_INSUFFICIENT_PERMISSION
    if r in ("quotaExceeded", "dailyLimitExceeded"):
        return E_QUOTA_EXCEEDED
    if e.status == 429 or r in ("rateLimitExceeded", "userRateLimitExceeded"):
        return E_RATE_LIMITED
    if r == "network":
        return E_NETWORK
    if e.kind == "not_found" or r == "videoNotFound":
        return E_VIDEO_NOT_FOUND
    if e.kind == "auth" or e.status == 401:
        return E_AUTH
    if r == "forbidden" or e.status == 403:
        return E_FORBIDDEN
    if e.retryable:
        return E_NETWORK
    return E_OTHER


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(text: str) -> float:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


@dataclass
class CommentTask:
    task_id: str  # = UploadJob.job_id (작업당 1개 → 중복 생성 없음)
    profile_id: str
    channel_id: str
    video_id: str
    title: str
    text: str
    publish_at_utc: str = ""
    privacy: str = "private"
    status: str = WAITING_PUBLIC
    next_at: float = 0.0  # 다음 확인/등록 시각 (epoch)
    first_comment_id: str = ""
    attempts: int = 0
    error_kind: str = ""
    error: str = ""
    posted_at: float = 0.0

    @property
    def label(self) -> str:
        if self.status in (READY, WAITING_PUBLIC) and self.error_kind:
            return f"{TASK_LABELS[self.status]} · {ERROR_LABELS.get(self.error_kind, '')} → 다시 시도 예정"
        if self.status == FAILED and self.error_kind:
            return f"실패 · {ERROR_LABELS.get(self.error_kind, '')}"
        return TASK_LABELS.get(self.status, self.status)

    @classmethod
    def from_dict(cls, d: dict) -> "CommentTask":
        known = set(cls.__dataclass_fields__)
        t = cls(**{k: v for k, v in (d or {}).items() if k in known})
        if t.status == POSTING and not t.first_comment_id:
            t.status, t.next_at = POSTING, 0.0  # 재실행: 이미 달렸는지 먼저 확인 (recover_posting)
        return t


# ---------------- 시청자 댓글 ----------------
C_NEW = "NEW"
C_REVIEW = "REVIEW_REQUIRED"
C_REPLIED = "REPLIED"
C_MANUAL = "ALREADY_REPLIED_MANUALLY"
C_HELD = "HELD"
C_COMMENTS_OFF = "COMMENTS_OFF"
C_EXCLUDED = "EXCLUDED"
C_DONE = "DONE"
C_SELF = "SELF"
COMMENT_LABELS = {C_NEW: "새 댓글", C_REVIEW: "검토 필요", C_REPLIED: "답글 완료", C_MANUAL: "직접 답글 있음",
                  C_HELD: "보류", C_COMMENTS_OFF: "댓글 사용 안 함", C_EXCLUDED: "자동답글 제외", C_DONE: "완료",
                  C_SELF: "내 댓글"}
OPEN_STATES = (C_NEW, C_REVIEW)

REPLY_OFF, REPLY_REVIEW, REPLY_AUTO = "off", "review", "auto_safe"
REPLY_MODE_LABELS = {REPLY_OFF: "사용 안 함", REPLY_REVIEW: "검토 후 답글", REPLY_AUTO: "자동답글 (안전형)"}
DAILY_CAPS = (10, 20, 30)  # 50 이상은 제공하지 않는다
MIN_REPLY_INTERVAL = 60.0
MAX_SAFE_LENGTH = 80
RECORDS_PER_PROFILE = 500
URL_RE = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|net|org|kr|jp|io|ly|me|co)\b|youtu\.be|bit\.ly)", re.I)
# 감사/응원/좋아요/짧은 감상 (이것이 있어야 안전형 자동답글 대상 — 없으면 애매 → 검토)
POSITIVE = ("감사", "고마", "좋아", "좋네", "좋다", "좋은", "최고", "힐링", "잘 들", "잘들", "사랑", "행복", "멋져", "멋지",
            "예뻐", "예쁘", "응원", "편안", "편해", "듣기 좋", "❤", "♥", "💕", "👍", "😊", "🥰",
            "ありがと", "好き", "最高", "素敵", "すてき", "いい曲", "良い", "癒", "嬉し", "応援", "落ち着", "心地",
            "thank", "love", "nice", "great", "beautiful", "amazing", "relax", "awesome")
# 부정/요청/홍보 신호 → 검토
RISKY = ("싫", "별로", "최악", "실망", "광고", "홍보", "구독", "맞구독", "신고", "저작권", "도용", "삭제", "돈", "협찬",
         "嫌", "ひどい", "最悪", "宣伝", "登録して", "著作権", "削除",
         "hate", "bad", "worst", "sub4sub", "subscri", "promo", "copyright", "scam", "free", "giveaway", "dm ", "@")
# 'subscri' = 영어 구독 요청/구독 단어의 앞부분
PROMO_IN_REPLY = ("구독", "좋아요 눌러", "좋아요를 눌러", "알림 설정", "subscri", "like and", "チャンネル登録", "高評価", "通知")

DEFAULT_REPLIES = {
    "ko": ["따뜻한 댓글 감사합니다. 오늘도 좋은 음악과 함께하세요.", "들어주셔서 감사합니다. 다음 플레이리스트에서도 만나요.",
           "좋게 들어주셨다니 정말 기쁩니다.", "편안한 시간이 되셨다니 다행이에요. 감사합니다.",
           "소중한 댓글 남겨주셔서 고맙습니다.", "함께 들어주셔서 감사해요. 좋은 하루 보내세요."],
    "ja": ["コメントありがとうございます。今日も素敵な時間になりますように。", "聴いてくださってありがとうございます。また遊びに来てくださいね。",
           "嬉しいコメント、ありがとうございます。", "心地よい時間になったなら嬉しいです。ありがとうございます。",
           "温かいお言葉をありがとうございます。", "一緒に聴いてくださってありがとうございます。良い一日を。"],
    "en": ["Thank you for listening. Have a lovely day.", "Thanks for the kind words!", "So glad you enjoyed it. Thank you.",
           "Thank you for stopping by. See you at the next playlist.", "Your comment made our day. Thank you."],
}
DEFAULT_FIRST_COMMENTS = {
    "ko": ["오늘도 함께해 주셔서 감사합니다.\n가장 마음에 드는 곡이 있다면 댓글로 남겨주세요.",
           "{title}\n편안하게 들어주세요. 좋았던 곡이 있다면 댓글로 알려주세요."],
    "ja": ["今日も聴きに来てくださってありがとうございます。\nお気に入りの曲があれば、ぜひコメントで教えてください。",
           "{title}\nゆっくりお楽しみください。好きな曲があればコメントで教えてくださいね。"],
    "en": ["Thank you for listening today.\nLet us know your favorite track in the comments."],
}
FIRST_COMMENT_VARIABLES = ("title", "channel", "date", "series", "episode", "filename")


def render_first_comment(text: str, *, title: str, channel: str, local_start: datetime, series: str = "",
                         episode: str = "", filename: str = "", language: str = "ko") -> str:
    """첫 댓글 템플릿 → 실제 문장 ({title} 외에는 영상 템플릿 엔진을 그대로 사용)."""
    body = (text or "").replace("{title}", "\x00TITLE\x00")
    out = render_template(body, local_start=local_start, session=1, channel=channel, series=series, episode=episode,
                          filename=filename, language=language)
    return validate_comment_text(out.replace("\x00TITLE\x00", title))


def assess_comment(text: str, *, exclude_keywords=(), moderation: str = "published") -> tuple[bool, str]:
    """(안전형 자동답글 가능?, 검토가 필요한 이유). 똑똑한 척하지 않는다 — 애매하면 검토."""
    t = (text or "").strip()
    low = t.lower()
    if moderation != "published":
        return False, "보류 중인 댓글"
    if not t:
        return False, "빈 댓글"
    if "?" in t or "？" in t:
        return False, "질문"
    if URL_RE.search(t):
        return False, "링크 포함"
    if len(t) > MAX_SAFE_LENGTH:
        return False, "긴 댓글"
    for k in exclude_keywords or ():
        if k and k.lower() in low:
            return False, f"제외 키워드 '{k}'"
    if any(k in low for k in RISKY):
        return False, "확인이 필요한 표현"
    if not any(k in low for k in POSITIVE):
        return False, "자동 판단 어려움"
    return True, ""


def choose_reply(comment_id: str, templates: list[str], last_text: str = "") -> str:
    """comment_id 해시로 고른다 (재현 가능). 바로 전에 쓴 문장과 같으면 다음 것."""
    pool = [t for t in templates if (t or "").strip()]
    if not pool:
        return ""
    i = int(hashlib.sha256(comment_id.encode("utf-8")).hexdigest()[:8], 16) % len(pool)
    if pool[i] == last_text and len(pool) > 1:
        i = (i + 1) % len(pool)
    return pool[i]


def template_warnings(templates: list[str]) -> list[str]:
    pool = [t.strip() for t in templates if (t or "").strip()]
    out = []
    if len(pool) < 5:
        out.append(f"답글 문구는 5개 이상을 권장합니다 (현재 {len(pool)}개). 같은 문장이 반복되면 스팸처럼 보입니다.")
    if len(set(pool)) < len(pool):
        out.append("같은 답글 문구가 여러 번 들어 있습니다.")
    promo = [t for t in pool if any(p.lower() in t.lower() for p in PROMO_IN_REPLY)]
    if promo:
        out.append("구독/좋아요 요청 같은 홍보 문구는 자동답글에 쓰지 않는 것이 좋습니다 (YouTube 스팸 정책).")
    return out


@dataclass
class CommentRecord:
    comment_id: str  # top-level comment id (= 답글 parentId)
    profile_id: str
    video_id: str
    author: str
    author_channel_id: str
    text: str
    published_at: str
    video_title: str = ""
    total_reply_count: int = 0
    status: str = C_NEW
    reason: str = ""
    recommended: str = ""
    reply_id: str = ""
    reply_text: str = ""
    replied_at: float = 0.0
    auto: bool = False

    @property
    def label(self) -> str:
        base = COMMENT_LABELS.get(self.status, self.status)
        return f"{base} ({self.reason})" if self.status == C_REVIEW and self.reason else base


@dataclass
class CommentSettings:
    profile_id: str
    monitor: bool = True
    reply_mode: str = REPLY_REVIEW
    daily_cap: int = 20
    reply_templates: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    needs_reauth: bool = False
    since: str = ""  # 이 시각 이후 댓글만 자동답글 대상 (처음 켠 뒤 오래된 댓글에 몰아서 답하지 않음)
    last_seen_published: str = ""
    reply_log: list[float] = field(default_factory=list)  # 자동답글 시각 (24시간 한도)
    last_reply_at: float = 0.0
    last_reply_text: str = ""
    last_poll_at: float = 0.0

    def validate(self) -> "CommentSettings":
        if self.reply_mode not in REPLY_MODE_LABELS:
            self.reply_mode = REPLY_REVIEW
        if int(self.daily_cap) not in DAILY_CAPS:
            self.daily_cap = 20
        return self

    def replies_last_24h(self, now: float) -> int:
        return sum(1 for t in self.reply_log if now - t < 86400)

    @classmethod
    def from_dict(cls, d: dict) -> "CommentSettings":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (d or {}).items() if k in known}).validate()


# ---------------- 저장 ----------------

class CommentStore:
    TASKS, RECORDS, SETTINGS = "comment_tasks", "comment_records", "comment_settings"

    def _mutate(self, key: str, fn):
        with SETTINGS_LOCK:
            data = load_settings()
            data[key] = fn(data.get(key))
            save_settings(data)

    # tasks
    def tasks(self) -> list[CommentTask]:
        return [CommentTask.from_dict(d) for d in (load_settings().get(self.TASKS) or []) if isinstance(d, dict)]

    def task(self, task_id: str) -> CommentTask | None:
        return next((t for t in self.tasks() if t.task_id == task_id), None)

    def add_task(self, task: CommentTask) -> bool:
        """같은 task_id(업로드 작업)가 이미 있으면 만들지 않는다."""
        added = []

        def fn(rows):
            rows = [r for r in (rows or []) if isinstance(r, dict)]
            if any(r.get("task_id") == task.task_id for r in rows):
                return rows
            added.append(True)
            return rows + [asdict(task)]
        self._mutate(self.TASKS, fn)
        return bool(added)

    def save_task(self, task: CommentTask) -> None:
        def fn(rows):
            rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("task_id") != task.task_id]
            return rows + [asdict(task)]
        self._mutate(self.TASKS, fn)

    # records
    def records(self, profile_id: str | None = None) -> list[CommentRecord]:
        known = set(CommentRecord.__dataclass_fields__)
        out = [CommentRecord(**{k: v for k, v in d.items() if k in known})
               for d in (load_settings().get(self.RECORDS) or []) if isinstance(d, dict) and d.get("comment_id")]
        out = [r for r in out if profile_id is None or r.profile_id == profile_id]
        return sorted(out, key=lambda r: _ts(r.published_at), reverse=True)

    def record(self, comment_id: str) -> CommentRecord | None:
        return next((r for r in self.records() if r.comment_id == comment_id), None)

    def upsert_records(self, recs: list[CommentRecord]) -> None:
        if not recs:
            return
        ids = {r.comment_id for r in recs}

        def fn(rows):
            rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("comment_id") not in ids]
            rows += [asdict(r) for r in recs]
            by_profile: dict[str, list] = {}
            for r in sorted(rows, key=lambda r: _ts(r.get("published_at", "")), reverse=True):
                by_profile.setdefault(r.get("profile_id", ""), []).append(r)
            return [r for v in by_profile.values() for r in v[:RECORDS_PER_PROFILE]]
        self._mutate(self.RECORDS, fn)

    # settings
    def has_settings(self, profile_id: str) -> bool:
        return isinstance((load_settings().get(self.SETTINGS) or {}).get(profile_id), dict)

    def settings_for(self, profile: ChannelProfile) -> CommentSettings:
        d = (load_settings().get(self.SETTINGS) or {}).get(profile.profile_id)
        if isinstance(d, dict):
            return CommentSettings.from_dict(d)
        lang = profile.language if profile.language in DEFAULT_REPLIES else "ko"
        return CommentSettings(profile.profile_id, reply_templates=list(DEFAULT_REPLIES[lang]))

    def save_settings(self, cs: CommentSettings) -> None:
        cs.validate()
        cs.reply_log = [t for t in cs.reply_log if t > 0][-200:]

        def fn(cur):
            cur = dict(cur) if isinstance(cur, dict) else {}
            cur[cs.profile_id] = asdict(cs)
            return cur
        self._mutate(self.SETTINGS, fn)


# ---------------- 서비스 ----------------

def default_api_factory(profile: ChannelProfile, profiles: ProfileStore) -> YouTubeApiClient:
    return build_profile_api(profile, profiles.token_store(profile.profile_id))


@dataclass
class PollResult:
    new: int = 0
    auto_replied: int = 0
    review: int = 0
    pages: int = 0
    error: str = ""


class CommentService:
    """첫 댓글 작업 + 채널 새 댓글 확인 + 답글. UI는 events 큐로만 갱신 신호를 받는다."""

    def __init__(self, profiles: ProfileStore, store: CommentStore | None = None, *,
                 api_factory: Callable[[ChannelProfile, ProfileStore], YouTubeApiClient] = default_api_factory,
                 jobs: Callable[[], list] = lambda: [], clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, connected: Callable[[ChannelProfile], bool] | None = None,
                 post_delay: float = POST_DELAY, poll_interval: float = 600.0, tick_seconds: float = 30.0):
        self.profiles = profiles
        self.store = store or CommentStore()
        self.api_factory = api_factory
        self.jobs = jobs
        self.clock = clock
        self.sleep = sleep
        self.connected = connected or profiles.is_connected
        self.post_delay = post_delay
        self.poll_interval = poll_interval
        self.tick_seconds = tick_seconds
        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._apis: dict[str, YouTubeApiClient] = {}
        self._verified: set[str] = set()
        self.next_poll_at = 0.0

    def __repr__(self) -> str:
        return f"CommentService(running={self.running})"

    # ---------- api ----------
    def _api(self, profile: ChannelProfile) -> YouTubeApiClient:
        api = self._apis.get(profile.profile_id)
        if api is None:
            api = self._apis[profile.profile_id] = self.api_factory(profile, self.profiles)
        return api

    def _verify(self, profile: ChannelProfile, api: YouTubeApiClient) -> None:
        """댓글/답글을 쓰기 전에 이 연결이 정말 그 채널인지 확인 (한 번의 처리 주기에 1회)."""
        if profile.profile_id not in self._verified:
            verify_channel(api, profile.channel_id)
            self._verified.add(profile.profile_id)

    def _flush_usage(self) -> None:
        for api in self._apis.values():
            calls = list(getattr(api, "calls", []))
            if calls:
                record_api_calls(calls, self.clock)
                api.calls.clear()

    def _end_cycle(self) -> None:
        self._flush_usage()
        self._apis.clear()
        self._verified.clear()
        self.events.put(("changed",))

    # ---------- 첫 댓글 ----------
    def sync_tasks(self, jobs=None) -> int:
        """업로드가 끝난(COMPLETE/PARTIAL) 작업 중 첫 댓글이 있는 것 → CommentTask (작업당 1개, 재실행 안전)."""
        from .youtube_upload_queue import COMPLETE, PARTIAL
        now = self.clock()
        made = 0
        for j in (self.jobs() if jobs is None else jobs):
            if j.status not in (COMPLETE, PARTIAL) or not j.video_id or not (j.first_comment or "").strip():
                continue
            if j.publish_at_utc:
                status, next_at = WAITING_PUBLIC, _ts(j.publish_at_utc) - PUBLISH_LEAD
            elif j.privacy in ("public", "unlisted"):
                status, next_at = READY, now + self.post_delay
            else:
                status, next_at = WAITING_PRIVACY_CHANGE, 0.0
            t = CommentTask(j.job_id, j.profile_id, j.channel_id, j.video_id, j.title, j.first_comment,
                            publish_at_utc=j.publish_at_utc, privacy=j.privacy, status=status, next_at=next_at)
            made += self.store.add_task(t)
        if made:
            self.events.put(("changed",))
        return made

    def task_for_job(self, job_id: str) -> CommentTask | None:
        return self.store.task(job_id)

    def cancel_task(self, task_id: str) -> None:
        with self._lock:
            t = self.store.task(task_id)
            if t and t.status not in (POSTED,):
                t.status = CANCELLED
                self.store.save_task(t)

    def process_tasks(self, *, force: bool = False) -> int:
        """기한이 된 작업만 처리. force=True(앱 시작/지금 확인)면 비공개 대기 작업도 한 번 확인한다."""
        done = 0
        with self._lock:
            try:
                for t in self.store.tasks():
                    if t.status in TASK_TERMINAL:
                        continue
                    if t.status == WAITING_PRIVACY_CHANGE and not force:
                        continue
                    if t.status in (WAITING_PUBLIC, READY) and self.clock() < t.next_at and not force:
                        continue
                    if t.status == READY and self.clock() < t.next_at:
                        continue  # 공개 확인 뒤 기다리는 중 (force여도 바로 쓰지 않음)
                    self._step(t)
                    done += 1
            finally:
                self._end_cycle()
        return done

    def _step(self, t: CommentTask) -> None:
        now = self.clock()
        profile = self.profiles.get(t.profile_id)
        if t.first_comment_id:
            t.status = POSTED
            return self.store.save_task(t)
        if profile is None or profile.channel_id != t.channel_id:
            t.status, t.error_kind, t.error = FAILED, E_CHANNEL, "YouTube 채널이 삭제되었거나 다른 채널로 바뀌었습니다."
            return self.store.save_task(t)
        try:
            api = self._api(profile)
            if t.status == POSTING:  # 재실행: 등록 직전/직후에 꺼졌다 → 이미 달렸는지 먼저 확인
                found = self._find_own_comment(profile, api, t)
                if found:
                    t.status, t.first_comment_id, t.posted_at = POSTED, found, now
                    return self.store.save_task(t)
                t.status = READY
            st = api.get_video_status(t.video_id)
            privacy = st.get("privacyStatus", "")
            if privacy in ("public", "unlisted"):
                if t.status != READY or t.next_at == 0.0:
                    t.status, t.next_at, t.error_kind, t.error = READY, now + self.post_delay, "", ""
                    return self.store.save_task(t)
                if now < t.next_at:
                    return self.store.save_task(t)
                if st.get("uploadStatus") == "uploaded":  # 아직 처리 중
                    t.next_at = now + 120
                    return self.store.save_task(t)
                return self._post(profile, api, t)
            self._wait_private(t, st, now)
            self.store.save_task(t)
        except Exception as e:  # noqa: BLE001 - 분류해서 상태로 남긴다
            self._fail(t, e, now, profile)

    def _wait_private(self, t: CommentTask, st: dict, now: float) -> None:
        pub = st.get("publishAt") or t.publish_at_utc
        if not pub:
            t.status, t.next_at = WAITING_PRIVACY_CHANGE, 0.0
            t.error_kind, t.error = E_VIDEO_PRIVATE, "비공개 영상에는 댓글을 달 수 없습니다. 공개/일부공개로 바꾸면 다음 확인 때 등록합니다."
            return
        pt = _ts(pub)
        if now < pt - PUBLISH_LEAD:
            t.status, t.next_at, t.error_kind, t.error = WAITING_PUBLIC, pt - PUBLISH_LEAD, "", ""
        elif now < pt:  # 공개 직전: 공개 시각 직후에 다시 확인
            t.status, t.next_at, t.error_kind, t.error = WAITING_PUBLIC, pt + 15, "", ""
        elif now - pt > LATE_PUBLISH_GIVE_UP:
            t.status, t.next_at = WAITING_PRIVACY_CHANGE, 0.0
            t.error_kind, t.error = E_VIDEO_PRIVATE, "예약 시각이 지났는데 아직 비공개입니다. YouTube Studio에서 공개 상태를 확인하세요."
        else:
            t.status, t.next_at, t.error_kind, t.error = WAITING_PUBLIC, now + 120, "", ""

    def _post(self, profile: ChannelProfile, api: YouTubeApiClient, t: CommentTask) -> None:
        self._verify(profile, api)
        t.status = POSTING
        self.store.save_task(t)
        thread = api.insert_top_level_comment(profile.channel_id, t.video_id, t.text)
        t.status, t.first_comment_id, t.posted_at = POSTED, thread.top.id or thread.id, self.clock()
        t.error_kind = t.error = ""
        self.store.save_task(t)
        self.events.put(("first_comment", t.task_id))

    def _find_own_comment(self, profile: ChannelProfile, api: YouTubeApiClient, t: CommentTask) -> str:
        token = ""
        for _ in range(2):
            threads, token = api.list_channel_comment_threads(profile.channel_id, page_token=token, max_results=50)
            for th in threads:
                if th.video_id == t.video_id and th.top.author_channel_id == profile.channel_id \
                        and th.top.text.strip() == t.text.strip():
                    return th.top.id
            if not token:
                break
        return ""

    def _fail(self, t: CommentTask, e: Exception, now: float, profile) -> None:
        kind = classify_error(e)
        t.error_kind, t.error = kind, str(e) if isinstance(e, (YouTubeApiError, OAuthError)) else f"오류 ({type(e).__name__})"
        if t.status == POSTING:
            t.status = READY
        t.attempts += 1
        if kind == E_COMMENTS_DISABLED:
            t.status = COMMENTS_DISABLED
        elif kind in (E_INSUFFICIENT_PERMISSION, E_AUTH):
            t.status, t.error = FAILED, REAUTH_MESSAGE if kind == E_INSUFFICIENT_PERMISSION else t.error
            if profile is not None and kind == E_INSUFFICIENT_PERMISSION:
                cs = self.store.settings_for(profile)
                cs.needs_reauth = True
                self.store.save_settings(cs)
        elif kind in (E_VIDEO_NOT_FOUND, E_CHANNEL, E_OTHER):
            t.status = FAILED
        elif kind == E_FORBIDDEN:  # 비공개로 다시 바뀐 영상 등 → 한 번 더 상태 확인 후 판단
            t.status, t.next_at = WAITING_PUBLIC, now + 300
        elif kind == E_QUOTA_EXCEEDED:
            t.next_at = now + 6 * 3600
        elif kind == E_RATE_LIMITED:
            t.next_at = now + 300 * (2 ** min(t.attempts - 1, 4))
        else:  # network
            t.next_at = now + 120
        if t.status not in TASK_TERMINAL and t.attempts >= MAX_ATTEMPTS:
            t.status = FAILED
        self.store.save_task(t)

    # ---------- 새 댓글 ----------
    def monitored_profiles(self) -> list[ChannelProfile]:
        out = []
        for p in self.profiles.all():
            if p.channel_id and self.store.has_settings(p.profile_id) and self.store.settings_for(p).monitor \
                    and self.connected(p):
                out.append(p)
        return out

    def poll_all(self) -> dict[str, PollResult]:
        out = {}
        for p in self.monitored_profiles():
            out[p.profile_id] = self.poll_profile(p)
        self.next_poll_at = self.clock() + self.poll_interval
        return out

    def poll_profile(self, profile: ChannelProfile, *, max_pages: int = 10) -> PollResult:
        """채널 전체 새 댓글 (order=time). 마지막으로 본 댓글/시각에서 멈춘다 (최대 max_pages, 처음엔 1페이지)."""
        res = PollResult()
        with self._lock:
            try:
                self._poll(profile, res, max_pages)
            except Exception as e:  # noqa: BLE001
                kind = classify_error(e)
                res.error = REAUTH_MESSAGE if kind == E_INSUFFICIENT_PERMISSION else (
                    str(e) if isinstance(e, (YouTubeApiError, OAuthError)) else f"오류 ({type(e).__name__})")
                if kind == E_INSUFFICIENT_PERMISSION:
                    cs = self.store.settings_for(profile)
                    cs.needs_reauth = True
                    self.store.save_settings(cs)
            finally:
                self._end_cycle()
        return res

    def _poll(self, profile: ChannelProfile, res: PollResult, max_pages: int) -> None:
        now = self.clock()
        cs = self.store.settings_for(profile)
        first = not cs.since
        if first:
            cs.since = _iso(now)
        api = self._api(profile)
        existing = {r.comment_id: r for r in self.store.records(profile.profile_id)}
        own_first = {t.first_comment_id for t in self.store.tasks() if t.first_comment_id}
        titles = {t.video_id: t.title for t in self.store.tasks()}
        titles.update({j.video_id: j.title for j in self.jobs() if getattr(j, "video_id", "")})
        last_seen = _ts(cs.last_seen_published)
        newest = last_seen
        new: list[CommentRecord] = []
        updated: list[CommentRecord] = []
        token = ""
        for _ in range(1 if first else max_pages):
            threads, token = api.list_channel_comment_threads(profile.channel_id, page_token=token, max_results=50)
            res.pages += 1
            reached = False
            for th in threads:
                pub = _ts(th.top.published_at)
                newest = max(newest, pub)
                old = existing.get(th.top.id)
                if old is not None:
                    reached = True
                    if old.status in OPEN_STATES and any(r.author_channel_id == profile.channel_id and r.id != old.reply_id
                                                         for r in th.replies):
                        old.status, old.reason = C_MANUAL, ""
                        updated.append(old)
                    continue
                if last_seen and pub < last_seen:
                    reached = True
                    continue
                rec = self._record(profile, cs, th, own_first, titles)
                new.append(rec)
            if reached or not token:
                break
        cs.last_seen_published = _iso(newest) if newest else cs.last_seen_published
        cs.last_poll_at = now
        self.store.save_settings(cs)
        self.store.upsert_records(new + updated)
        res.new = sum(r.status in OPEN_STATES for r in new)
        res.review = sum(r.status == C_REVIEW for r in new)
        if cs.reply_mode == REPLY_AUTO:
            res.auto_replied = self._auto_reply(profile, api)

    def _record(self, profile, cs: CommentSettings, th, own_first: set, titles: dict) -> CommentRecord:
        top = th.top
        rec = CommentRecord(top.id, profile.profile_id, th.video_id, top.author, top.author_channel_id, top.text,
                            top.published_at, video_title=titles.get(th.video_id, ""),
                            total_reply_count=th.total_reply_count)
        if top.author_channel_id == profile.channel_id or top.id in own_first:
            rec.status = C_SELF
        elif any(r.author_channel_id == profile.channel_id for r in th.replies):
            rec.status = C_MANUAL
        elif th.moderation_status != "published":
            rec.status, rec.reason = C_HELD, "보류 중인 댓글"
        elif not th.can_reply:
            rec.status = C_COMMENTS_OFF
        else:
            safe, reason = assess_comment(top.text, exclude_keywords=cs.exclude_keywords)
            rec.status, rec.reason = (C_NEW, "") if safe else (C_REVIEW, reason)
        rec.recommended = choose_reply(top.id, cs.reply_templates, cs.last_reply_text)
        return rec

    # ---------- 답글 ----------
    def _owner_replied(self, profile, api, rec: CommentRecord) -> bool:
        if rec.total_reply_count <= 0:
            return False
        return any(r.author_channel_id == profile.channel_id for r in api.list_comment_replies(rec.comment_id))

    def _auto_reply(self, profile: ChannelProfile, api: YouTubeApiClient) -> int:
        cs = self.store.settings_for(profile)
        since = _ts(cs.since)
        cands = [r for r in self.store.records(profile.profile_id)
                 if r.status == C_NEW and not r.reply_id and _ts(r.published_at) >= since]
        cands.sort(key=lambda r: _ts(r.published_at))
        n = 0
        for rec in cands:
            if self.stop_event.is_set():
                break
            now = self.clock()
            if cs.replies_last_24h(now) >= cs.daily_cap:
                break
            wait = cs.last_reply_at + MIN_REPLY_INTERVAL - now
            if wait > 0:
                self.sleep(wait)
            text = choose_reply(rec.comment_id, cs.reply_templates, cs.last_reply_text)
            if not text:
                break
            try:
                if self._owner_replied(profile, api, rec):
                    rec.status = C_MANUAL
                    self.store.upsert_records([rec])
                    continue
                self._verify(profile, api)
                reply = api.insert_comment_reply(rec.comment_id, text)
            except Exception as e:  # noqa: BLE001
                kind = classify_error(e)
                if kind == E_COMMENTS_DISABLED:
                    rec.status = C_COMMENTS_OFF
                    self.store.upsert_records([rec])
                    continue
                break  # 권한/한도/네트워크: 이번 주기는 멈춘다
            now = self.clock()
            rec.status, rec.reply_id, rec.reply_text, rec.replied_at, rec.auto = C_REPLIED, reply.id, text, now, True
            cs.reply_log.append(now)
            cs.last_reply_at, cs.last_reply_text = now, text
            self.store.upsert_records([rec])
            self.store.save_settings(cs)
            n += 1
        return n

    def reply(self, comment_id: str, text: str) -> CommentRecord:
        """[답글 작성]/[추천 답글 사용]. 이미 답한 댓글/내 댓글/직접 답글 있는 댓글에는 다시 쓰지 않는다."""
        with self._lock:
            try:
                rec = self.store.record(comment_id)
                if rec is None:
                    raise ValueError("댓글을 찾을 수 없습니다.")
                if rec.reply_id or rec.status == C_REPLIED:
                    raise ValueError("이미 이 프로그램에서 답글을 달았습니다 (중복 답글 방지).")
                if rec.status in (C_SELF, C_COMMENTS_OFF):
                    raise ValueError(f"'{COMMENT_LABELS[rec.status]}' 댓글에는 답글을 달지 않습니다.")
                body = validate_comment_text(text, "답글")
                profile = self.profiles.get(rec.profile_id)
                if profile is None or not profile.channel_id:
                    raise ValueError("이 YouTube 채널이 연결되어 있지 않습니다.")
                api = self._api(profile)
                if self._owner_replied(profile, api, rec):
                    rec.status = C_MANUAL
                    self.store.upsert_records([rec])
                    raise ValueError("YouTube Studio에서 이미 직접 답글을 달았습니다.")
                self._verify(profile, api)
                reply = api.insert_comment_reply(rec.comment_id, body)
                rec.status, rec.reply_id, rec.reply_text, rec.replied_at, rec.auto = C_REPLIED, reply.id, body, self.clock(), False
                self.store.upsert_records([rec])
                cs = self.store.settings_for(profile)
                cs.last_reply_text = body
                self.store.save_settings(cs)
                return rec
            finally:
                self._end_cycle()

    def mark(self, comment_id: str, status: str) -> None:
        if status not in (C_EXCLUDED, C_DONE, C_REVIEW):
            raise ValueError(status)
        with self._lock:
            rec = self.store.record(comment_id)
            if rec is not None and not rec.reply_id:
                rec.status = status
                self.store.upsert_records([rec])
        self.events.put(("changed",))

    def counts(self, profile_id: str | None = None) -> dict:
        recs = [r for r in self.store.records(profile_id) if r.status != C_SELF]
        tasks = [t for t in self.store.tasks() if profile_id is None or t.profile_id == profile_id]
        return {"new": sum(r.status == C_NEW for r in recs), "replied": sum(r.status == C_REPLIED for r in recs),
                "auto_replied": sum(r.status == C_REPLIED and r.auto for r in recs),
                "review": sum(r.status == C_REVIEW for r in recs),
                "waiting_first": sum(t.status not in TASK_TERMINAL for t in tasks),
                "posted_first": sum(t.status == POSTED for t in tasks)}

    # ---------- 백그라운드 (프로그램이 켜져 있을 때만) ----------
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def run_once(self, *, catch_up: bool = False) -> None:
        self.sync_tasks()
        self.process_tasks(force=catch_up)
        if catch_up or self.clock() >= self.next_poll_at:
            self.poll_all()

    def _loop(self) -> None:
        first = True
        while not self.stop_event.is_set():
            try:
                self.run_once(catch_up=first)
            except Exception:  # pragma: no cover - 다음 주기에 다시
                pass
            first = False
            self.stop_event.wait(self.tick_seconds)

    def start(self) -> bool:
        if self.running:
            return False
        self.stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="youtube-comments", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 3.0) -> None:
        self.stop_event.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)
