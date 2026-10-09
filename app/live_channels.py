"""여러 채널 동시 Cloud LIVE — 채널 Profile (Tk 비의존).

한 프로그램에서 시니어 채널 + 일본 채널처럼 최대 2개 채널을 무료 Cloud에서 동시에 송출한다.
채널마다 따로: 영상 Playlist · Stream Key(DPAPI 파일) · Cloud 송출(서버 경로/서비스) · YouTube 연결(OAuth token) · 예약.

- 기본 채널(default)은 기존 1채널 설정을 그대로 쓴다: live_secret.dat · settings["youtube"] · long-live.service.
  → 기존 사용자는 아무것도 옮기지 않아도 지금처럼 동작한다.
- settings.json "live_channels"에는 비밀이 아닌 값만 저장한다 (Stream Key/token 없음).
- 채널 Stream Key: live_secret_<채널ID>.dat (Windows DPAPI). 두 채널 key를 같은 파일에 저장하지 않는다.
- 채널 YouTube 연결: youtube_accounts 프로필 token(youtube_token_<프로필ID>.dat)을 채널별로 연결.
  같은 Google 연결 파일(OAuth Client JSON)을 여러 채널에 써도 token은 채널(프로필)마다 따로다.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
from dataclasses import asdict, dataclass, field

from . import live_secrets, settings as _settings
from .cloud_model import (
    DEFAULT_LIVE_PROFILE, MAX_CONCURRENT_LIVE, CloudConfigError, validate_live_profile_id,
)
from .live_playlist import MAX_PLAYLIST_ITEMS

SETTINGS_KEY = "live_channels"
SELECTED_KEY = "live_channel_selected"
DEFAULT_NAME = "기본 채널"
MAX_CHANNELS = 10  # 저장 가능한 채널 수 (동시 송출은 MAX_CONCURRENT_LIVE개)
STREAM_MODE_MANUAL = "MANUAL_STREAM_KEY"
STREAM_MODE_API = "API"
STREAM_MODES = (STREAM_MODE_MANUAL, STREAM_MODE_API)
BANNED_KEYS = {"refresh_token", "access_token", "client_secret", "stream_name", "stream_key", "token", "key"}
BACKUP_SUFFIX = ".bak-before-multichannel"
STREAM_OVERHEAD = 0.10  # RTMPS/TCP/FLV 오버헤드 대략치
BANDWIDTH_WARN_MBPS = 25.0  # 두 채널 합계가 이보다 크면 경고 (무료 서버 네트워크 한도는 Oracle Console에서 확인)
DEFAULT_AUDIO_KBPS = 128


class ChannelError(ValueError):
    pass


@dataclass
class LiveChannelProfile:
    channel_profile_id: str
    display_name: str
    youtube_channel_id: str = ""
    stream_mode: str = STREAM_MODE_MANUAL
    media_playlist: list[str] = field(default_factory=list)  # 로컬 영상 경로 (순서대로 반복)
    cloud_profile: str = "default"  # settings["cloud"] 서버 (현재 1대)
    oauth_profile_id: str = ""  # youtube_accounts 프로필 (채널별 OAuth token)
    stream_key_store_id: str = ""  # 비우면 채널 ID (default → 기존 live_secret.dat)
    schedule_rules: list[str] = field(default_factory=list)
    enabled: bool = True

    @property
    def is_default(self) -> bool:
        return self.channel_profile_id == DEFAULT_LIVE_PROFILE

    @property
    def key_store_id(self) -> str:
        return self.stream_key_store_id or self.channel_profile_id

    def validate(self) -> "LiveChannelProfile":
        try:
            validate_live_profile_id(self.channel_profile_id)
            validate_live_profile_id(self.key_store_id)
        except CloudConfigError as e:
            raise ChannelError(str(e)) from None
        self.display_name = (self.display_name or "").strip()
        if not self.display_name:
            raise ChannelError("채널 이름을 입력하세요 (예: 시니어 채널).")
        if len(self.display_name) > 40:
            raise ChannelError("채널 이름은 40자 이하로 입력하세요.")
        if self.stream_mode not in STREAM_MODES:
            raise ChannelError("송출 방식이 올바르지 않습니다.")
        if not isinstance(self.media_playlist, list) or len(self.media_playlist) > MAX_PLAYLIST_ITEMS:
            raise ChannelError(f"채널 Playlist는 최대 {MAX_PLAYLIST_ITEMS}개입니다.")
        self.media_playlist = [str(p) for p in self.media_playlist]
        self.schedule_rules = [str(r) for r in (self.schedule_rules or [])]
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LiveChannelProfile":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


def default_channel() -> LiveChannelProfile:
    return LiveChannelProfile(DEFAULT_LIVE_PROFILE, DEFAULT_NAME)


def suggest_profile_id(display_name: str, taken) -> str:
    """이름에서 채널 ID 만들기 (영문이면 이름 기반, 한글 등은 ch_<임의>). 서버 경로/서비스 이름으로 쓰인다."""
    base = re.sub(r"[^a-z0-9]+", "_", (display_name or "").lower()).strip("_")[:20]
    if not base or not base[0].isalpha():
        base = "ch_" + secrets.token_hex(3) if not base else "ch_" + base
    base = base[:28]
    pid, n = base, 2
    while pid in taken or pid == DEFAULT_LIVE_PROFILE:
        pid, n = f"{base}_{n}", n + 1
    return validate_live_profile_id(pid)


class LiveChannelStore:
    """채널 Profile 목록 (settings.json "live_channels"). 기본 채널은 항상 있고 지울 수 없다."""

    def all(self) -> list[LiveChannelProfile]:
        rows = _settings.load_settings().get(SETTINGS_KEY)
        out: list[LiveChannelProfile] = []
        for d in rows if isinstance(rows, list) else []:
            if not isinstance(d, dict):
                continue
            try:
                p = LiveChannelProfile.from_dict(d).validate()
            except (ChannelError, TypeError):
                continue
            if all(x.channel_profile_id != p.channel_profile_id for x in out):
                out.append(p)
        if not any(p.is_default for p in out):
            out.insert(0, default_channel())
        out.sort(key=lambda p: not p.is_default)
        return out

    def get(self, profile_id: str) -> LiveChannelProfile | None:
        return next((p for p in self.all() if p.channel_profile_id == profile_id), None)

    def migrated(self) -> bool:
        return isinstance(_settings.load_settings().get(SETTINGS_KEY), list)

    def ensure_migrated(self) -> bool:
        """처음 채널을 추가할 때 1번: settings.json 원본을 백업한 뒤 기본 채널을 기록한다 (기존 값은 그대로).
        True = 이번에 migration 함."""
        with _settings.SETTINGS_LOCK:
            if self.migrated():
                return False
            src = _settings.settings_file()
            if src.is_file():
                backup = src.with_name(src.name + BACKUP_SUFFIX)
                if not backup.exists():
                    tmp = backup.with_name(backup.name + ".tmp")
                    shutil.copy2(src, tmp)
                    os.replace(tmp, backup)
            _settings.update_settings(**{SETTINGS_KEY: [default_channel().to_dict()]})
            return True

    def _write(self, profiles: list[LiveChannelProfile]) -> None:
        rows = [p.validate().to_dict() for p in profiles]
        for r in rows:
            if BANNED_KEYS & set(r):
                raise ValueError("secret must not be stored in settings.json")
        _settings.update_settings(**{SETTINGS_KEY: rows})

    def add(self, display_name: str, *, profile_id: str | None = None, **fields) -> LiveChannelProfile:
        self.ensure_migrated()
        cur = self.all()
        if len(cur) >= MAX_CHANNELS:
            raise ChannelError(f"채널은 최대 {MAX_CHANNELS}개까지 등록할 수 있습니다.")
        taken = {p.channel_profile_id for p in cur}
        pid = profile_id or suggest_profile_id(display_name, taken)
        if pid in taken:
            raise ChannelError("같은 ID의 채널이 이미 있습니다.")
        p = LiveChannelProfile(pid, display_name, **fields).validate()
        return self.save(p)

    def save(self, profile: LiveChannelProfile) -> LiveChannelProfile:
        self.ensure_migrated()
        profile.validate()
        cur = self.all()
        if any(p.display_name == profile.display_name and p.channel_profile_id != profile.channel_profile_id for p in cur):
            raise ChannelError(f"같은 이름의 채널이 이미 있습니다: {profile.display_name}")
        if profile.youtube_channel_id and any(
                p.youtube_channel_id == profile.youtube_channel_id and p.channel_profile_id != profile.channel_profile_id
                for p in cur):
            raise ChannelError("이 YouTube 채널은 이미 다른 채널 Profile에 연결되어 있습니다.")
        idx = next((i for i, p in enumerate(cur) if p.channel_profile_id == profile.channel_profile_id), None)
        if idx is None:
            cur.append(profile)
        else:
            cur[idx] = profile
        self._write(cur)
        return profile

    def delete(self, profile_id: str) -> None:
        """기본 채널은 지울 수 없다. 채널을 지우면 그 채널의 Stream Key 파일도 지운다 (다른 채널 key는 그대로)."""
        if profile_id == DEFAULT_LIVE_PROFILE:
            raise ChannelError("기본 채널은 지울 수 없습니다.")
        p = self.get(profile_id)
        if p is None:
            return
        key_store_for(p.key_store_id).clear()
        self._write([x for x in self.all() if x.channel_profile_id != profile_id])

    def selected_id(self) -> str:
        sel = _settings.load_settings().get(SELECTED_KEY)
        return sel if isinstance(sel, str) and self.get(sel) is not None else DEFAULT_LIVE_PROFILE

    def select(self, profile_id: str) -> None:
        if self.get(profile_id) is None:
            raise ChannelError("채널을 찾을 수 없습니다.")
        _settings.update_settings(**{SELECTED_KEY: profile_id})


# ---------------- Stream Key (채널별 DPAPI 파일) ----------------

_MEMORY_KEY_STORES: dict[str, object] = {}


def key_file_name(store_id: str) -> str:
    store_id = validate_live_profile_id(store_id)
    return live_secrets.SECRET_FILE_NAME if store_id == DEFAULT_LIVE_PROFILE else f"live_secret_{store_id}.dat"


def key_store_for(store_id: str, *, is_windows: bool | None = None):
    """채널 Stream Key 저장소. default = 기존 live_secret.dat (그대로), 그 외 = live_secret_<id>.dat."""
    store_id = validate_live_profile_id(store_id)
    is_windows = (os.name == "nt") if is_windows is None else is_windows
    if is_windows:
        if store_id == DEFAULT_LIVE_PROFILE:
            return live_secrets.default_key_store(is_windows=True)
        return live_secrets.WindowsDpapiStreamKeyStore(live_secrets.settings_dir() / key_file_name(store_id))
    if store_id not in _MEMORY_KEY_STORES:  # Windows 외: 디스크 저장 없음 (프로세스 메모리만)
        _MEMORY_KEY_STORES[store_id] = live_secrets.SessionStreamKeyStore()
    return _MEMORY_KEY_STORES[store_id]


# ---------------- YouTube 연결 (채널별 OAuth) ----------------

def oauth_profile_for(channel: LiveChannelProfile, profiles=None):
    """채널에 연결된 youtube_accounts 프로필 (없으면 None)."""
    if not channel.oauth_profile_id:
        return None
    from .youtube_accounts import ProfileStore
    return (profiles or ProfileStore()).get(channel.oauth_profile_id)


def oauth_connected(channel: LiveChannelProfile, profiles=None) -> bool:
    """채널 YouTube 연결 여부. 기본 채널은 채널 프로필이 없으면 기존 youtube_token.dat 연결을 본다."""
    from .youtube_accounts import ProfileStore
    profiles = profiles or ProfileStore()
    prof = oauth_profile_for(channel, profiles)
    if prof is not None:
        return profiles.is_connected(prof)
    if channel.is_default:
        from .youtube_config import is_connected
        return is_connected()
    return False


def channel_api(channel: LiveChannelProfile, profiles=None):
    """채널의 YouTube API client. 채널 프로필 token만 쓴다 (다른 채널 token을 쓰지 않음)."""
    from .youtube_accounts import ProfileStore, build_profile_api
    from .youtube_oauth import OAuthError
    profiles = profiles or ProfileStore()
    prof = oauth_profile_for(channel, profiles)
    if prof is not None:
        return build_profile_api(prof, profiles.token_store(prof.profile_id))
    if channel.is_default:
        from .youtube_config import build_api_client
        return build_api_client()
    raise OAuthError(f"'{channel.display_name}' 채널의 YouTube 연결이 없습니다. [채널 관리]에서 Google 계정을 연결하세요.",
                     "invalid_grant")


def migrate_legacy_youtube_token(store: LiveChannelStore | None = None, profiles=None) -> str:
    """기존 youtube_token.dat(LIVE 자동 세션 연결)이 있으면 기본 채널의 채널 프로필로 복사한다.
    원본 token/settings["youtube"]는 지우거나 바꾸지 않는다. 결과: 만든 프로필 ID ("" = 할 일 없음)."""
    from .youtube_accounts import ChannelProfile, ProfileStore, new_profile_id
    from .youtube_config import load_youtube_settings
    from .youtube_oauth import YouTubeAuthStore
    store = store or LiveChannelStore()
    profiles = profiles or ProfileStore()
    default = store.get(DEFAULT_LIVE_PROFILE) or default_channel()
    if default.oauth_profile_id and profiles.get(default.oauth_profile_id) is not None:
        return ""
    legacy = YouTubeAuthStore()
    ys = load_youtube_settings()
    if not (legacy.persistent and legacy.has_saved() and ys.get("client_file") and ys.get("channel_id")):
        return ""
    if any(p.channel_id == ys["channel_id"] for p in profiles.all()):
        return ""  # 이미 다른 프로필로 연결된 채널: 자동으로 섞지 않는다
    prof = ChannelProfile(profile_id=new_profile_id(), alias=f"{DEFAULT_NAME} (기존 연결)",
                          channel_id=str(ys["channel_id"]), channel_title=str(ys.get("channel_title") or ""),
                          client_file=str(ys["client_file"]), stream_id=str(ys.get("stream_id") or ""))
    target = profiles.token_store(prof.profile_id)
    tmp = target.path.with_name(target.path.name + ".tmp")
    shutil.copy2(legacy.path, tmp)  # 복사 (원본 유지). DPAPI blob 그대로 = 같은 Windows 계정에서만 풀림
    os.replace(tmp, target.path)
    profiles.save(prof)
    default.oauth_profile_id = prof.profile_id
    default.youtube_channel_id = default.youtube_channel_id or prof.channel_id
    default.stream_mode = STREAM_MODE_API if ys.get("stream_mode") == "API" else default.stream_mode
    store.save(default)
    return prof.profile_id


# ---------------- 동시 송출 계산 ----------------

def parse_kbps(value) -> float | None:
    """FFmpeg `8123.4kbits/s` / 숫자(kbps) → kbps."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    v = str(value).strip()
    if v.endswith("kbits/s"):
        v = v[:-7]
    try:
        return float(v)
    except ValueError:
        return None


def playlist_kbps(reports) -> float:
    """LIVE READY 분석 결과로 송출 비트레이트 추정 (가장 높은 항목 기준, 영상+오디오)."""
    best = 0.0
    for r in reports or []:
        if r is None:
            continue
        v = float(getattr(r, "video_kbps", 0) or 0)
        a = float(getattr(r, "audio_kbps", 0) or 0) or DEFAULT_AUDIO_KBPS
        best = max(best, v + a)
    return best


@dataclass(frozen=True)
class BandwidthEstimate:
    total_mbps: float
    warn: bool

    @property
    def text(self) -> str:
        t = f"예상 Cloud 송출 대역폭: 약 {self.total_mbps:.1f} Mbps"
        if self.warn:
            t += (f"\n⚠ 두 채널 합계가 {BANDWIDTH_WARN_MBPS:.0f} Mbps를 넘습니다. "
                  "무료 서버 네트워크 한도를 Oracle Console에서 확인하고 영상 비트레이트를 낮추는 것을 권장합니다.")
        return t


def estimate_bandwidth(kbps_values) -> BandwidthEstimate:
    total = sum(v for v in (parse_kbps(x) for x in kbps_values) if v) * (1 + STREAM_OVERHEAD) / 1000
    return BandwidthEstimate(round(total, 1), total > BANDWIDTH_WARN_MBPS)


def concurrency_problem(live_count: int, *, starting_is_live: bool = False, max_live: int = MAX_CONCURRENT_LIVE) -> str:
    """PC 쪽 사전 확인 (서버 worker도 slot 잠금으로 한 번 더 막는다)."""
    from .cloud_model import CONCURRENT_BUSY
    if not starting_is_live and live_count >= max_live:
        return CONCURRENT_BUSY
    return ""


def format_live_line(name: str, seconds: float | None, live: bool) -> str:
    from .core import format_duration
    if not live:
        return f"{name}\n○ 대기"
    return f"{name}\n● LIVE\n{format_duration(seconds) if seconds else '00:00:00'}"
