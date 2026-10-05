"""Gate 1: Google OAuth (Desktop, loopback + PKCE + state) — 실제 Google 접속 없음."""
import base64
import hashlib
import json
import logging
import os
import threading
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from app.youtube_oauth import (
    TESTING_TOKEN_WARNING, YOUTUBE_SCOPE, LoopbackReceiver, OAuthClient, OAuthError, OAuthSession, TransportError,
    YouTubeAuthStore, authorize, build_auth_url, exchange_code, load_client_file, make_pkce, refresh_access_token,
    urllib_transport,
)
from youtube_fakes import FAKE_REFRESH, FakeYouTube

FAKE_SECRET = "GOCSPX-fake-client-secret-0000"


def client_file(tmp_path, kind="installed", **override):
    inst = {"client_id": "123-fake.apps.googleusercontent.com", "client_secret": FAKE_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"], **override}
    p = tmp_path / "client_secret_test.json"
    p.write_text(json.dumps({kind: inst}), encoding="utf-8")
    return p


@pytest.fixture
def fake():
    f = FakeYouTube()
    yield f
    f.close()


def fake_client(fake):
    return OAuthClient("123-fake.apps.googleusercontent.com", FAKE_SECRET, token_uri=fake.token_uri)


def test_load_client_file_desktop_ok_and_repr_hides_secret(tmp_path):
    c = load_client_file(client_file(tmp_path))
    assert c.client_id.startswith("123-fake") and c.client_secret == FAKE_SECRET
    assert FAKE_SECRET not in repr(c) and FAKE_SECRET not in str(c)


@pytest.mark.parametrize("kind,override,msg", [
    ("web", {}, "데스크톱 앱"),
    ("installed", {"client_secret": ""}, "올바른"),
    ("installed", {"token_uri": "https://evil.example.com/token"}, "Google 주소"),
    ("installed", {"auth_uri": "http://accounts.google.com/x"}, "Google 주소"),
])
def test_load_client_file_rejects(tmp_path, kind, override, msg):
    with pytest.raises(OAuthError, match=msg):
        load_client_file(client_file(tmp_path, kind, **override))
    with pytest.raises(OAuthError):
        load_client_file(tmp_path / "missing.json")


def test_pkce_s256():
    v, c = make_pkce()
    assert 43 <= len(v) <= 128 and "=" not in v and "=" not in c
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert make_pkce()[0] != v


def test_auth_url_parameters():
    c = OAuthClient("cid", FAKE_SECRET)
    url = build_auth_url(c, redirect_uri="http://127.0.0.1:53111", state="st8", code_challenge="chal")
    parts = urllib.parse.urlsplit(url)
    q = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
    assert parts.scheme == "https" and parts.hostname == "accounts.google.com"
    assert q == {"client_id": "cid", "redirect_uri": "http://127.0.0.1:53111", "response_type": "code",
                 "scope": YOUTUBE_SCOPE, "state": "st8", "code_challenge": "chal", "code_challenge_method": "S256",
                 "access_type": "offline", "prompt": "consent"}
    assert FAKE_SECRET not in url  # secret은 URL에 넣지 않음
    assert "oob" not in url  # OOB copy/paste 방식 사용 안 함


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read().decode()


def _receive(state, query):
    rec = LoopbackReceiver(state)
    assert rec.redirect_uri.startswith("http://127.0.0.1:") and rec.port > 0
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("out", _catch(lambda: rec.wait(10))), daemon=True)
    t.start()
    status, html = _get(rec.redirect_uri + "/?" + urllib.parse.urlencode(query))
    t.join(10)
    return status, html, result["out"]


def _catch(fn):
    try:
        return fn()
    except OAuthError as e:
        return e


def test_loopback_receives_code():
    status, html, out = _receive("good-state", {"code": "auth-code-1", "state": "good-state"})
    assert status == 200 and "프로그램으로 돌아가세요" in html and out == "auth-code-1"


def test_loopback_rejects_csrf_state():
    _, _, out = _receive("good-state", {"code": "auth-code-1", "state": "attacker"})
    assert isinstance(out, OAuthError) and out.kind == "csrf"


def test_loopback_user_denied():
    _, _, out = _receive("s", {"error": "access_denied", "state": "s"})
    assert isinstance(out, OAuthError) and out.kind == "denied"


def test_loopback_timeout():
    rec = LoopbackReceiver("s")
    with pytest.raises(OAuthError) as e:
        rec.wait(0.6)
    assert e.value.kind == "timeout"


def test_authorize_end_to_end_with_fake_google(fake):
    """브라우저 대신: 인증 URL을 받아 사용자가 동의한 것처럼 loopback으로 code+state를 보낸다."""
    c = fake_client(fake)
    seen = {}

    def browser(url):
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
        seen.update(q)
        threading.Thread(target=lambda: _get(q["redirect_uri"] + "/?" + urllib.parse.urlencode(
            {"code": "code-xyz", "state": q["state"]})), daemon=True).start()
    tok = authorize(c, open_browser=browser, timeout=10)
    assert tok.refresh_token == FAKE_REFRESH and tok.access_token.startswith("fake-access-token-")
    form = fake.token_forms[-1]
    assert form["grant_type"] == "authorization_code" and form["code"] == "code-xyz"
    assert form["redirect_uri"] == seen["redirect_uri"] and form["client_secret"] == FAKE_SECRET
    challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == seen["code_challenge"]  # PKCE 검증값 일치
    assert FAKE_REFRESH not in repr(tok) and tok.access_token not in repr(tok)


def test_refresh_and_invalid_grant(fake):
    c = fake_client(fake)
    t = refresh_access_token(c, FAKE_REFRESH)
    assert t.access_token.startswith("fake-access-token-") and t.refresh_token == FAKE_REFRESH
    fake.token_mode = "invalid_grant"
    with pytest.raises(OAuthError) as e:
        refresh_access_token(c, FAKE_REFRESH)
    assert e.value.kind == "invalid_grant" and "7일" in str(e.value) and FAKE_REFRESH not in str(e.value)
    fake.token_mode = "server"
    with pytest.raises(OAuthError) as e:
        refresh_access_token(c, FAKE_REFRESH)
    assert e.value.kind == "server"


def test_network_error_is_friendly():
    def down(*a):
        raise TransportError("URLError")
    with pytest.raises(OAuthError) as e:
        refresh_access_token(OAuthClient("c", FAKE_SECRET), FAKE_REFRESH, transport=down)
    assert e.value.kind == "network" and "인터넷" in str(e.value)


def test_exchange_requires_refresh_token():
    def no_refresh(method, url, headers, body, timeout):
        return 200, json.dumps({"access_token": "a", "expires_in": 10}).encode()
    with pytest.raises(OAuthError, match="refresh token"):
        exchange_code(OAuthClient("c", FAKE_SECRET), code="x", code_verifier="v", redirect_uri="http://127.0.0.1:1",
                      transport=no_refresh)


def test_auth_store_dpapi_no_plaintext(tmp_path, _isolated_settings):
    if os.name != "nt":
        pytest.skip("DPAPI is Windows-only")
    store = YouTubeAuthStore(tmp_path / "youtube_token.dat")
    store.save(FAKE_REFRESH, client_id="cid", scope=YOUTUBE_SCOPE)
    raw = (tmp_path / "youtube_token.dat").read_bytes()
    for enc in ("utf-8", "utf-16-le"):
        assert FAKE_REFRESH.encode(enc) not in raw
    assert store.load()["refresh_token"] == FAKE_REFRESH
    assert FAKE_REFRESH not in repr(store)
    store.clear()
    assert store.load() is None
    assert not (_isolated_settings / "settings.json").exists() or \
        FAKE_REFRESH not in (_isolated_settings / "settings.json").read_text(encoding="utf-8")


def test_auth_store_non_windows_memory_only(tmp_path):
    store = YouTubeAuthStore(tmp_path / "t.dat", is_windows=False)
    store.save(FAKE_REFRESH, client_id="cid")
    assert store.load()["refresh_token"] == FAKE_REFRESH and not (tmp_path / "t.dat").exists()


def test_oauth_session_caches_and_refreshes(fake, tmp_path):
    store = YouTubeAuthStore(tmp_path / "t.dat", is_windows=False)
    store.save(FAKE_REFRESH, client_id="cid")
    now = [1000.0]
    s = OAuthSession(fake_client(fake), store, clock=lambda: now[0])
    a1 = s()
    assert s() == a1 and fake.issued == 1  # 캐시
    now[0] += 3600  # 만료
    a2 = s()
    assert a2 != a1 and fake.issued == 2
    assert s(force_refresh=True) != a2 and fake.issued == 3
    store.clear()
    with pytest.raises(OAuthError) as e:
        OAuthSession(fake_client(fake), store)()
    assert e.value.kind == "invalid_grant"


def test_tokens_never_logged(fake, tmp_path, caplog):
    from app.youtube_api import YouTubeApiClient
    store = YouTubeAuthStore(tmp_path / "t.dat", is_windows=False)
    store.save(FAKE_REFRESH, client_id="cid")
    session = OAuthSession(fake_client(fake), store)
    api = YouTubeApiClient(session, base_url=fake.api_base, sleep=lambda s: None)
    with caplog.at_level(logging.DEBUG):
        api.get_channel()
    text = caplog.text
    assert FAKE_REFRESH not in text and "fake-access-token" not in text and FAKE_SECRET not in text
    assert "channels.list" in text
    assert TESTING_TOKEN_WARNING and "Testing" in TESTING_TOKEN_WARNING
