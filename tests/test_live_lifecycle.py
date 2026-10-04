"""Gate 6: 실제 FFmpeg + 실제 Tk 창으로 닫기 lifecycle 검증 (네트워크 없음: 로컬 FLV 출력)."""
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.live_controller import LiveController, run_preflight
from app.live_core import LiveProcess, build_live_command
from app.live_profile import preset_by_key
from app.live_secrets import SessionStreamKeyStore
from app.live_supervisor import LiveState
from app.tooling import FfmpegExecutionGuard, KeepAwake

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
FAKE_KEY = "dummy-life-0000-not-real"
pytestmark = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")


def pid_alive(pid):
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, check=False).stdout
    return f'"{pid}"' in out


@pytest.fixture
def live_setup(tmp_path, monkeypatch):
    import app.settings as settings
    monkeypatch.setattr(settings, "settings_dir", lambda: tmp_path)
    src = tmp_path / "set.mp4"
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=320x180:r=30:d=1",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src),
    ], check=True)
    procs = []

    def builder(ffmpeg, config):
        def factory():
            cmd = build_live_command(ffmpeg=ffmpeg, config=config, output_target=str(tmp_path / f"o{len(procs)}.flv"))
            p = LiveProcess(cmd, secrets=[config.stream_key])
            procs.append(p)
            return p
        return factory

    guard = FfmpegExecutionGuard()
    controller = LiveController(guard=guard, keep_awake=KeepAwake(setter=lambda f: None), factory_builder=builder)
    r = run_preflight(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), input_path=src,
                      ingest_url="rtmps://a.rtmps.youtube.com:443/live2", stream_key=FAKE_KEY,
                      preset=preset_by_key("720p30"), guard=guard)
    assert r.ok, r.report()
    return controller, r.config, procs, guard


def start_and_wait(controller, config, procs):
    controller.start(ffmpeg=Path(FFMPEG), config=config)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        s = procs[-1].stats()
        if s.out_time_seconds and s.out_time_seconds >= 1.0:
            break
        time.sleep(0.1)
    assert controller.state is LiveState.RUNNING
    return procs[-1]._proc.pid


def pump_until(root, cond, timeout=20):
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        try:
            root.update()
        except Exception:
            break
        time.sleep(0.02)
    return cond()


def test_live_window_close_stops_ffmpeg(live_setup, monkeypatch):
    import tkinter as tk
    from tkinter import messagebox
    from app.live_ui import LiveWindow
    controller, config, procs, guard = live_setup
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    try:
        w = LiveWindow(root, tools=lambda: (Path(FFMPEG), Path(FFPROBE)), controller=controller, key_store=SessionStreamKeyStore())
        pid = start_and_wait(controller, config, procs)
        w.request_close()  # LIVE 창 X → 확인(예)
        assert pump_until(root, lambda: not w.winfo_exists())
        assert controller.state is LiveState.STOPPED
        assert procs[-1].last_stop_method == "graceful"
        assert not pid_alive(pid)
        assert guard.owner is None
        assert not controller.supervisor._thread.is_alive()
    finally:
        root.destroy()


def test_main_app_close_stops_live(live_setup, monkeypatch, tmp_path):
    from tkinter import messagebox
    import app.ui as ui
    controller, config, procs, guard = live_setup
    asked = []
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: asked.append(a) or True)
    monkeypatch.setattr(ui, "discover_ffmpeg", lambda root: (Path(FFMPEG), Path(FFPROBE)))
    try:
        app = ui.MainWindow(tmp_path)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no display: {e}")
    app.withdraw()
    destroyed = []
    orig_destroy = app.destroy
    app.destroy = lambda: (destroyed.append(True), orig_destroy())
    app.live_win = ui.LiveWindow(app, tools=app._live_tools, controller=controller, key_store=SessionStreamKeyStore())
    pid = start_and_wait(controller, config, procs)
    app._close()  # 메인 X → "현재 LIVE 송출 중입니다" 확인(예)
    assert any("현재 LIVE 송출 중입니다." in str(a) for a in asked)
    deadline = time.monotonic() + 20
    while not destroyed and time.monotonic() < deadline:
        app.update()
        time.sleep(0.02)
    assert destroyed
    assert controller.state is LiveState.STOPPED
    assert procs[-1].last_stop_method == "graceful"
    assert not pid_alive(pid)
    assert guard.owner is None
    assert not (tmp_path / "settings.json").exists() or FAKE_KEY not in (tmp_path / "settings.json").read_text(encoding="utf-8")
