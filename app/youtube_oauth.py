"""YouTube API용 Google OAuth 2.0 (Desktop app) — Python 표준 라이브러리만 사용.

- 시스템 브라우저 + 127.0.0.1 임의 포트 loopback으로 인증 코드를 받는다 (OOB copy/paste 방식은 Google이 지원 종료).
- PKCE(S256) + state(CSRF) 검사.
- 사용자의 Google 비밀번호는 프로그램이 받지 않는다 (브라우저에서 Google이 직접 처리).
- refresh token은 Windows DPAPI 암호화 파일(youtube_token.dat)에만 저장. settings.json/로그/repr에 남기지 않는다.
- OAuth client JSON(client_secret 포함)은 사용자가 지정한 파일 경로만 기억한다 (저장소 커밋 금지: .gitignore).

주의 (화면/문서에 표시): OAuth 동의 화면이 외부(External) + 게시 상태 "Testing"이면
YouTube 같은 scope의 refresh token은 7일 후 만료된다. 장기 자동 운영 전 게시 상태를 확인해야 한다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube"  # LIVE 생성/바인드/전환에 필요한 최소 scope
MASK = "********"
TESTING_TOKEN_WARNING = ("Google OAuth 앱이 'Testing'(테스트) 상태이면 YouTube 연결은 7일 후 만료될 수 있습니다.\n"
                         "장기 자동 운영 전에 Google Cloud Console에서 OAuth 앱 게시 상태를 확인하세요.")


class OAuthError(RuntimeError):
    """kind: config | invalid_grant | denied | csrf | timeout | network | server"""

    def __init__(self, message: str, kind: str = "config"):
        super().__init__(message)
        self.kind = kind


# ---------------- HTTP transport (테스트에서 교체 가능) ----------------

class TransportError(RuntimeError):
    pass


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TransportError(type(e).__name__) from None


# ---------------- client file ----------------

@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str = field(repr=False)
    auth_uri: str = AUTH_URI
    token_uri: str = TOKEN_URI

    def __repr__(self) -> str:
        return f"OAuthClient(client_id={self.client_id[:12]}…, client_secret={MASK})"


def load_client_file(path) -> OAuthClient:
    """Google Cloud Console에서 받은 'Desktop app' OAuth client JSON."""
    p = Path(str(path).strip().strip('"'))
    if not p.is_file():
        raise OAuthError("OAuth Client JSON 파일을 찾을 수 없습니다.")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise OAuthError("OAuth Client JSON 파일을 읽을 수 없습니다.") from None
    if "web" in data and "installed" not in data:
        raise OAuthError("웹 애플리케이션용 Client입니다. Google Cloud에서 '데스크톱 앱' 유형으로 OAuth Client를 만들어 주세요.")
    inst = data.get("installed")
    if not isinstance(inst, dict) or not inst.get("client_id") or not inst.get("client_secret"):
        raise OAuthError("올바른 OAuth Client JSON이 아닙니다 (데스크톱 앱 client_id/client_secret 필요).")
    auth_uri = inst.get("auth_uri") or AUTH_URI
    token_uri = inst.get("token_uri") or TOKEN_URI
    for u in (auth_uri, token_uri):
        host = urllib.parse.urlsplit(str(u)).hostname or ""
        if not str(u).startswith("https://") or not host.endswith((".google.com", ".googleapis.com")):
            raise OAuthError("OAuth Client JSON의 주소가 Google 주소가 아닙니다.")
    return OAuthClient(str(inst["client_id"]), str(inst["client_secret"]), auth_uri, token_uri)


# ---------------- PKCE / URL ----------------

def make_pkce() -> tuple[str, str]:
    """(code_verifier, code_challenge S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")  # 64자
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_auth_url(client: OAuthClient, *, redirect_uri: str, state: str, code_challenge: str,
                   scope: str = YOUTUBE_SCOPE) -> str:
    q = {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",  # refresh token
        "prompt": "consent",
    }
    return client.auth_uri + "?" + urllib.parse.urlencode(q)


# ---------------- loopback receiver ----------------

_DONE_HTML = ("<html><head><meta charset='utf-8'><title>연결 완료</title></head><body style='font-family:sans-serif'>"
              "<h2>YouTube 연결 단계가 끝났습니다.</h2><p>이 창을 닫고 프로그램으로 돌아가세요.</p></body></html>").encode("utf-8")


class LoopbackReceiver:
    """127.0.0.1 임의 포트에서 Google의 redirect 1건을 받는다. state가 다르면 거부(CSRF)."""

    def __init__(self, state: str):
        self.expected_state = state
        self.code: str | None = None
        self.error: OAuthError | None = None
        self._done = threading.Event()
        receiver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parts = urllib.parse.urlsplit(self.path)
                if parts.path != "/":
                    self.send_response(404)
                    self.end_headers()
                    return
                qs = urllib.parse.parse_qs(parts.query)
                state = (qs.get("state") or [""])[0]
                if not hmac.compare_digest(state, receiver.expected_state):
                    receiver.error = OAuthError("보안 확인(state)이 맞지 않아 연결을 거부했습니다. 다시 시도하세요.", "csrf")
                elif qs.get("error"):
                    receiver.error = OAuthError("Google 계정 연결이 취소되었거나 거부되었습니다.", "denied")
                elif qs.get("code"):
                    receiver.code = qs["code"][0]
                else:
                    receiver.error = OAuthError("Google 응답에 인증 코드가 없습니다.", "server")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_DONE_HTML)
                receiver._done.set()

            def log_message(self, *a):  # 코드/state를 콘솔 로그에 남기지 않음
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.server.timeout = 0.5
        self.port = self.server.server_address[1]
        self.redirect_uri = f"http://127.0.0.1:{self.port}"

    def wait(self, timeout: float = 300.0) -> str:
        end = time.monotonic() + timeout
        try:
            while not self._done.is_set() and time.monotonic() < end:
                self.server.handle_request()
        finally:
            self.server.server_close()
        if self.error:
            raise self.error
        if not self.code:
            raise OAuthError("시간 안에 Google 계정 연결이 끝나지 않았습니다. 다시 시도하세요.", "timeout")
        return self.code


# ---------------- tokens ----------------

@dataclass
class TokenSet:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: float = 0.0
    scope: str = ""

    def __repr__(self) -> str:
        return f"TokenSet(access_token={MASK}, refresh_token={MASK if self.refresh_token else None}, scope={self.scope!r})"


def _token_request(client: OAuthClient, form: dict, transport, timeout: float = 30.0) -> dict:
    body = urllib.parse.urlencode(form).encode("ascii")
    try:
        status, raw = transport("POST", client.token_uri, {"Content-Type": "application/x-www-form-urlencoded"}, body, timeout)
    except TransportError:
        raise OAuthError("Google 서버에 연결할 수 없습니다. 인터넷 연결을 확인하세요.", "network") from None
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        data = {}
    if status != 200:
        err = data.get("error", "")
        if err == "invalid_grant":
            raise OAuthError("YouTube 연결이 만료되었거나 취소되었습니다. [YouTube 자동 세션 연결]을 다시 진행하세요.\n"
                             + TESTING_TOKEN_WARNING, "invalid_grant")
        if err in ("invalid_client", "unauthorized_client"):
            raise OAuthError("OAuth Client 설정이 올바르지 않습니다 (Client JSON 확인).", "config")
        if status >= 500:
            raise OAuthError("Google 서버 오류입니다. 잠시 후 다시 시도하세요.", "server")
        raise OAuthError(f"Google 인증 오류 ({err or status}).", "config")
    if not data.get("access_token"):
        raise OAuthError("Google 응답에 access token이 없습니다.", "server")
    return data


def exchange_code(client: OAuthClient, *, code: str, code_verifier: str, redirect_uri: str,
                  transport=urllib_transport, clock=time.time) -> TokenSet:
    data = _token_request(client, {
        "client_id": client.client_id, "client_secret": client.client_secret, "code": code,
        "code_verifier": code_verifier, "grant_type": "authorization_code", "redirect_uri": redirect_uri,
    }, transport)
    if not data.get("refresh_token"):
        raise OAuthError("Google이 장기 연결 토큰(refresh token)을 주지 않았습니다. 다시 연결해 주세요.", "server")
    return TokenSet(data["access_token"], data["refresh_token"], clock() + float(data.get("expires_in", 3600)),
                    str(data.get("scope", "")))


def refresh_access_token(client: OAuthClient, refresh_token: str, *, transport=urllib_transport,
                         clock=time.time) -> TokenSet:
    data = _token_request(client, {
        "client_id": client.client_id, "client_secret": client.client_secret,
        "refresh_token": refresh_token, "grant_type": "refresh_token",
    }, transport)
    return TokenSet(data["access_token"], data.get("refresh_token") or refresh_token,
                    clock() + float(data.get("expires_in", 3600)), str(data.get("scope", "")))


def authorize(client: OAuthClient, *, open_browser: Callable[[str], object] = webbrowser.open,
              transport=urllib_transport, timeout: float = 300.0, scope: str = YOUTUBE_SCOPE) -> TokenSet:
    """브라우저 열기 → 사용자 로그인/동의 → loopback으로 코드 수신 → 토큰 교환."""
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(24)
    receiver = LoopbackReceiver(state)
    open_browser(build_auth_url(client, redirect_uri=receiver.redirect_uri, state=state,
                                code_challenge=challenge, scope=scope))
    code = receiver.wait(timeout)
    return exchange_code(client, code=code, code_verifier=verifier, redirect_uri=receiver.redirect_uri,
                         transport=transport)


# ---------------- 저장 (DPAPI) ----------------

class YouTubeAuthStore:
    """refresh token 저장소. Windows: DPAPI 암호화 파일만 / 그 외: 메모리 (디스크 평문 저장 없음)."""

    FILE_NAME = "youtube_token.dat"

    def __init__(self, path: Path | None = None, *, is_windows: bool | None = None):
        from .settings import settings_dir
        self.is_windows = (os.name == "nt") if is_windows is None else is_windows
        self.path = Path(path) if path else settings_dir() / self.FILE_NAME
        self._memory: dict | None = None

    def __repr__(self) -> str:
        return f"YouTubeAuthStore(persistent={self.is_windows}, saved={self.has_saved()})"

    @property
    def persistent(self) -> bool:
        return self.is_windows

    def has_saved(self) -> bool:
        return self.path.is_file() if self.is_windows else self._memory is not None

    def load(self) -> dict | None:
        if not self.is_windows:
            return dict(self._memory) if self._memory else None
        if not self.path.is_file():
            return None
        from .live_secrets import DpapiError, dpapi_unprotect
        try:
            data = json.loads(dpapi_unprotect(self.path.read_bytes()).decode("utf-8"))
        except (OSError, ValueError, DpapiError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) and data.get("refresh_token") else None

    def save(self, refresh_token: str, *, client_id: str, scope: str = "") -> None:
        data = {"refresh_token": refresh_token, "client_id": client_id, "scope": scope, "saved_at": time.time()}
        if not self.is_windows:
            self._memory = data
            return
        from .live_secrets import dpapi_protect
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_bytes(dpapi_protect(json.dumps(data).encode("utf-8")))
        os.replace(tmp, self.path)

    def clear(self) -> None:
        self._memory = None
        for p in (self.path, self.path.with_name(self.path.name + ".tmp")):
            try:
                p.unlink()
            except FileNotFoundError:
                pass


class OAuthSession:
    """access token 캐시 + 자동 refresh. YouTubeApiClient의 token_provider로 쓴다."""

    MARGIN = 60.0

    def __init__(self, client: OAuthClient, store: YouTubeAuthStore, *, transport=urllib_transport, clock=time.time):
        self.client = client
        self.store = store
        self.transport = transport
        self.clock = clock
        self._token: TokenSet | None = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"OAuthSession({self.client!r})"

    def __call__(self, force_refresh: bool = False) -> str:
        with self._lock:
            if not force_refresh and self._token and self._token.expires_at - self.MARGIN > self.clock():
                return self._token.access_token
            saved = self.store.load()
            if not saved:
                raise OAuthError("YouTube가 연결되어 있지 않습니다. [YouTube 자동 세션 연결]을 진행하세요.", "invalid_grant")
            tok = refresh_access_token(self.client, saved["refresh_token"], transport=self.transport, clock=self.clock)
            if tok.refresh_token and tok.refresh_token != saved["refresh_token"]:
                self.store.save(tok.refresh_token, client_id=self.client.client_id, scope=tok.scope)
            self._token = tok
            return tok.access_token
