"""YouTube 자동 세션 설정/연결/실행기 (Tk 비의존).

settings.json의 "youtube"에는 비밀이 아닌 값만 저장한다:
  client_file(경로만), channel_id, channel_title, stream_id, stream_mode, template(제목/설명/공개 상태/아동용/제목 규칙)
refresh token은 YouTubeAuthStore(DPAPI), Stream Key(streamName)는 저장하지 않고 필요할 때 API로 받는다.

Phase 3B 구조:
  Windows: OAuth 연결 / 설정 / 관리 UI + (이번 단계) 교체 실행기(RolloverRunner)
  Cloud:   교체 실행기를 서버에서 돌리려면 장기 credential이 서버에 필요하다 → 별도 Gate (이번 commit에서 배포하지 않음)
  ⇒ 이번 단계의 자동 교체는 PC 프로그램이 켜져 있을 때 동작한다 (화면/문서에 명시).
"""
from __future__ import annotations

import queue
import threading
import time
import webbrowser
from typing import Callable

from .settings import load_settings, save_settings
from .youtube_api import YouTubeApiClient, YouTubeApiError, YouTubeChannelInfo
from .youtube_oauth import OAuthClient, OAuthError, OAuthSession, YouTubeAuthStore, authorize, load_client_file, urllib_transport
from .youtube_session import (
    STREAM_MODE_API, STREAM_MODE_MANUAL, TITLE_RULE_SAME, BroadcastTemplate, RolloverSnapshot, YouTubeRolloverManager,
)

SETTINGS_KEY = "youtube"
GOOGLE_API_LIBRARY_URL = "https://console.cloud.google.com/apis/library/youtube.googleapis.com"
GOOGLE_CREDENTIALS_URL = "https://console.cloud.google.com/apis/credentials"
GOOGLE_CONSENT_URL = "https://console.cloud.google.com/apis/credentials/consent"


def load_youtube_settings() -> dict:
    d = load_settings().get(SETTINGS_KEY)
    return dict(d) if isinstance(d, dict) else {}


def save_youtube_settings(**updates) -> dict:
    """비밀이 아닌 값만 저장. token/secret/streamName 같은 키는 거부한다."""
    banned = {"refresh_token", "access_token", "client_secret", "stream_name", "stream_key", "token"}
    if banned & set(updates):
        raise ValueError("secret must not be stored in settings.json")
    data = load_settings()
    cur = dict(data.get(SETTINGS_KEY) or {})
    cur.update({k: v for k, v in updates.items() if v is not None})
    data[SETTINGS_KEY] = cur
    save_settings(data)
    return cur


def clear_youtube_connection(store: YouTubeAuthStore | None = None) -> None:
    (store or YouTubeAuthStore()).clear()
    data = load_settings()
    cur = dict(data.get(SETTINGS_KEY) or {})
    for k in ("channel_id", "channel_title", "stream_id"):
        cur.pop(k, None)
    data[SETTINGS_KEY] = cur
    save_settings(data)


def template_from_settings() -> BroadcastTemplate:
    t = load_youtube_settings().get("template") or {}
    return BroadcastTemplate(title=str(t.get("title") or "24H LIVE"), description=str(t.get("description") or ""),
                             privacy=str(t.get("privacy") or "unlisted"), made_for_kids=bool(t.get("made_for_kids")),
                             title_rule=str(t.get("title_rule") or TITLE_RULE_SAME))


def stream_mode() -> str:
    m = load_youtube_settings().get("stream_mode")
    return m if m in (STREAM_MODE_MANUAL, STREAM_MODE_API) else STREAM_MODE_MANUAL


def is_connected(store: YouTubeAuthStore | None = None) -> bool:
    s = load_youtube_settings()
    return bool(s.get("client_file") and s.get("channel_id") and (store or YouTubeAuthStore()).has_saved())


def build_api_client(*, store: YouTubeAuthStore | None = None, transport=urllib_transport, base_url: str | None = None,
                     client: OAuthClient | None = None, sleep=time.sleep) -> YouTubeApiClient:
    s = load_youtube_settings()
    client = client or load_client_file(s.get("client_file", ""))
    session = OAuthSession(client, store or YouTubeAuthStore(), transport=transport)
    kw = {"base_url": base_url} if base_url else {}
    return YouTubeApiClient(session, transport=transport, sleep=sleep, **kw)


def connect_account(client_file: str, *, store: YouTubeAuthStore | None = None,
                    open_browser: Callable[[str], object] = webbrowser.open, transport=urllib_transport,
                    client: OAuthClient | None = None, api_base: str | None = None, timeout: float = 300.0) -> YouTubeChannelInfo:
    """브라우저 로그인/동의 → refresh token DPAPI 저장 → 채널 확인 → 설정(비밀 아님) 저장."""
    store = store or YouTubeAuthStore()
    client = client or load_client_file(client_file)
    tok = authorize(client, open_browser=open_browser, transport=transport, timeout=timeout)
    store.save(tok.refresh_token, client_id=client.client_id, scope=tok.scope)
    session = OAuthSession(client, store, transport=transport)
    session._token = tok  # 방금 받은 access token 재사용
    kw = {"base_url": api_base} if api_base else {}
    channel = YouTubeApiClient(session, transport=transport, **kw).get_channel()
    save_youtube_settings(client_file=str(client_file), channel_id=channel.id, channel_title=channel.title)
    return channel


class RolloverRunner:
    """manager.tick()을 백그라운드에서 주기적으로 호출하고, snapshot을 큐로만 전달한다 (Tk 객체를 참조하지 않음)."""

    def __init__(self, manager: YouTubeRolloverManager, *, interval: float = 10.0):
        self.manager = manager
        self.interval = interval
        self.events: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        m, q, stop, interval = self.manager, self.events, self._stop, self.interval

        def loop():
            q.put(("yt", m.snapshot()))
            while not stop.wait(interval):
                try:
                    m.tick()
                except (YouTubeApiError, OAuthError) as e:  # 예상 밖 오류도 송출을 멈추지 않는다
                    m.last_error = str(e)
                except Exception as e:
                    m.last_error = f"YouTube 자동 교체 오류 ({type(e).__name__})"
                q.put(("yt", m.snapshot()))
        self._thread = threading.Thread(target=loop, name="youtube-rollover", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def drain(self) -> list[RolloverSnapshot]:
        out = []
        while True:
            try:
                out.append(self.events.get_nowait()[1])
            except queue.Empty:
                return out
