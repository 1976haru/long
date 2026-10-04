"""Cloud/Local lifecycle 분리 + 초보자 Wizard UI smoke (실제 OCI/SSH 없음, 가짜 서버 사용)."""
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from cloud_fakes import FakeRemote, make_client

from app.cloud_client import CloudClient, CloudLiveController
from app.cloud_model import CLOUD_UNAVAILABLE, FREE_NOTICE, ORACLE_FREE_URL, CloudProfile, load_cloud_profile, save_cloud_profile
from app.live_secrets import SessionStreamKeyStore

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
FAKE_KEY = "dummy-life-cloud-0000-not-real"


@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    r.withdraw()
    yield r
    try:
        r.destroy()
    except tk.TclError:
        pass


def pump(root, cond, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def texts(widget):
    out = []
    try:
        out.append(str(widget.cget("text")))
    except Exception:
        pass
    for c in widget.winfo_children():
        out += texts(c)
    return out


@pytest.fixture
def cloud_env(tmp_path, monkeypatch):
    from tkinter import messagebox
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(messagebox, name, lambda *a, **k: None)
    remote = FakeRemote()
    client = make_client(tmp_path, remote)
    client.prepare()
    save_cloud_profile(client.profile)
    ctl = CloudLiveController(lambda: client, poll_seconds=3600)
    return remote, client, ctl


def live_window(root, ctl, tmp_path):
    from app.live_ui import LiveWindow
    tools = (lambda: (Path(FFMPEG), Path(FFPROBE))) if FFMPEG else (lambda: (None, None))
    return LiveWindow(root, tools=tools, key_store=SessionStreamKeyStore(), cloud=ctl)


def start_cloud_live(root, w, ctl, tmp_path):
    m = tmp_path / "EP_LIVE_READY.mp4"
    m.write_bytes(b"ready" * 1000)
    ctl.start_async(local=m, ingest_url="rtmps://a.rtmps.youtube.com:443/live2", stream_key=FAKE_KEY)
    assert pump(root, lambda: not ctl.busy and ctl.cloud_live_active)
    w._tick()


def test_cloud_pc_close_keeps_live(root, cloud_env, tmp_path, monkeypatch):
    import app.live_ui as live_ui
    remote, client, ctl = cloud_env
    w = live_window(root, ctl, tmp_path)
    start_cloud_live(root, w, ctl, tmp_path)
    assert w.st["state"].get() == "● CLOUD LIVE"
    assert str(w.btn_stop.cget("state")) == "normal" and str(w.btn_start.cget("state")) == "disabled"
    asked = []
    monkeypatch.setattr(live_ui, "ask_cloud_close", lambda parent, msg: asked.append(msg) or "pc")
    n = len(remote.calls)
    done = []
    w.confirm_close(lambda: done.append(True), for_app=True)
    assert done and "Cloud에서 LIVE가 계속 방송 중입니다." in asked[0]
    assert remote.active  # Cloud LIVE 유지
    assert not any("disable --now" in c["args"][-1] for c in remote.calls[n:])
    w.destroy()
    assert remote.active


def test_cloud_close_with_live_stop(root, cloud_env, tmp_path, monkeypatch):
    import app.live_ui as live_ui
    remote, client, ctl = cloud_env
    w = live_window(root, ctl, tmp_path)
    start_cloud_live(root, w, ctl, tmp_path)
    monkeypatch.setattr(live_ui, "ask_cloud_close", lambda parent, msg: "stop")
    done = []
    w.confirm_close(lambda: done.append(True), for_app=True)
    assert pump(root, lambda: bool(done))
    assert not remote.active and not remote.enabled
    w.destroy()


def test_cloud_close_cancel(root, cloud_env, tmp_path, monkeypatch):
    import app.live_ui as live_ui
    remote, client, ctl = cloud_env
    w = live_window(root, ctl, tmp_path)
    start_cloud_live(root, w, ctl, tmp_path)
    monkeypatch.setattr(live_ui, "ask_cloud_close", lambda parent, msg: "cancel")
    done = []
    w.confirm_close(lambda: done.append(True), for_app=True)
    root.update()
    assert not done and remote.active
    w.destroy()


def test_main_app_close_during_cloud_live_keeps_cloud(cloud_env, tmp_path, monkeypatch):
    import app.live_ui as live_ui
    import app.ui as ui
    remote, client, ctl = cloud_env
    monkeypatch.setattr(ui, "discover_ffmpeg", lambda r: (Path(FFMPEG), Path(FFPROBE)) if FFMPEG else None)
    monkeypatch.setattr(live_ui, "ask_cloud_close", lambda parent, msg: "pc")
    try:
        app = ui.MainWindow(tmp_path)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no display: {e}")
    app.withdraw()
    destroyed = []
    orig = app.destroy
    app.destroy = lambda: (destroyed.append(True), orig())
    app.live_win = live_window(app, ctl, tmp_path)
    start_cloud_live(app, app.live_win, ctl, tmp_path)
    app._close()
    assert destroyed
    assert remote.active  # PC 프로그램만 종료, Cloud LIVE 계속


def test_cloud_unavailable_offers_local(root, tmp_path, monkeypatch):
    from tkinter import messagebox
    monkeypatch.setattr(messagebox, "showerror", lambda *a, **k: None)
    remote = FakeRemote(reachable=False)
    client = make_client(tmp_path, remote)
    save_cloud_profile(client.profile)
    ctl = CloudLiveController(lambda: client, poll_seconds=3600)
    w = live_window(root, ctl, tmp_path)
    assert pump(root, lambda: CLOUD_UNAVAILABLE in w.cloud_line.get())
    labels = texts(w)
    assert "다시 확인" in labels and "내 PC에서 LIVE" in labels
    w._use_local()
    assert w.location.get() == "local"
    w.destroy()


@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")
def test_live_ready_shown_and_auto_direct_copy(root, cloud_env, tmp_path):
    remote, client, ctl = cloud_env
    src = tmp_path / "notready.mp4"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x180:r=30:d=6",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=6", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-g", "150", "-c:a", "aac", "-ac", "2", "-shortest", str(src)], check=True)
    w = live_window(root, ctl, tmp_path)
    w.set_input(src, Path(FFPROBE))
    assert pump(root, lambda: w.ready_report is not None)
    root.update()
    assert "⚠ LIVE READY 아님" in w.ready_text.get() and "Keyframe 간격 5초" in w.ready_text.get()
    assert w.btn_make_ready.winfo_ismapped()
    assert w.effective_mode() == "copy"
    w.location.set("local"); w.send_mode.set("transcode"); w._sync_widgets()
    assert w.effective_mode() == "transcode"
    w.location.set("cloud"); w._sync_widgets()
    assert w.send_mode.get() == "auto"  # Cloud는 재인코딩 금지
    w.destroy()


# ---------------- Wizard (Gate 6) ----------------

def test_wizard_four_steps_happy_path(root, tmp_path):
    from app.cloud_setup_ui import CloudSetupWizard
    remote = FakeRemote()
    key = tmp_path / "oci.key"
    key.write_text("-----BEGIN FAKE-----\nNOT-REAL-KEY-BODY\n-----END FAKE-----")
    opened = []
    ssh = tmp_path / "ssh.exe"; ssh.write_bytes(b"")
    clients = []

    def factory(profile):
        c = CloudClient(profile, ssh=ssh, runner=remote.run, popen=remote.popen)
        clients.append(c)
        return c
    done = []
    wz = CloudSetupWizard(root, client_factory=factory, open_url=opened.append, on_done=done.append)
    root.update()
    assert FREE_NOTICE in " ".join(texts(wz))
    assert "STEP 1/4" in wz.step_title.get()
    next(b for b in wz.body.winfo_children()[1].winfo_children() if b.cget("text") == "Oracle Cloud 열기").invoke()
    assert opened == [ORACLE_FREE_URL]
    wz._next()
    assert "STEP 2/4" in wz.step_title.get()
    page = " ".join(texts(wz))
    assert "Always Free Eligible 표시가 있는지 확인하세요." in page and "Ubuntu" in page and "A1" in page
    assert "비용이 발생하는 서버를 만들지 말고" in page
    wz._next()
    assert "STEP 3/4" in wz.step_title.get()
    assert str(wz.btn_next.cget("state")) == "disabled"
    wz.host.set("not an ip!")
    wz._check_conn()
    assert "형식" in wz.conn_msg.get()
    wz.host.set("123.45.67.89"); wz.user.set("ubuntu"); wz.key_path.set(str(key))
    if str(wz.btn_conn.cget("state")) == "disabled":
        wz.btn_conn.configure(state="normal")  # ssh.exe가 없는 CI에서도 흐름 검증
    wz._check_conn()
    assert pump(root, lambda: wz.connected)
    assert "연결 성공" in wz.conn_msg.get()
    prof = load_cloud_profile()
    assert prof == CloudProfile("123.45.67.89", "ubuntu", str(key))
    settings_text = (Path(__import__("app.settings", fromlist=["x"]).settings_dir()) / "settings.json").read_text(encoding="utf-8")
    assert "NOT-REAL-KEY-BODY" not in settings_text
    wz._next()
    assert "STEP 4/4" in wz.step_title.get()
    wz._prepare()
    assert pump(root, lambda: wz.prepared)
    root.update()
    assert "✓ 무료 Cloud 준비 완료" in wz.prep_msg.get() and "Oracle Console" in wz.prep_msg.get()
    assert all(v.get().startswith("✓") for v in wz.step_labels)
    assert remote.installed
    wz._next()  # 닫기
    assert done and done[0].host == "123.45.67.89"


def test_wizard_shows_prepare_failure_and_local_hint(root, tmp_path):
    from app.cloud_setup_ui import CloudSetupWizard
    remote = FakeRemote(os_id="ol")
    client = make_client(tmp_path, remote)
    wz = CloudSetupWizard(root, client_factory=lambda p: client, open_url=lambda u: None)
    wz.client = client
    wz._go(4)
    wz._prepare()
    assert pump(root, lambda: not wz.busy and "✗" in wz.prep_msg.get())
    assert "Ubuntu" in wz.prep_msg.get() and "내 PC에서 LIVE" in wz.prep_msg.get()
    wz.destroy()
