import time
from pathlib import Path

import pytest

from app.core import BuildError, VideoInfo
from app.live_controller import (
    LiveController, format_bitrate, probe_live_input, run_preflight,
)
from app.live_profile import LIVE_PRESETS, LiveConfigError, preset_by_key, recommend_preset
from app.live_secrets import SessionStreamKeyStore
from app.live_supervisor import LiveBusyError, LiveState
from app.tooling import FfmpegExecutionGuard, KeepAwake

FAKE_KEY = "dummy-ui-0000-not-real-key"
INGEST = "rtmps://a.rtmps.youtube.com:443/live2"


def vinfo(path, *, audio="aac", width=1920, height=1080, duration=60.0):
    return VideoInfo(Path(path), duration, 1000, width, height, 30.0, "h264", audio, "yuv420p", "High",
                     44100 if audio else 0, 2 if audio else 0)


@pytest.fixture
def env(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    src = tmp_path / "CHILI_LAB_EP001.mp4"
    for p in (ffmpeg, ffprobe, src):
        p.write_bytes(b"x")
    return ffmpeg, ffprobe, src


def preflight(env, *, probe=None, key=FAKE_KEY, url=INGEST, guard=None, state=LiveState.STOPPED, input_path="src"):
    ffmpeg, ffprobe, src = env
    return run_preflight(
        ffmpeg=ffmpeg, ffprobe=ffprobe, input_path=src if input_path == "src" else input_path,
        ingest_url=url, stream_key=key, preset=preset_by_key("1080p30"),
        guard=guard or FfmpegExecutionGuard(), supervisor_state=state,
        probe=probe or (lambda p, f: vinfo(p)),
    )


def no_secret(r):
    return FAKE_KEY not in r.report() and all(FAKE_KEY not in e for e in r.errors()) and FAKE_KEY not in repr(r)


def test_preflight_success(env):
    r = preflight(env)
    assert r.ok, r.report()
    assert r.config is not None and r.config.stream_key == FAKE_KEY
    assert r.config.video_bitrate_kbps == 8000 and r.config.audio_bitrate_kbps == 128
    assert "LIVE 시작 준비 완료" in r.report()
    assert "YouTube RTMPS" in r.report()
    assert no_secret(r)


def test_preflight_missing_input(env, tmp_path):
    r = preflight(env, input_path=tmp_path / "nope.mp4")
    assert not r.ok and any("찾을 수 없습니다" in e for e in r.errors())
    r = preflight(env, input_path=None)
    assert not r.ok and any("선택하세요" in e for e in r.errors())


def test_preflight_missing_audio(env):
    r = preflight(env, probe=lambda p, f: vinfo(p, audio=""))
    assert not r.ok
    assert any("오디오" in e for e in r.errors())
    assert r.config is None


def test_preflight_probe_failure_and_zero_duration(env):
    def bad(p, f):
        raise BuildError("no video stream")
    assert not preflight(env, probe=bad).ok
    assert not preflight(env, probe=lambda p, f: vinfo(p, duration=0)).ok


def test_preflight_missing_key_and_bad_url(env):
    r = preflight(env, key="  ")
    assert not r.ok and any("Stream Key" in e for e in r.errors())
    r = preflight(env, url="http://example.com/live2")
    assert not r.ok and no_secret(r)
    r = preflight(env, key="bad key/with space")
    assert not r.ok and "bad key/with space" not in r.report()


def test_preflight_missing_ffmpeg(env, tmp_path):
    _, ffprobe, src = env
    r = run_preflight(ffmpeg=tmp_path / "missing.exe", ffprobe=ffprobe, input_path=src, ingest_url=INGEST,
                      stream_key=FAKE_KEY, preset=LIVE_PRESETS[1], probe=lambda p, f: vinfo(p),
                      guard=FfmpegExecutionGuard())
    assert not r.ok and any("FFmpeg" in e for e in r.errors())


def test_preflight_guard_conflict_and_running(env):
    g = FfmpegExecutionGuard()
    g.try_acquire("long")
    r = preflight(env, guard=g)
    assert not r.ok and "현재 장시간 영상 제작 중입니다." in r.errors()
    r = preflight(env, state=LiveState.RUNNING)
    assert not r.ok


def test_probe_live_input_rejects_missing(tmp_path):
    with pytest.raises(LiveConfigError):
        probe_live_input(tmp_path / "x.mp4", Path("ffprobe"))


def test_recommend_preset_matches_input_resolution():
    assert recommend_preset(720).key == "720p30"
    assert recommend_preset(1080).key == "1080p30"
    assert {p.key: p.video_bitrate_kbps for p in LIVE_PRESETS} == {"720p30": 5000, "1080p30": 8000, "1080p30hq": 10000}
    assert "1080p 입력용" in preset_by_key("1080p30").label


def test_format_bitrate():
    assert format_bitrate("7912.3kbits/s") == "7.9 Mbit/s"
    assert format_bitrate("640.0kbits/s") == "640 kbit/s"
    assert format_bitrate(None) == "-"


# ---------------- controller ----------------

class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FakeProcess:
    def __init__(self):
        self.running = False
        self.rc = None

    def start(self):
        self.running = True

    def stop(self, *a, **k):
        if self.running:
            self.running = False
            self.rc = 0
        return self.rc

    def crash(self):
        self.running = False
        self.rc = 1

    def is_running(self):
        return self.running

    def return_code(self):
        return self.rc

    def uptime(self):
        return 0.0

    def recent_errors(self):
        return [f"rtmps://host/live2/********: Connection reset"]


def make_controller(guard=None):
    procs = []
    calls = []
    clock = FakeClock()

    def builder(ffmpeg, config):
        def factory():
            p = FakeProcess()
            procs.append(p)
            return p
        return factory

    ka = KeepAwake(setter=calls.append)
    c = LiveController(guard=guard or FfmpegExecutionGuard(), keep_awake=ka, clock=clock, factory_builder=builder)
    return c, procs, clock, ka


def config(env):
    return preflight(env).config


def events_text(evs):
    return repr(evs)


def test_controller_state_transitions_and_reconnect(env):
    c, procs, clock, ka = make_controller()
    assert c.start(ffmpeg=env[0], config=config(env), background=False) is LiveState.RUNNING
    assert ka.active
    evs = c.drain_events()
    assert [e[1] for e in evs] == [LiveState.STARTING, LiveState.RUNNING]
    assert evs[-1][2] == "● LIVE"
    procs[-1].crash()
    c.supervisor.poll()
    snap = c.snapshot()
    assert snap.state is LiveState.RECONNECT_WAIT and snap.label == "재연결 대기"
    assert snap.retry_in == 5
    assert "Connection reset" in snap.last_error
    clock.t += 5
    c.supervisor.poll()
    assert c.state is LiveState.RUNNING
    assert c.snapshot().reconnects == 1
    evs = c.drain_events()
    assert [e[1] for e in evs] == [LiveState.RECONNECT_WAIT, LiveState.STARTING, LiveState.RUNNING]
    assert FAKE_KEY not in events_text(evs)
    assert FAKE_KEY not in repr(c.snapshot()) and FAKE_KEY not in repr(c)
    c.stop_blocking()
    assert c.state is LiveState.STOPPED and not ka.active


def test_controller_user_stop_no_reconnect(env):
    g = FfmpegExecutionGuard()
    c, procs, clock, ka = make_controller(g)
    c.start(ffmpeg=env[0], config=config(env), background=False)
    t = c.stop_async()
    t.join(5)
    assert c.state is LiveState.STOPPED
    clock.t += 1000
    c.supervisor.poll()
    assert len(procs) == 1 and c.state is LiveState.STOPPED
    c.drain_events()
    assert not ka.active
    assert g.owner is None


def test_controller_reconnect_disabled_fails(env):
    g = FfmpegExecutionGuard()
    c, procs, clock, ka = make_controller(g)
    c.start(ffmpeg=env[0], config=config(env), reconnect=False, background=False)
    procs[-1].crash()
    c.supervisor.poll()
    assert c.state is LiveState.FAILED
    assert g.owner is None
    c.drain_events()
    assert not ka.active
    clock.t += 100
    c.supervisor.poll()
    assert len(procs) == 1


def test_controller_start_failure_event_is_redacted(env):
    g = FfmpegExecutionGuard()

    def builder(ffmpeg, cfg):
        def factory():
            raise RuntimeError(f"cannot open rtmps://x/live2/{cfg.stream_key}")
        return factory
    c = LiveController(guard=g, keep_awake=KeepAwake(setter=lambda f: None), clock=FakeClock(), factory_builder=builder)
    assert c.start(ffmpeg=env[0], config=config(env), background=False) is LiveState.FAILED
    evs = c.drain_events()
    assert FAKE_KEY not in events_text(evs)
    assert FAKE_KEY not in c.snapshot().last_error
    assert g.owner is None


def test_controller_background_watchdog_reconnects(env, monkeypatch):
    """실제 watchdog 스레드: crash → 재연결 대기 → 재시작 (delay를 짧게 패치)."""
    import app.live_supervisor as sup_mod
    monkeypatch.setattr(sup_mod, "RETRY_DELAYS", (0.2,))
    procs = []

    def builder(ffmpeg, cfg):
        def factory():
            p = FakeProcess()
            procs.append(p)
            return p
        return factory
    c = LiveController(guard=FfmpegExecutionGuard(), keep_awake=KeepAwake(setter=lambda f: None), factory_builder=builder)
    c.start(ffmpeg=env[0], config=config(env))
    procs[-1].crash()
    deadline = time.monotonic() + 5
    while len(procs) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(procs) == 2 and c.state is LiveState.RUNNING
    c.stop_blocking()
    assert c.state is LiveState.STOPPED
    assert not c.supervisor._thread.is_alive()


# ---------------- FFmpeg mutual exclusion (Gate 5) ----------------

def test_mutual_exclusion_all_directions(env):
    g = FfmpegExecutionGuard()
    c, procs, clock, _ = make_controller(g)
    # long running → live blocked
    assert g.try_acquire("long")
    with pytest.raises(LiveBusyError, match="현재 장시간 영상 제작 중입니다."):
        c.start(ffmpeg=env[0], config=config(env), background=False)
    assert procs == []
    # long stop → live allowed
    g.release("long")
    c.start(ffmpeg=env[0], config=config(env), background=False)
    # live running → long blocked
    assert not g.try_acquire("long")
    # live reconnect wait → long blocked
    procs[-1].crash()
    c.supervisor.poll()
    assert c.state is LiveState.RECONNECT_WAIT
    assert not g.try_acquire("long")
    # live stop → long allowed
    c.stop_blocking()
    assert g.try_acquire("long")
    g.release("long")


# ---------------- Tk window smoke ----------------

def _tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    return root


def test_live_window_masked_key_locking_and_close(env):
    from app.live_ui import LiveWindow
    root = _tk_root()
    try:
        c, procs, clock, ka = make_controller()
        store = SessionStreamKeyStore()
        w = LiveWindow(root, tools=lambda: (env[0], env[1]), controller=c, key_store=store)
        root.update()
        assert w.title() == "24H Playlist LIVE Studio"
        assert w.ent_key.cget("show") == "●"
        assert str(w.chk_remember.cget("state")) == "disabled"  # non-persistent store
        w.key_var.set(FAKE_KEY)
        w._toggle_reveal()
        assert w.ent_key.cget("show") == ""
        w._hide_key()
        assert w.ent_key.cget("show") == "●"
        assert str(w.btn_start.cget("state")) == "normal" and str(w.btn_stop.cget("state")) == "disabled"

        c.start(ffmpeg=env[0], config=config(env), background=False)
        w._tick()
        assert w.st["state"].get() == "● LIVE"
        for b in (w.btn_start, w.btn_video, w.btn_check):
            assert str(b.cget("state")) == "disabled"
        assert str(w.ent_key.cget("state")) == "disabled"
        assert str(w.cmb_preset.cget("state")) == "disabled"
        assert str(w.btn_stop.cget("state")) == "normal"
        assert FAKE_KEY not in w.title()
        assert all(FAKE_KEY not in v.get() for v in w.st.values())

        closed = []
        w.shutdown(lambda: closed.append(True))
        deadline = time.monotonic() + 5
        while not closed and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        assert closed and c.state is LiveState.STOPPED
        assert c.guard.owner is None and not ka.active
        w.destroy()
    finally:
        root.destroy()


def test_dpapi_store_loaded_key_stays_masked(env, tmp_path):
    import os
    if os.name != "nt":
        pytest.skip("Windows only")
    from app.live_secrets import WindowsDpapiStreamKeyStore
    from app.live_ui import LiveWindow
    store = WindowsDpapiStreamKeyStore(tmp_path / "s.dat")
    store.set(FAKE_KEY)
    root = _tk_root()
    try:
        w = LiveWindow(root, tools=lambda: (env[0], env[1]), controller=make_controller()[0], key_store=store)
        assert w.remember.get() is True
        assert w.ent_key.cget("show") == "●"
        assert FAKE_KEY not in w.key_note.get()
        w.remember.set(False)
        w._apply_remember()
        assert not store.has_saved()
        w.destroy()
    finally:
        root.destroy()


def test_supervisor_log_never_contains_key(env, caplog):
    """start 실패 예외에 key가 섞여도 logging 출력에는 남지 않는다."""
    import logging

    def builder(ffmpeg, cfg):
        def factory():
            raise RuntimeError(f"cannot open rtmps://x/live2/{cfg.stream_key}")
        return factory
    c = LiveController(guard=FfmpegExecutionGuard(), keep_awake=KeepAwake(setter=lambda f: None),
                       clock=FakeClock(), factory_builder=builder)
    with caplog.at_level(logging.DEBUG):
        c.start(ffmpeg=env[0], config=config(env), background=False)
    assert c.state is LiveState.FAILED
    assert caplog.text and FAKE_KEY not in caplog.text
    assert FAKE_KEY not in c.supervisor.last_error
