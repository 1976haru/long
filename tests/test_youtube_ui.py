"""Gate 6: YouTube 설정/연결/실행기 + Wizard + LIVE 창 API 모드 GUI smoke (실제 Google/YouTube 접속 없음)."""
import json
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from app.live_controller import LiveController
from app.live_secrets import SessionStreamKeyStore
from app.live_session import SESSION_ARCHIVE_SAFE, SESSION_CONTINUOUS
from app.tooling import FfmpegExecutionGuard, KeepAwake
from app.youtube_api import YouTubeApiClient, YouTubeChannelInfo
from app.youtube_config import (
    GOOGLE_API_LIBRARY_URL, RolloverRunner, connect_account, is_connected, load_youtube_settings,
    save_youtube_settings, template_from_settings,
)
from app.youtube_oauth import OAuthClient, YouTubeAuthStore
from app.youtube_session import (
    SESSION_YOUTUBE_AUTO, STREAM_MODE_API, STREAM_MODE_MANUAL, BroadcastTemplate, RolloverState, YouTubeRolloverManager,
)
from youtube_fakes import FAKE_ACCESS, FAKE_REFRESH, FAKE_STREAM_NAME, FakeClock, FakeYouTube

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


@pytest.fixture
def fake():
    f = FakeYouTube()
    yield f
    f.close()


@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    r.withdraw()
    yield r
    r.destroy()


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from tkinter import messagebox
    shown = []
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(messagebox, name, lambda *a, **k: shown.append(a))
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    return shown


def pump(root, cond, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.03)
    return cond()


def consent_browser(url):
    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}

    def go():
        urllib.request.urlopen(q["redirect_uri"] + "/?" + urllib.parse.urlencode({"code": "c1", "state": q["state"]}),
                               timeout=10).read()
    threading.Thread(target=go, daemon=True).start()


# ---------------- config ----------------

def test_settings_never_store_secrets(_isolated_settings):
    for bad in ({"refresh_token": "x"}, {"client_secret": "x"}, {"stream_key": "x"}, {"stream_name": "x"}):
        with pytest.raises(ValueError):
            save_youtube_settings(**bad)
    save_youtube_settings(stream_mode=STREAM_MODE_API, template={"title": "T", "privacy": "private"})
    t = template_from_settings()
    assert t.title == "T" and t.privacy == "private" and t.title_rule == "same"
    assert load_youtube_settings()["stream_mode"] == STREAM_MODE_API


def test_connect_account_end_to_end(fake, tmp_path, _isolated_settings):
    store = YouTubeAuthStore(tmp_path / "t.dat", is_windows=False)
    client = OAuthClient("cid.apps.googleusercontent.com", "GOCSPX-fake-0000", token_uri=fake.token_uri)
    cf = tmp_path / "client_secret_x.json"
    cf.write_text("{}")
    ch = connect_account(str(cf), store=store, open_browser=consent_browser, client=client, api_base=fake.api_base,
                         timeout=10)
    assert ch.title == "Old Pop Lounge"
    s = load_youtube_settings()
    assert s["channel_title"] == "Old Pop Lounge" and s["client_file"] == str(cf)
    assert store.load()["refresh_token"] == FAKE_REFRESH
    text = (_isolated_settings / "settings.json").read_text(encoding="utf-8")
    assert FAKE_REFRESH not in text and "GOCSPX" not in text and "fake-access" not in text
    assert is_connected(store)


def test_rollover_runner_ticks_in_background(fake):
    clock = FakeClock()
    api = YouTubeApiClient(lambda force_refresh=False: FAKE_ACCESS, base_url=fake.api_base, sleep=clock.sleep, clock=clock)
    st = api.ensure_reusable_stream()
    m = YouTubeRolloverManager(api, stream_id=st.id, template=BroadcastTemplate("T"), clock=clock, sleep=clock.sleep,
                               session_seconds=1000, prepare_lead=600)
    m.go_live_first()
    clock.t += 500  # 준비 시점
    r = RolloverRunner(m, interval=0.05)
    r.start()
    end = time.monotonic() + 5
    while time.monotonic() < end and not m.next_id:
        time.sleep(0.05)
    r.stop()
    snaps = r.drain()
    assert m.next_id and snaps and snaps[-1].next_ready and not r.running


# ---------------- wizard ----------------

def test_wizard_five_steps(root, tmp_path, _isolated_settings):
    from app.youtube_setup_ui import YouTubeSetupWizard
    opened, done = [], []
    good = tmp_path / "client_secret_good.json"
    good.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "GOCSPX-fake",
                                              "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                                              "token_uri": "https://oauth2.googleapis.com/token"}}))
    bad = tmp_path / "web.json"
    bad.write_text(json.dumps({"web": {"client_id": "x", "client_secret": "y"}}))
    picks = [str(bad), str(good)]

    def fake_connect(path, open_browser):
        save_youtube_settings(channel_id="UCfake0001", channel_title="Old Pop Lounge")
        return YouTubeChannelInfo("UCfake0001", "Old Pop Lounge")
    w = YouTubeSetupWizard(root, on_done=done.append, connect=fake_connect, open_url=opened.append,
                           pick_file=lambda **kw: picks.pop(0))
    root.update()
    assert "STEP 1/5" in w.step_title.get()
    next(b for b in w.body.winfo_children() if isinstance(b, __import__("tkinter").ttk.Button)).invoke()
    assert opened == [GOOGLE_API_LIBRARY_URL]
    w._next()
    assert "STEP 2/5" in w.step_title.get()
    texts = " ".join(str(c.cget("text")) for c in w.body.winfo_children() if hasattr(c, "cget") and "text" in c.keys())
    assert "데스크톱 앱" in texts and "7일" in texts  # Testing 만료 경고 표시
    w._next()
    assert "STEP 3/5" in w.step_title.get() and str(w.btn_next.cget("state")) == "disabled"
    w._pick()
    assert "✗" in w.file_msg.get() and "데스크톱 앱" in w.file_msg.get()
    w._pick()
    assert "✓" in w.file_msg.get() and str(w.btn_next.cget("state")) == "normal"
    assert load_youtube_settings()["client_file"] == str(good)
    assert "GOCSPX" not in (_isolated_settings / "settings.json").read_text(encoding="utf-8")
    w._next()
    assert "STEP 4/5" in w.step_title.get()
    w._start_connect()
    assert pump(root, lambda: w.step == 5)
    assert w.channel_title.get() == "Old Pop Lounge" and w.connected
    w._next()  # 완료
    assert done and done[0]["channel_title"] == "Old Pop Lounge"


def test_wizard_connect_failure_message(root, _isolated_settings):
    from app.youtube_setup_ui import YouTubeSetupWizard
    from app.youtube_oauth import OAuthError

    def failing(path, open_browser):
        raise OAuthError("Google 계정 연결이 취소되었거나 거부되었습니다.", "denied")
    w = YouTubeSetupWizard(root, connect=failing, open_url=lambda u: None)
    w._go(4)
    w._start_connect()
    assert pump(root, lambda: "✗" in w.conn_msg.get())
    assert "취소" in w.conn_msg.get() and w.step == 4
    w.destroy()


# ---------------- LIVE 창 API 모드 ----------------

class Proc:
    running = False
    rc = None

    def start(self):
        self.running = True

    def stop(self, *a, **k):
        self.running, self.rc = False, 0
        return 0

    def is_running(self):
        return self.running

    def return_code(self):
        return self.rc

    def uptime(self):
        return 0.0


def window(root, **kw):
    from app.cloud_client import CloudLiveController
    from app.live_ui import LiveWindow
    tools = (lambda: (Path(FFMPEG), Path(FFPROBE))) if FFMPEG else (lambda: (None, None))
    return LiveWindow(root, tools=tools, key_store=SessionStreamKeyStore(),
                      cloud=CloudLiveController(lambda: None, poll_seconds=3600), **kw)


def test_api_mode_widgets(root, _isolated_settings):
    w = window(root)
    assert w.yt_mode.get() == STREAM_MODE_MANUAL and not w.yt_frame.winfo_manager()
    assert str(w.rb_yt_auto.cget("state")) == "disabled"
    w.yt_mode.set(STREAM_MODE_API)
    w._on_yt_mode()
    assert w.yt_frame.winfo_manager() == "pack" and str(w.ent_key.cget("state")) == "disabled"
    assert str(w.rb_yt_auto.cget("state")) == "normal" and "연결 안 됨" in w.yt_status.get()
    w.session_mode.set(SESSION_YOUTUBE_AUTO)
    w.yt_mode.set(STREAM_MODE_MANUAL)
    w._on_yt_mode()
    assert w.session_mode.get() == SESSION_CONTINUOUS  # 수동 Key에서는 자동 교체 불가
    assert load_youtube_settings()["stream_mode"] == STREAM_MODE_MANUAL
    w.destroy()


@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg not installed")
def test_api_mode_local_start_golive_rollover_rows_and_stop(root, fake, tmp_path, monkeypatch, quiet, _isolated_settings):
    import app.live_ui as live_ui
    src = tmp_path / "EP_LIVE_READY.mp4"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x180:r=30:d=4",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=4", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0", "-c:a", "aac",
                    "-ac", "2", "-shortest", str(src)], check=True)
    api = YouTubeApiClient(lambda force_refresh=False: FAKE_ACCESS, base_url=fake.api_base, sleep=lambda s: None)
    monkeypatch.setattr(live_ui, "build_api_client", lambda: api)
    monkeypatch.setattr(live_ui, "is_connected", lambda: True)
    save_youtube_settings(client_file="x.json", channel_id="UCfake0001", channel_title="Old Pop Lounge")
    configs = []

    def builder(ffmpeg, cfg):
        configs.append(cfg)
        return Proc
    ctl = LiveController(guard=FfmpegExecutionGuard(), keep_awake=KeepAwake(setter=lambda f: None), factory_builder=builder)
    w = window(root, controller=ctl)
    w.location.set("local")
    w.set_input(src, Path(FFPROBE))
    assert pump(root, lambda: w.ready_report is not None)
    w.yt_mode.set(STREAM_MODE_API)
    w._on_yt_mode()
    w.yt_title.set("🍂 가을에 듣기 좋은 샹송 | 24H LIVE")
    w.yt_privacy.set("private")
    w.session_mode.set(SESSION_ARCHIVE_SAFE)
    w._start()
    assert any("계속 방송" in str(a) for a in quiet)  # API 모드에서 보관 안전(수동)은 막음
    w.session_mode.set(SESSION_YOUTUBE_AUTO)
    w._start()
    assert pump(root, lambda: w.yt_manager is not None and w.yt_runner is not None, timeout=30), quiet
    cfg = configs[-1]
    assert cfg.stream_key == FAKE_STREAM_NAME and cfg.ingest_url.startswith("rtmps://")  # API가 준 stream으로 송출
    assert ctl.supervisor.session_limit is None  # 자동 교체: FFmpeg는 11:50에 멈추지 않음
    bid = w.yt_manager.current_id
    assert fake.status_of(bid) == "live"
    assert fake.broadcasts[bid]["body"]["status"]["privacyStatus"] == "private"
    w._tick()
    assert w.st["yt_broadcast"].get() == "● LIVE" and w.st["yt_rollover"].get() in ("11:50:00", "11:49:59")
    assert FAKE_STREAM_NAME not in w.key_var.get() and all(FAKE_STREAM_NAME not in v.get() for v in w.st.values())
    assert FAKE_STREAM_NAME not in (_isolated_settings / "settings.json").read_text(encoding="utf-8")
    assert load_youtube_settings()["stream_id"]  # stream id만 저장 (재사용)
    w._stop()
    assert pump(root, lambda: fake.status_of(bid) == "complete", timeout=10)  # 종료 시 현재 방송 complete
    assert w.yt_runner is None
    w.destroy()
