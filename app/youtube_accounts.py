"""다채널 Channel Profile (예약 업로드) — 프로필마다 OAuth token을 따로 DPAPI 파일에 저장한다.

settings.json의 "channel_profiles"에는 비밀이 아닌 값만 저장한다:
  profile_id, alias, channel_id, channel_title, language, timezone, category_id, privacy, made_for_kids,
  client_file(= OAuth Client JSON 파일 위치만, 내용/secret 복사 없음)
refresh token → youtube_token_<profile_id>.dat (Windows DPAPI). 프로필끼리 token 파일을 공유하지 않는다.

업로드 직전에는 반드시 verify_channel(channels.list mine=true)로 실제 채널 ID를 다시 확인하고,
프로필에 저장된 채널 ID와 다르면 업로드를 차단한다 (한국 채널 영상이 일본 채널에 올라가는 사고 방지).
"""
from __future__ import annotations

import re
import secrets
import time
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .settings import load_settings, update_settings
from .youtube_api import YouTubeApiClient, YouTubeApiError, YouTubeChannelInfo
from .youtube_metadata import DEFAULT_CATEGORY_ID, LANGUAGES, PRIVACY_LABELS
from .youtube_oauth import (
    YOUTUBE_SCOPE, OAuthClient, OAuthError, OAuthSession, YouTubeAuthStore, authorize, load_client_file, urllib_transport,
)
from .youtube_schedule import get_zone

SETTINGS_KEY = "channel_profiles"
PROFILE_ID_RE = re.compile(r"^[a-z0-9]{6,32}$")
BANNED_KEYS = {"refresh_token", "access_token", "client_secret", "stream_name", "stream_key", "token"}


class ProfileError(ValueError):
    pass


class ChannelMismatchError(YouTubeApiError):
    """연결된 Google 계정의 실제 채널이 프로필의 채널과 다르다 → 업로드 차단 (재시도하지 않음)."""

    def __init__(self, expected: str, actual: str, actual_title: str = ""):
        super().__init__(f"채널 불일치로 업로드를 차단했습니다. 프로필 채널 {expected} ≠ 연결된 채널 {actual}"
                         + (f" ({actual_title})" if actual_title else "") + ". [채널 관리]에서 계정을 다시 연결하세요.",
                         kind="config", reason="channelMismatch")
        self.expected = expected
        self.actual = actual


def new_profile_id() -> str:
    return secrets.token_hex(6)


@dataclass
class ChannelProfile:
    profile_id: str
    alias: str
    channel_id: str = ""
    channel_title: str = ""
    language: str = "ko"
    timezone: str = "Asia/Seoul"
    category_id: str = DEFAULT_CATEGORY_ID
    privacy: str = "private"
    made_for_kids: bool = False
    client_file: str = ""  # OAuth secret reference: 파일 위치만
    default_template_id: str = ""  # 예약 업로드 기본 메타데이터 템플릿 (youtube_batch.UploadTemplateStore)

    def validate(self) -> "ChannelProfile":
        if not PROFILE_ID_RE.match(self.profile_id or ""):
            raise ProfileError("프로필 ID가 올바르지 않습니다.")
        self.alias = (self.alias or "").strip()
        if not self.alias:
            raise ProfileError("채널 별칭을 입력하세요 (예: 🇰🇷 한국 시니어).")
        if len(self.alias) > 60:
            raise ProfileError("채널 별칭은 60자 이하로 입력하세요.")
        if self.language not in LANGUAGES:
            raise ProfileError("언어가 올바르지 않습니다.")
        try:
            get_zone(self.timezone)
        except ValueError as e:
            raise ProfileError(str(e)) from None
        if not str(self.category_id).isdigit():
            raise ProfileError("카테고리가 올바르지 않습니다.")
        if self.privacy not in PRIVACY_LABELS:
            raise ProfileError("공개 상태가 올바르지 않습니다.")
        return self

    @property
    def label(self) -> str:
        return f"{self.alias} · {self.channel_title}" if self.channel_title else f"{self.alias} (연결 안 됨)"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ChannelProfile":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


def token_store_for(profile_id: str, *, is_windows: bool | None = None) -> YouTubeAuthStore:
    """프로필 전용 token 파일 (youtube_token_<id>.dat). LIVE 자동 세션의 youtube_token.dat와도 분리."""
    if not PROFILE_ID_RE.match(profile_id or ""):
        raise ProfileError("프로필 ID가 올바르지 않습니다.")
    from .settings import settings_dir
    return YouTubeAuthStore(settings_dir() / f"youtube_token_{profile_id}.dat", is_windows=is_windows)


class ProfileStore:
    """Channel Profile 목록 (settings.json). 비밀 키는 거부한다. token store는 store_factory로 만든다 (테스트 교체용)."""

    def __init__(self, *, store_factory: Callable[[str], YouTubeAuthStore] = token_store_for):
        self.store_factory = store_factory
        self._memory_stores: dict[str, YouTubeAuthStore] = {}

    def token_store(self, profile_id: str) -> YouTubeAuthStore:
        # 메모리 저장(Windows 외)일 때도 같은 프로필은 같은 store 객체를 써야 token이 유지된다.
        if profile_id not in self._memory_stores:
            self._memory_stores[profile_id] = self.store_factory(profile_id)
        return self._memory_stores[profile_id]

    def all(self) -> list[ChannelProfile]:
        out = []
        for d in load_settings().get(SETTINGS_KEY) or []:
            if isinstance(d, dict):
                try:
                    out.append(ChannelProfile.from_dict(d).validate())
                except (ProfileError, TypeError):
                    continue
        return out

    def get(self, profile_id: str) -> ChannelProfile | None:
        return next((p for p in self.all() if p.profile_id == profile_id), None)

    def _write(self, profiles: list[ChannelProfile]) -> None:
        rows = [p.to_dict() for p in profiles]
        for r in rows:
            if BANNED_KEYS & set(r):
                raise ValueError("secret must not be stored in settings.json")
        update_settings(**{SETTINGS_KEY: rows})  # 업로드 스레드의 저장과 섞이지 않게 잠금 안에서

    def add(self, profile: ChannelProfile) -> ChannelProfile:
        """새 프로필. 같은 profile_id가 이미 있으면 거부 (수정은 save)."""
        if self.get(profile.profile_id) is not None:
            raise ProfileError("같은 ID의 채널 프로필이 이미 있습니다.")
        return self.save(profile)

    def save(self, profile: ChannelProfile) -> ChannelProfile:
        profile.validate()
        others = [p for p in self.all() if p.profile_id != profile.profile_id]
        if any(p.alias == profile.alias for p in others):
            raise ProfileError(f"같은 별칭의 채널 프로필이 이미 있습니다: {profile.alias}")
        dup = next((p for p in others if profile.channel_id and p.channel_id == profile.channel_id), None)
        if dup is not None:
            raise ProfileError(f"이 YouTube 채널은 이미 '{dup.alias}' 프로필에 연결되어 있습니다.")
        cur = self.all()
        idx = next((i for i, p in enumerate(cur) if p.profile_id == profile.profile_id), None)
        if idx is None:
            cur.append(profile)
        else:
            cur[idx] = profile
        self._write(cur)
        return profile

    def delete(self, profile_id: str) -> None:
        """token 정리 정책: 프로필을 지우면 그 프로필의 token 파일(.dat/.tmp)도 즉시 지운다.
        OAuth Client JSON 파일은 사용자 파일이므로 지우지 않는다 (위치 기록만 사라짐)."""
        self.token_store(profile_id).clear()
        self._memory_stores.pop(profile_id, None)
        self._write([p for p in self.all() if p.profile_id != profile_id])

    def is_connected(self, profile: ChannelProfile) -> bool:
        return bool(profile.client_file and profile.channel_id and self.token_store(profile.profile_id).has_saved())


class ProfileOAuthSession(OAuthSession):
    """다른 OAuth Client로 받은 token은 쓰지 않는다 (프로필 간 섞임 방지)."""

    _client_checked = False

    def __call__(self, force_refresh: bool = False) -> str:
        if not self._client_checked:
            saved = self.store.load()
            if saved and saved.get("client_id") and saved["client_id"] != self.client.client_id:
                raise OAuthError("이 채널 프로필의 저장된 연결이 다른 OAuth Client로 만들어졌습니다. 계정을 다시 연결하세요.",
                                 "invalid_grant")
            self._client_checked = True
        return super().__call__(force_refresh)


def build_profile_api(profile: ChannelProfile, store: YouTubeAuthStore, *, transport=urllib_transport,
                      base_url: str | None = None, client: OAuthClient | None = None, sleep=time.sleep) -> YouTubeApiClient:
    if not store.has_saved():
        raise OAuthError(f"'{profile.alias}' 채널이 연결되어 있지 않습니다. [채널 관리]에서 Google 계정을 연결하세요.",
                         "invalid_grant")
    client = client or load_client_file(profile.client_file)
    session = ProfileOAuthSession(client, store, transport=transport)
    kw = {"base_url": base_url} if base_url else {}
    return YouTubeApiClient(session, transport=transport, sleep=sleep, **kw)


def verify_channel(api: YouTubeApiClient, expected_channel_id: str) -> YouTubeChannelInfo:
    """channels.list(mine=true)로 실제 채널 확인. 기대값이 없거나 다르면 ChannelMismatchError."""
    actual = api.get_channel()
    if not expected_channel_id or actual.id != expected_channel_id:
        raise ChannelMismatchError(expected_channel_id or "(없음)", actual.id, actual.title)
    return actual


def connect_profile(profiles: ProfileStore, profile: ChannelProfile, client_file: str, *,
                    open_browser: Callable[[str], object] = webbrowser.open, transport=urllib_transport,
                    client: OAuthClient | None = None, api_base: str | None = None,
                    timeout: float = 300.0, scope: str = YOUTUBE_SCOPE) -> ChannelProfile:
    """이 프로필 전용으로 브라우저 로그인/동의 → token은 이 프로필 파일에만 DPAPI 저장 → 채널 ID/이름 기록.

    이미 다른 채널로 연결된 프로필에 다른 채널 계정을 연결하면 차단한다 (실수로 채널이 바뀌는 것 방지).
    """
    profile.validate()
    client = client or load_client_file(client_file)
    tok = authorize(client, open_browser=open_browser, transport=transport, timeout=timeout, scope=scope)
    kw = {"base_url": api_base} if api_base else {}
    probe = OAuthSession(client, YouTubeAuthStore(Path("unused"), is_windows=False), transport=transport)
    probe._token = tok  # 방금 받은 access token으로 채널만 확인 (아직 저장하지 않음)
    channel = YouTubeApiClient(probe, transport=transport, **kw).get_channel()
    if profile.channel_id and profile.channel_id != channel.id:
        raise ChannelMismatchError(profile.channel_id, channel.id, channel.title)
    for other in profiles.all():
        if other.profile_id != profile.profile_id and other.channel_id == channel.id:
            raise ProfileError(f"이 YouTube 채널은 이미 '{other.alias}' 프로필에 연결되어 있습니다.")
    profiles.token_store(profile.profile_id).save(tok.refresh_token, client_id=client.client_id, scope=tok.scope)
    profile.client_file = str(client_file)
    profile.channel_id, profile.channel_title = channel.id, channel.title
    return profiles.save(profile)


def disconnect_profile(profiles: ProfileStore, profile: ChannelProfile) -> ChannelProfile:
    profiles.token_store(profile.profile_id).clear()
    profile.channel_id = profile.channel_title = ""
    return profiles.save(profile)
