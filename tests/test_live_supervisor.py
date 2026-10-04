import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.live_core import LiveProcess, build_live_command
from app.live_profile import LiveConfig
from app.live_supervisor import (
    RETRY_DELAYS, STABLE_RESET_SECONDS, LiveBusyError, LiveState, LiveSupervisor, retry_delay,
)
from app.tooling import FfmpegExecutionGuard


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeProcess:
    def __init__(self):
        self.running = False
        self.rc = None
        self.stop_calls = 0

    def start(self):
        self.running = True

    def stop(self, timeout=None):
        self.stop_calls += 1
        if self.running:
            self.running = False
            self.rc = -15
        return self.rc

    def crash(self, rc=1):
        self.running = False
        self.rc = rc

    def is_running(self):
        return self.running

    def return_code(self):
        return self.rc

    def uptime(self):
        return 0.0


def make(guard=None):
    procs = []

    def factory():
        p = FakeProcess()
        procs.append(p)
        return p

    clock = FakeClock()
    sup = LiveSupervisor(factory, guard=guard or FfmpegExecutionGuard(), clock=clock)
    return sup, procs, clock


def test_retry_delays_bounded():
    assert RETRY_DELAYS == (5, 10, 30, 60)
    assert [retry_delay(i) for i in range(7)] == [5, 10, 30, 60, 60, 60, 60]


def test_unexpected_exit_schedules_reconnect_with_backoff():
    sup, procs, clock = make()
    sup.start()
    assert sup.state is LiveState.RUNNING
    seen = []
    for expected in (5, 10, 30, 60, 60):
        procs[-1].crash()
        assert sup.poll() is LiveState.RECONNECT_WAIT
        assert sup.seconds_until_retry() == expected
        seen.append(expected)
        clock.t += expected - 0.1
        assert sup.poll() is LiveState.RECONNECT_WAIT
        clock.t += 0.1
        assert sup.poll() is LiveState.RUNNING
    assert len(procs) == 6
    assert sup.last_exit_code == 1


def test_backoff_resets_after_stable_run():
    sup, procs, clock = make()
    sup.start()
    procs[-1].crash()
    sup.poll(); clock.t += 5; sup.poll()
    procs[-1].crash()
    sup.poll()
    assert sup.seconds_until_retry() == 10
    clock.t += 10; sup.poll()
    clock.t += STABLE_RESET_SECONDS
    procs[-1].crash()
    sup.poll()
    assert sup.seconds_until_retry() == 5


def test_user_stop_does_not_reconnect():
    guard = FfmpegExecutionGuard()
    sup, procs, clock = make(guard)
    sup.start()
    sup.stop()
    assert sup.state is LiveState.STOPPED
    assert procs[0].stop_calls == 1
    clock.t += 1000
    assert sup.poll() is LiveState.STOPPED
    assert len(procs) == 1
    assert guard.owner is None


def test_user_stop_during_reconnect_wait_cancels_retry():
    sup, procs, clock = make()
    sup.start()
    procs[-1].crash()
    sup.poll()
    sup.stop()
    clock.t += 1000
    assert sup.poll() is LiveState.STOPPED
    assert len(procs) == 1


def test_start_failure_goes_failed_and_releases_guard():
    guard = FfmpegExecutionGuard()

    def factory():
        raise RuntimeError("FFmpeg를 실행할 수 없습니다")

    sup = LiveSupervisor(factory, guard=guard, clock=FakeClock())
    sup.start()
    assert sup.state is LiveState.FAILED
    assert guard.owner is None


def test_guard_blocks_live_while_long_video_running():
    guard = FfmpegExecutionGuard()
    assert guard.try_acquire("long")
    sup, procs, _ = make(guard)
    with pytest.raises(LiveBusyError):
        sup.start()
    assert procs == [] and sup.state is LiveState.STOPPED
    guard.release("long")
    sup.start()
    assert guard.owner == "live"
    assert not guard.try_acquire("long")  # LIVE 중 장시간 제작 불가
    sup.stop()
    assert guard.try_acquire("long")


def test_guard_release_by_wrong_owner_is_ignored():
    guard = FfmpegExecutionGuard()
    guard.try_acquire("live")
    guard.release("long")
    assert guard.owner == "live"


def test_double_start_rejected():
    sup, _, _ = make()
    sup.start()
    with pytest.raises(LiveBusyError):
        sup.start()
    sup.stop()


FFMPEG = shutil.which("ffmpeg")


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_live_process_smoke_loop_start_stop_restart(tmp_path: Path):
    """실제 송출 없이 로컬 파일로 출력: 1초 MP4가 무한 반복되어 실시간으로 계속 재생되는지 확인."""
    src = tmp_path / "set.mp4"
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=320x180:r=30:d=1",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src),
    ], check=True)
    config = LiveConfig(input_path=src, ingest_url="rtmp://127.0.0.1/live", stream_key="unused-fake-key",
                        video_bitrate_kbps=500, audio_bitrate_kbps=64)
    for run in range(2):  # stop 후 재시작 가능
        out = tmp_path / f"out{run}.flv"
        cmd = build_live_command(ffmpeg=Path(FFMPEG), config=config, output_target=str(out))
        proc = LiveProcess(cmd, secrets=[config.stream_key])
        proc.start()
        deadline = time.monotonic() + 15
        stats = proc.stats()
        while time.monotonic() < deadline:
            stats = proc.stats()
            if stats.out_time_seconds and stats.out_time_seconds >= 2.5:
                break
            time.sleep(0.2)
        assert proc.is_running(), proc.recent_errors()
        # 1초 입력이 2.5초 이상 출력됨 = 반복 재생 중
        assert stats.out_time_seconds and stats.out_time_seconds >= 2.5
        # 실시간 pacing: 출력 시간이 경과 시간보다 크게 앞서지 않는다
        assert stats.out_time_seconds <= proc.uptime() + 2.0
        proc.stop()
        assert not proc.is_running()
        assert proc.return_code() is not None  # wait()로 회수됨 (zombie 없음)
        assert out.exists()
