"""세션 엔진 (Gate 4): 계속 방송 / 보관 안전 11:50, 재접속 억제, 24시간 가속(fake clock) 수명 테스트."""
import sys
from pathlib import Path

import pytest

from app.live_controller import LiveController
from app.live_session import (
    ARCHIVE_SAFE_SECONDS, NEXT_SESSION_MESSAGE, SESSION_ARCHIVE_SAFE, SESSION_CONTINUOUS, ManualSessionProvider,
    archive_notice, format_tb, monthly_transfer_bytes, session_limit_seconds,
)
from app.live_supervisor import TERMINAL_STATES, LiveState, LiveSupervisor
from app.tooling import FfmpegExecutionGuard, KeepAwake

ROOT = Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Proc:
    def __init__(self):
        self.running = False
        self.rc = None
        self.stops = 0

    def start(self):
        self.running = True

    def stop(self, *a, **k):
        self.stops += 1
        if self.running:
            self.running, self.rc = False, 0  # q 정상 종료
        return self.rc

    def crash(self):
        self.running, self.rc = False, 1

    def is_running(self):
        return self.running

    def return_code(self):
        return self.rc

    def uptime(self):
        return 0.0


def sup(limit=None, guard=None):
    procs, clock = [], Clock()

    def factory():
        p = Proc()
        procs.append(p)
        return p
    s = LiveSupervisor(factory, guard=guard or FfmpegExecutionGuard(), clock=clock, session_limit=limit)
    return s, procs, clock


def test_constant_and_modes():
    assert ARCHIVE_SAFE_SECONDS == 42600 == 11 * 3600 + 50 * 60
    sys.path.insert(0, str(ROOT / "cloud"))
    import long_live_worker
    assert long_live_worker.ARCHIVE_SAFE_SECONDS == ARCHIVE_SAFE_SECONDS  # 서버 worker와 같은 값
    assert session_limit_seconds(SESSION_CONTINUOUS) is None
    assert session_limit_seconds(SESSION_ARCHIVE_SAFE) == 42600
    with pytest.raises(ValueError):
        session_limit_seconds("forever")


@pytest.mark.parametrize("remaining,text", [
    (None, ""), (3600, ""), (601, ""), (600, "약 10분 후 보관 안전 종료"), (301, "약 10분 후 보관 안전 종료"),
    (300, "약 5분 후 종료"), (61, "약 5분 후 종료"), (60, "약 1분 후 종료"), (1, "약 1분 후 종료"), (0, "보관 안전 종료 시간입니다"),
])
def test_countdown_notice(remaining, text):
    assert archive_notice(remaining) == text


def test_transfer_estimate():
    b = monthly_transfer_bytes(6.3)
    assert b == pytest.approx(6.3e6 / 8 * 86400 * 30)
    assert format_tb(b) == "2.04 TB"
    assert monthly_transfer_bytes(6.3, streams=5) == pytest.approx(5 * b)


def test_manual_provider():
    p = ManualSessionProvider()
    assert p.prepare_next_broadcast() == NEXT_SESSION_MESSAGE and "[다음 세션 시작]" in NEXT_SESSION_MESSAGE
    assert p.get_next_ingest("rtmps://x/live2") == "rtmps://x/live2"
    assert p.complete_current_broadcast() is None


def test_continuous_never_stops_on_timer():
    s, procs, clock = sup(limit=None)
    s.start()
    clock.t += 3 * 24 * 3600  # 72시간
    assert s.poll() is LiveState.RUNNING and s.session_remaining() is None
    assert procs[0].stops == 0


def test_archive_safe_exact_limit_graceful_and_no_restart():
    g = FfmpegExecutionGuard()
    s, procs, clock = sup(limit=ARCHIVE_SAFE_SECONDS, guard=g)
    s.start()
    clock.t += ARCHIVE_SAFE_SECONDS - 1
    assert s.poll() is LiveState.RUNNING and s.session_remaining() == 1
    clock.t += 1  # 정확히 42600초
    assert s.poll() is LiveState.SESSION_LIMIT_REACHED
    assert procs[0].stops == 1 and procs[0].rc == 0  # q 정상 종료
    assert g.owner is None  # 장시간 제작 등 다른 FFmpeg 작업 가능
    assert s.state in TERMINAL_STATES and not s.active
    clock.t += 3600
    assert s.poll() is LiveState.SESSION_LIMIT_REACHED and len(procs) == 1  # watchdog 재시작 없음


def test_crash_near_limit_does_not_reconnect_past_limit():
    s, procs, clock = sup(limit=100)
    s.start()
    clock.t += 95
    procs[0].crash()
    assert s.poll() is LiveState.RECONNECT_WAIT  # 5초 뒤 재접속 예약
    clock.t += 5  # = 100초 (한도)
    assert s.poll() is LiveState.SESSION_LIMIT_REACHED
    assert len(procs) == 1  # 한도에서는 재접속하지 않음


def test_next_session_starts_fresh():
    s, procs, clock = sup(limit=100)
    s.start()
    clock.t += 100
    s.poll()
    s.start()  # [다음 세션 시작]
    assert s.state is LiveState.RUNNING and len(procs) == 2
    assert s.session_remaining() == 100


def test_accelerated_24h_lifecycle_with_crashes():
    """fake clock 24시간: 계속 방송은 crash마다 재접속하며 24시간 유지, 보관 안전은 11:50에 정확히 1회 종료."""
    for limit, expect_end in ((None, LiveState.RUNNING), (ARCHIVE_SAFE_SECONDS, LiveState.SESSION_LIMIT_REACHED)):
        s, procs, clock = sup(limit=limit)
        s.start()
        crashes = 0
        for step in range(24 * 60):  # 1분 단위 24시간
            clock.t += 60
            if step % 97 == 96 and s.state is LiveState.RUNNING:  # 가끔 YouTube 연결 끊김
                procs[-1].crash()
                crashes += 1
            s.poll()
            if s.state is LiveState.RECONNECT_WAIT:
                clock.t += 60
                s.poll()
        assert s.state is expect_end, (limit, s.state)
        assert len(s.state_history) <= 100 and len(s.reconnect_history) <= 100  # 기록은 bounded
        if limit is None:
            assert s.reconnect_count == crashes
        else:
            ended = [h for h in s.state_history if h[1] == "SESSION_LIMIT_REACHED"]
            assert len(ended) == 1


def test_controller_snapshot_session_and_keepawake():
    calls = []
    procs = []

    def builder(ffmpeg, config):
        def factory():
            p = Proc()
            procs.append(p)
            return p
        return factory
    clock = Clock()
    c = LiveController(guard=FfmpegExecutionGuard(), keep_awake=KeepAwake(setter=calls.append), clock=clock,
                       factory_builder=builder)
    from app.live_profile import MODE_COPY, LiveConfig
    cfg = LiveConfig(input_path=Path("a.mp4"), ingest_url="rtmps://x/live2", stream_key="dummy-k", mode=MODE_COPY)
    c.start(ffmpeg=Path("ffmpeg"), config=cfg, session_limit=ARCHIVE_SAFE_SECONDS, background=False,
            playlist=[("A.mp4", 10.0), ("B.mp4", 10.0)])
    clock.t += ARCHIVE_SAFE_SECONDS - 290
    snap = c.snapshot()
    assert snap.session_limit == ARCHIVE_SAFE_SECONDS and snap.session_remaining == 290
    assert snap.notice == "약 5분 후 종료" and snap.playlist_count == 2
    clock.t += 290
    c.supervisor.poll()
    c.drain_events()
    snap = c.snapshot()
    assert snap.state is LiveState.SESSION_LIMIT_REACHED and snap.label.startswith("보관 안전 종료")
    assert not c.keep_awake.active and not c.active


def test_systemd_does_not_restart_after_session_complete():
    sys.path.insert(0, str(ROOT / "cloud"))
    import long_live_worker as w
    svc = (ROOT / "deploy" / "linux" / "long-live.service").read_text(encoding="utf-8")
    assert "Restart=on-failure" in svc  # exit 0 은 재시작하지 않는다
    assert w.EXIT_SESSION_COMPLETE == 0 and "RestartPreventExitStatus=3" in svc
