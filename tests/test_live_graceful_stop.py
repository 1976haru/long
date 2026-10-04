import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.live_core import LiveProcess, build_live_command
from app.live_profile import LiveConfig


class FakePopenProc:
    """q/terminate/kill 반응을 조절할 수 있는 가짜 Popen."""

    def __init__(self, exits_on):
        self.exits_on = exits_on  # "q" | "terminate" | "kill" | "never"
        self.rc = None
        self.calls = []
        self.waits = 0
        self.stdout = None
        self.stderr = None
        self.pid = 4242
        outer = self

        class Stdin:
            closed = False

            def write(self, data):
                outer.calls.append(("write", data))
                if outer.exits_on == "q" and data == "q":
                    outer.rc = 0

            def flush(self):
                pass

            def close(self):
                self.closed = True

        self.stdin = Stdin()

    def poll(self):
        return self.rc

    def wait(self, timeout=None):
        self.waits += 1
        if self.rc is None:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return self.rc

    @property
    def returncode(self):
        return self.rc

    def terminate(self):
        self.calls.append(("terminate",))
        if self.exits_on == "terminate":
            self.rc = 1

    def kill(self):
        self.calls.append(("kill",))
        if self.exits_on == "kill":
            self.rc = -9


def started(exits_on):
    fake = FakePopenProc(exits_on)
    proc = LiveProcess(["ffmpeg"], popen=lambda *a, **k: fake)
    proc.start()
    return proc, fake


def test_graceful_q_stop():
    proc, fake = started("q")
    assert proc.stop(graceful_timeout=0.01, terminate_timeout=0.01, kill_timeout=0.01) == 0
    assert proc.last_stop_method == "graceful"
    assert fake.calls == [("write", "q")]
    assert fake.stdin.closed


def test_terminate_fallback_when_q_ignored():
    proc, fake = started("terminate")
    assert proc.stop(graceful_timeout=0.01, terminate_timeout=0.01, kill_timeout=0.01) == 1
    assert proc.last_stop_method == "terminate"
    assert [c[0] for c in fake.calls] == ["write", "terminate"]


def test_kill_fallback_when_terminate_ignored():
    proc, fake = started("kill")
    assert proc.stop(graceful_timeout=0.01, terminate_timeout=0.01, kill_timeout=0.01) == -9
    assert proc.last_stop_method == "kill"
    assert [c[0] for c in fake.calls] == ["write", "terminate", "kill"]
    assert not proc.is_running()


def test_stop_already_exited_process_just_reaps():
    proc, fake = started("q")
    fake.rc = 3
    assert proc.stop() == 3
    assert proc.last_stop_method == "exited"
    assert fake.calls == []


def test_stop_survives_broken_stdin():
    proc, fake = started("terminate")

    def broken(_):
        raise BrokenPipeError()
    fake.stdin.write = broken
    assert proc.stop(graceful_timeout=0.01, terminate_timeout=0.01) == 1
    assert proc.last_stop_method == "terminate"


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def _make_source(tmp_path: Path) -> Path:
    src = tmp_path / "set.mp4"
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=320x180:r=30:d=1",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src),
    ], check=True)
    return src


def _ffmpeg_pids():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ffmpeg.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, check=False).stdout
    except OSError:
        return set()
    return {line.split('","')[1] for line in out.splitlines() if line.startswith('"ffmpeg.exe"')}


@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")
def test_real_ffmpeg_q_stop_restart_and_valid_output(tmp_path: Path):
    """실제 FFmpeg: 1초 MP4 무한 반복 → 3초 이상 → q 종료 → rc 0 → 출력 FLV를 ffprobe가 읽음 → 재시작."""
    src = _make_source(tmp_path)
    config = LiveConfig(input_path=src, ingest_url="rtmp://127.0.0.1/live", stream_key="unused-dummy",
                        video_bitrate_kbps=500, audio_bitrate_kbps=64)
    before = _ffmpeg_pids()
    for run in range(2):
        out = tmp_path / f"live{run}.flv"
        proc = LiveProcess(build_live_command(ffmpeg=Path(FFMPEG), config=config, output_target=str(out)),
                           secrets=[config.stream_key])
        proc.start()
        pid = proc._proc.pid
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            s = proc.stats()
            if s.out_time_seconds and s.out_time_seconds >= 3.0:
                break
            time.sleep(0.2)
        assert proc.is_running(), proc.recent_errors()
        assert proc.stats().out_time_seconds >= 3.0
        rc = proc.stop()
        assert proc.last_stop_method == "graceful", proc.recent_errors()
        assert rc == 0
        assert proc.return_code() == 0
        assert str(pid) not in _ffmpeg_pids()  # zombie/orphan 없음
        probe = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                               capture_output=True, text=True, check=False)
        assert probe.returncode == 0, probe.stderr
        assert float(probe.stdout.strip()) >= 3.0
    assert _ffmpeg_pids() <= before


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_real_ffmpeg_in_kill_on_exit_job(tmp_path: Path):
    import os
    src = _make_source(tmp_path)
    config = LiveConfig(input_path=src, ingest_url="rtmp://127.0.0.1/live", stream_key="unused-dummy",
                        video_bitrate_kbps=300, audio_bitrate_kbps=64)
    proc = LiveProcess(build_live_command(ffmpeg=Path(FFMPEG), config=config, output_target=str(tmp_path / "j.flv")))
    proc.start()
    try:
        if os.name == "nt":
            assert proc.in_kill_job
    finally:
        proc.stop()
