"""채널별 YouTube LIVE 방송 정보 — 제목·설명·태그·썸네일·카테고리·YouTube 재생목록·공개 상태 (Tk 비의존).

- 저장: settings.json "live_channel_metadata" = {LIVE 채널 ID: {...}}. 비밀이 아닌 값만 (Stream Key/token 없음).
  채널(시니어/도쿄칠/…)마다 따로 저장되어 서로 섞이지 않는다. 키가 없으면 기본값 (migration 불필요).
- 검증: youtube_metadata의 예약 LIVE validator를 그대로 쓴다 (예약 LIVE 창과 같은 규칙).
- 적용: youtube_api.YouTubeApiClient의 기존 호출만 쓴다 (videos.update / thumbnails.set / playlistItems.insert).
  결과는 항목별로 기록하고, 실패한 항목만 다시 적용할 수 있다. 성공한 항목은 되돌리지 않는다.
- "YouTube 재생목록"(YouTube 채널 안의 재생목록)은 LIVE 창의 "송출 영상 Playlist"(Cloud에서 반복할 MP4 목록)와 다르다.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from . import settings as _settings
from .youtube_api import YouTubeApiError, YouTubeBroadcastInfo
from .youtube_metadata import (
    DEFAULT_CATEGORIES, DEFAULT_CATEGORY_ID, DESCRIPTION_MAX, PRIVACY_LABELS, TITLE_MAX, BroadcastMetadata, MetadataError,
    parse_tags,
)
from .youtube_oauth import OAuthError

SETTINGS_KEY = "live_channel_metadata"
CATEGORY_CACHE_KEY = "youtube_category_cache"
BANNED_KEYS = {"refresh_token", "access_token", "client_secret", "stream_name", "stream_key", "token", "key"}
DEFAULT_PRIVACY = "unlisted"
DEFAULT_REGION = "KR"

STEP_TITLE = "title_description"
STEP_PRIVACY = "privacy"
STEP_TAGS = "tags"
STEP_CATEGORY = "category"
STEP_THUMBNAIL = "thumbnail"
STEP_PLAYLIST = "playlist"
STEPS = (STEP_TITLE, STEP_PRIVACY, STEP_TAGS, STEP_CATEGORY, STEP_THUMBNAIL, STEP_PLAYLIST)
STEP_LABELS = {STEP_TITLE: "제목/설명", STEP_PRIVACY: "공개 상태", STEP_TAGS: "태그", STEP_CATEGORY: "카테고리",
               STEP_THUMBNAIL: "썸네일", STEP_PLAYLIST: "YouTube 재생목록"}
SNIPPET_STEPS = (STEP_TITLE, STEP_TAGS, STEP_CATEGORY)  # videos.update(part=snippet) 한 번으로 함께 적용

NO_PLAYLIST = "선택 안 함"
LOCAL_ONLY_TEXT = ("방송 정보는 이 프로그램에 저장됩니다.\n"
                   "YouTube에 자동 반영하려면 YouTube 연결이 필요합니다.")
SAVED_TEXT = "✓ 로컬에 저장됨 — YouTube 자동 반영에는 계정 연결이 필요합니다."
LIFE_LABELS = {"live": "LIVE 중", "liveStarting": "LIVE 시작 중", "testing": "테스트 중", "ready": "예정",
               "created": "예정"}


# ---------------- 모델 ----------------

@dataclass
class LiveMetadata:
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    thumbnail_path: str = ""
    category_id: str = DEFAULT_CATEGORY_ID
    youtube_playlist_id: str = ""  # YouTube 재생목록 ID ("" = 선택 안 함)
    youtube_playlist_title: str = ""  # 화면 표시용 (목록을 못 받아도 저장된 이름을 보여 준다)
    privacy: str = DEFAULT_PRIVACY

    def validate(self, *, check_thumbnail: bool = True) -> "LiveMetadata":
        """예약 LIVE와 같은 규칙 (빈 제목/100자 초과/< > 차단, 설명 5000자, 태그 정리·500자, 썸네일 JPG/PNG).
        check_thumbnail=False: 적용할 때 — 썸네일 문제는 썸네일 항목 실패로만 기록한다 (다른 항목은 계속)."""
        md = self.to_broadcast_metadata()
        if not check_thumbnail:
            md.thumbnail_path = ""
        md = md.validate("방송 제목")
        self.title, self.description, self.tags = md.title, md.description, md.tags
        self.youtube_playlist_id = (self.youtube_playlist_id or "").strip()
        if not self.youtube_playlist_id:
            self.youtube_playlist_title = ""
        return self

    def to_broadcast_metadata(self) -> BroadcastMetadata:
        return BroadcastMetadata(title=self.title, description=self.description, tags=list(self.tags),
                                 thumbnail_path=(self.thumbnail_path or "").strip(), category_id=str(self.category_id),
                                 privacy_status=self.privacy)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d) -> "LiveMetadata":
        """저장 값이 깨져 있어도 읽기는 실패하지 않는다 (잘못된 칸만 기본값)."""
        d = d if isinstance(d, dict) else {}
        out = cls()
        for k in ("title", "description", "thumbnail_path", "youtube_playlist_id", "youtube_playlist_title"):
            if isinstance(d.get(k), str):
                setattr(out, k, d[k])
        if isinstance(d.get("tags"), list):
            out.tags = [str(t) for t in d["tags"] if str(t).strip()]
        if str(d.get("category_id", "")).isdigit():
            out.category_id = str(d["category_id"])
        if d.get("privacy") in PRIVACY_LABELS:
            out.privacy = d["privacy"]
        return out


def title_count_text(title: str) -> str:
    return f"{len((title or '').strip())} / {TITLE_MAX}자"


def description_count_text(desc: str) -> str:
    return f"{len((desc or '').replace(chr(13) + chr(10), chr(10)))} / {DESCRIPTION_MAX}자"


def normalize_tags(text) -> list[str]:
    """'Tokyo Chill, R&B' 또는 줄바꿈 → 앞뒤 공백 제거·빈 값 제거·중복 제거(순서 유지). 예약 LIVE parse_tags 그대로."""
    return parse_tags(text)


# ---------------- 공개 상태 (예약 LIVE와 같은 표시명) ----------------

def privacy_label(value: str) -> str:
    return PRIVACY_LABELS.get(value, PRIVACY_LABELS[DEFAULT_PRIVACY])


def privacy_value(label: str) -> str:
    return next((k for k, v in PRIVACY_LABELS.items() if v == label or k == label), DEFAULT_PRIVACY)


# ---------------- 채널별 저장 ----------------

def _all_rows() -> dict:
    rows = _settings.load_settings().get(SETTINGS_KEY)
    return dict(rows) if isinstance(rows, dict) else {}


def load_metadata(profile_id: str) -> LiveMetadata:
    return LiveMetadata.from_dict(_all_rows().get(profile_id))


def has_saved_metadata(profile_id: str) -> bool:
    return isinstance(_all_rows().get(profile_id), dict)


def save_metadata(profile_id: str, md: LiveMetadata) -> LiveMetadata:
    """검증 후 이 채널 값만 바꾼다 (다른 채널 값은 그대로). 비밀 키는 거부."""
    if not profile_id:
        raise MetadataError("채널을 찾을 수 없습니다.")
    md.validate()
    row = md.to_dict()
    if BANNED_KEYS & set(row):
        raise ValueError("secret must not be stored in settings.json")
    with _settings.SETTINGS_LOCK:
        rows = _all_rows()
        rows[profile_id] = row
        _settings.update_settings(**{SETTINGS_KEY: rows})
    return md


# ---------------- 카테고리 (videoCategories.list + cache) ----------------

def cached_categories(region: str = DEFAULT_REGION) -> list[tuple[str, str]]:
    """저장된 목록 (없으면 기본 음악/엔터테인먼트/인물·블로그/비영리)."""
    c = _settings.load_settings().get(CATEGORY_CACHE_KEY)
    items = (c or {}).get(region, {}).get("items") if isinstance(c, dict) and isinstance(c.get(region), dict) else None
    out = [(str(i), str(t)) for i, t in items or [] if str(i).isdigit() and t] if isinstance(items, list) else []
    return out or list(DEFAULT_CATEGORIES.items())


def fetch_categories(api, region: str = DEFAULT_REGION, hl: str = "ko") -> tuple[list[tuple[str, str]], str]:
    """YouTube에서 받아 저장. 실패하면 (저장된/기본 목록, 오류 문구) — 저장된 category ID는 바꾸지 않는다."""
    try:
        got = [(i, t) for i, t in api.list_video_categories(region, hl) if str(i).isdigit() and t]
    except (YouTubeApiError, OAuthError, OSError) as e:
        return cached_categories(region), str(e)
    if not got:
        return cached_categories(region), "카테고리 목록이 비어 있습니다."
    with _settings.SETTINGS_LOCK:
        c = _settings.load_settings().get(CATEGORY_CACHE_KEY)
        c = dict(c) if isinstance(c, dict) else {}
        c[region] = {"items": [list(x) for x in got], "fetched_at": int(time.time())}
        _settings.update_settings(**{CATEGORY_CACHE_KEY: c})
    return got, ""


def category_label(category_id: str, choices) -> str:
    return dict(choices).get(str(category_id)) or f"카테고리 {category_id} (저장된 값)"


def category_id_for(label: str, choices, current: str) -> str:
    """표시명 → ID. 모르는 표시명이면 현재(저장된) ID 유지 — 임의의 다른 카테고리로 바꾸지 않는다."""
    return next((i for i, t in choices if t == label), current)


# ---------------- YouTube 재생목록 (playlists.list mine=true) ----------------

def playlist_choices(playlists, saved_id: str = "", saved_title: str = "") -> list[tuple[str, str]]:
    """[("", 선택 안 함), (ID, 이름) …]. 저장된 재생목록이 목록에 없으면 '(저장됨)'으로 남겨 둔다 (몰래 지우지 않음)."""
    out = [("", NO_PLAYLIST)] + [(p.id, p.title or p.id) for p in playlists or []]
    if saved_id and all(i != saved_id for i, _ in out):
        out.append((saved_id, f"{saved_title or saved_id} (저장됨)"))
    return out


# ---------------- 적용할 방송 (Stream Key 직접 송출: 사용자가 직접 고른다) ----------------

def list_target_broadcasts(api) -> list[YouTubeBroadcastInfo]:
    """지금 진행 중 + 예정된 내 방송. 자동으로 하나를 고르지 않는다."""
    out, seen = [], set()
    for b in list(api.list_active_broadcasts()) + list(api.list_upcoming_broadcasts()):
        if b.id and b.id not in seen and b.life_cycle_status not in ("complete", "revoked"):
            seen.add(b.id)
            out.append(b)
    return out


def broadcast_label(b: YouTubeBroadcastInfo) -> str:
    when = (b.scheduled_start or "")[:16].replace("T", " ")
    state = LIFE_LABELS.get(b.life_cycle_status, b.life_cycle_status or "?")
    return f"{b.title or '(제목 없음)'} · {state}" + (f" · {when}" if when else "")


def change_lines(md: LiveMetadata, categories=None) -> list[str]:
    """확인창 '변경' 목록 (Stream Key/token 없음)."""
    lines = [f"제목: {md.title}", f"설명: {len(md.description)}자", f"태그: {', '.join(md.tags) or '(없음)'}",
             f"카테고리: {category_label(md.category_id, categories or cached_categories())}"]
    if md.thumbnail_path:
        lines.append(f"썸네일: {md.thumbnail_path.replace(chr(92), '/').rsplit('/', 1)[-1]}")
    if md.youtube_playlist_id:
        lines.append(f"YouTube 재생목록: {md.youtube_playlist_title or md.youtube_playlist_id}")
    lines.append(f"공개 상태: {privacy_label(md.privacy)}")
    return lines


# ---------------- 적용 결과 ----------------

@dataclass
class ApplyResult:
    video_id: str = ""
    broadcast_title: str = ""
    channel_title: str = ""
    steps: dict = field(default_factory=dict)  # step → True(성공) / False(실패)
    errors: dict = field(default_factory=dict)  # step → 사용자 문구 (token 없음)
    playlist_state: str = ""  # added | already

    def failed_steps(self) -> list[str]:
        return [s for s in STEPS if self.steps.get(s) is False]

    @property
    def ok(self) -> bool:
        return not self.failed_steps()

    def summary_lines(self) -> list[str]:
        out = []
        for s in STEPS:
            if s not in self.steps:
                continue
            if self.steps[s]:
                extra = " (이미 들어 있음)" if s == STEP_PLAYLIST and self.playlist_state == "already" else ""
                out.append(f"✓ {STEP_LABELS[s]}{extra}")
            else:
                out.append(f"✗ {STEP_LABELS[s]}: {self.errors.get(s, '')}")
        return out


def requested_steps(md: LiveMetadata) -> list[str]:
    steps = [STEP_TITLE, STEP_PRIVACY, STEP_TAGS, STEP_CATEGORY]
    if (md.thumbnail_path or "").strip():
        steps.append(STEP_THUMBNAIL)
    if md.youtube_playlist_id:
        steps.append(STEP_PLAYLIST)
    return steps


def _fail(result: ApplyResult, step: str, e: Exception) -> None:
    result.steps[step] = False
    result.errors[step] = str(e) if isinstance(e, (YouTubeApiError, OAuthError, MetadataError)) else \
        f"{STEP_LABELS[step]} 적용 오류 ({type(e).__name__})"


def _ok(result: ApplyResult, step: str) -> None:
    result.steps[step] = True
    result.errors.pop(step, None)


def apply_metadata_steps(api, video_id: str, md: LiveMetadata, *, steps=None, result: ApplyResult | None = None,
                         channel_id: str = "") -> ApplyResult:
    """video_id 하나에만 적용. steps를 주면 그 항목만 (실패 항목 다시 적용). 한 항목 실패가 다른 항목을 막지 않는다.

    channel_id: 재생목록이 이 YouTube 채널 것인지 확인 (다른 계정의 재생목록에 넣지 않음)."""
    if not video_id:
        raise ValueError("적용할 방송이 선택되지 않았습니다.")
    md.validate(check_thumbnail=False)
    r = result or ApplyResult(video_id=video_id)
    steps = [s for s in (steps if steps is not None else requested_steps(md)) if s in STEPS]
    snippet = [s for s in SNIPPET_STEPS if s in steps]
    if snippet:  # 제목/설명·태그·카테고리: 기존 snippet을 읽어 바꿀 값만 바꿔 보낸다 (다른 값이 지워지지 않게)
        try:
            api.update_video_metadata(
                video_id,
                title=md.title if STEP_TITLE in snippet else None,
                description=md.description if STEP_TITLE in snippet else None,
                tags=list(md.tags) if STEP_TAGS in snippet else None,
                category_id=md.category_id if STEP_CATEGORY in snippet else None)
            for s in snippet:
                _ok(r, s)
        except Exception as e:  # noqa: BLE001 — 항목 실패로 기록하고 다음 항목 계속
            for s in snippet:
                _fail(r, s, e)
    if STEP_PRIVACY in steps:
        try:
            cur = api.get_video_status(video_id)
            kids = bool(cur.get("selfDeclaredMadeForKids", cur.get("madeForKids", False)))
            api.update_video_status(video_id, privacy=md.privacy, made_for_kids=kids)
            _ok(r, STEP_PRIVACY)
        except Exception as e:  # noqa: BLE001
            _fail(r, STEP_PRIVACY, e)
    if STEP_THUMBNAIL in steps:
        from .youtube_schedule import ReservationResult, upload_thumbnail  # 예약 LIVE와 같은 썸네일 업로드
        tr = ReservationResult()
        try:
            upload_thumbnail(api, video_id, md.thumbnail_path, tr)
        except Exception as e:  # noqa: BLE001
            _fail(r, STEP_THUMBNAIL, e)
        else:
            if tr.thumbnail_ok is True:
                _ok(r, STEP_THUMBNAIL)
            else:
                r.steps[STEP_THUMBNAIL] = False
                r.errors[STEP_THUMBNAIL] = tr.errors.get("thumbnail") or "썸네일 파일을 선택하세요."
    if STEP_PLAYLIST in steps:
        pid = md.youtube_playlist_id
        try:
            if not pid:
                raise MetadataError("YouTube 재생목록이 선택되지 않았습니다.")
            if channel_id:
                api.ensure_own_playlist(pid, channel_id)
            if r.playlist_state in ("added", "already") or api.playlist_contains(pid, video_id):
                r.playlist_state = r.playlist_state or "already"
            else:
                r.playlist_state = "already" if api.add_video_to_playlist(pid, video_id) == "already" else "added"
            _ok(r, STEP_PLAYLIST)
        except Exception as e:  # noqa: BLE001
            _fail(r, STEP_PLAYLIST, e)
    return r


def apply_after_create(api, video_id: str, md: LiveMetadata, *, channel_id: str = "") -> ApplyResult:
    """API 자동 세션: 방송 생성(insert) 때 제목/설명/공개 상태는 이미 들어갔다 → 태그·카테고리·썸네일·재생목록만."""
    r = ApplyResult(video_id=video_id, broadcast_title=md.title)
    r.steps[STEP_TITLE] = r.steps[STEP_PRIVACY] = True
    rest = [s for s in requested_steps(md) if s not in (STEP_TITLE, STEP_PRIVACY)]
    return apply_metadata_steps(api, video_id, md, steps=rest, result=r, channel_id=channel_id)
