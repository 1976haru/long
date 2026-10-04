import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.live_controller import run_preflight
from app.live_core import LiveProcess, build_live_copy_command, build_stream_command
from app.live_profile import MODE_COPY, MODE_TRANSCODE, LiveConfig, preset_by_key
from app.live_ready import analyze_live_ready
from app.tooling import FfmpegExecutionGuard

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
FAKE_KEY = "dummy-copy-0000-not-real"


def cfg(**kw):
    base = dict(input_path=Path("READY.mp4"), ingest_url="rtmps://a.rtmps.youtube.com:443/live2", stream_key=FAKE_KEY, mode=MODE_COPY)
    base.update(kw)
    return LiveConfig(**base)


def test_copy_command_is_pure_streamcopy():
    cmd = build_live_copy_command(ffmpeg=Path("ffmpeg"), config=cfg())
    opt = lambda k: cmd[cmd.index(k) + 1]
    assert cmd.index("-re") < cmd.index("-i") and cmd.index("-stream_loop") < cmd.index("-i")
    assert opt("-stream_loop") == "-1" and opt("-i") == "READY.mp4"
    assert opt("-c:v") == "copy" and opt("-c:a") == "copy"
    assert ["-map", "0:v:0"] == cmd[cmd.index("-map"):cmd.index("-map") + 2] and "0:a:0" in cmd
    assert opt("-progress") == "pipe:1" and opt("-f") == "flv"
    assert cmd[-1] == f"rtmps://a.rtmps.youtube.com:443/live2/{FAKE_KEY}"
    for banned in ("libx264", "-r", "-g", "-b:v", "-vf", "-filter:v", "-filter_complex", "-s", "-preset", "-x264-params", "-af"):
        assert banned not in cmd, banned
    assert not any("scale" in a for a in cmd)


def test_dispatch_by_mode():
    assert "copy" in build_stream_command(ffmpeg=Path("ffmpeg"), config=cfg())
    assert "libx264" in build_stream_command(ffmpeg=Path("ffmpeg"), config=cfg(mode=MODE_TRANSCODE))


class Rep:
    def __init__(self, ready):
        self.ready = ready
        from app.live_ready import LiveReadyIssue
        self.issues = [] if ready else [LiveReadyIssue("keyframe", "Keyframe 간격 5초 (4초 이하 필요, 2초 권장).")]


def _pf(tmp_path, *, mode=MODE_COPY, report=None, location="local", guard=None):
    from app.core import VideoInfo
    ff = tmp_path / "ffmpeg.exe"; fp = tmp_path / "ffprobe.exe"; src = tmp_path / "a_LIVE_READY.mp4"
    for p in (ff, fp, src):
        p.write_bytes(b"x")
    probe = lambda p, f: VideoInfo(Path(p), 60.0, 1, 1920, 1080, 30.0, "h264", "aac", "yuv420p", "High", 44100, 2)
    return run_preflight(ffmpeg=ff, ffprobe=fp, input_path=src, ingest_url="rtmps://a.rtmps.youtube.com:443/live2",
                         stream_key=FAKE_KEY, preset=preset_by_key("1080p30"), probe=probe, mode=mode,
                         ready_report=report, location=location, guard=guard or FfmpegExecutionGuard())


def test_auto_mode_preflight_requires_live_ready(tmp_path):
    r = _pf(tmp_path, report=Rep(True))
    assert r.ok and r.config.mode == MODE_COPY
    assert "DIRECT COPY" in r.report() and FAKE_KEY not in r.report()
    r = _pf(tmp_path, report=Rep(False))
    assert not r.ok and any("LIVE READY 파일 만들기" in e for e in r.errors())
    assert not _pf(tmp_path, report=None).ok
    assert _pf(tmp_path, mode=MODE_TRANSCODE, report=Rep(False)).ok  # 고급: 사용자가 명시한 재인코딩


def test_cloud_preflight_ignores_local_guard(tmp_path):
    g = FfmpegExecutionGuard()
    g.try_acquire("long")
    assert not _pf(tmp_path, report=Rep(True), guard=g).ok  # 로컬은 장시간 제작 중 불가
    assert _pf(tmp_path, report=Rep(True), guard=g, location="cloud").ok  # Cloud는 서버에서 실행


def _ffmpeg_mem_kb(pid):
    if os.name != "nt":
        try:
            return int(next(l for l in open(f"/proc/{pid}/status") if l.startswith("VmRSS")).split()[1])
        except (OSError, StopIteration):
            return None
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    if f'"{pid}"' not in out:
        return None
    return int(out.strip().split('","')[-1].strip('"').replace(",", "").replace(" K", "").replace("K", "").strip())


def _packets(path, stream):
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", stream, "-show_entries", "packet=dts_time,pts_time",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout
    rows = [l.split(",") for l in out.split() if l]
    return [float(r[1]) for r in rows if len(r) > 1 and r[1] not in ("", "N/A")]


@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")
def test_real_direct_copy_loops_three_times_with_sane_timestamps(tmp_path):
    src = tmp_path / "set_LIVE_READY.mp4"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=s=320x180:r=30:d=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
                    "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-shortest", str(src)], check=True)
    assert analyze_live_ready(src, Path(FFPROBE)).ready
    out = tmp_path / "copy.flv"
    cmd = build_live_copy_command(ffmpeg=Path(FFMPEG), config=cfg(input_path=src), output_target=str(out))
    proc = LiveProcess(cmd, secrets=[FAKE_KEY])
    proc.start()
    pid = proc._proc.pid
    mem = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        s = proc.stats()
        mem = _ffmpeg_mem_kb(pid) or mem
        if s.out_time_seconds and s.out_time_seconds >= 6.5:  # 2초 영상 × 3회 이상 반복
            break
        time.sleep(0.2)
    s = proc.stats()
    assert proc.is_running(), proc.recent_errors()
    assert s.out_time_seconds >= 6.5
    assert s.out_time_seconds <= proc.uptime() + 1.5  # -re 실시간 pacing (빨리 밀어내지 않음)
    rc = proc.stop()
    assert proc.last_stop_method == "graceful" and rc == 0
    assert _ffmpeg_mem_kb(pid) is None  # zombie/orphan 없음
    if mem is not None:
        assert mem < 150 * 1024, f"ffmpeg RSS {mem} KB"  # DIRECT COPY 저메모리
    v, a = _packets(out, "v:0"), _packets(out, "a:0")
    assert v and a
    assert all(y >= x for x, y in zip(v, v[1:])), "video dts must be monotonic across loops"
    assert all(y >= x for x, y in zip(a, a[1:])), "audio dts must be monotonic across loops"
    assert v[-1] >= 6.0 and a[-1] >= 6.0
    assert abs(v[-1] - a[-1]) < 0.5  # A/V 끝 시각 차이 (싱크 누적 어긋남 없음)
    gaps = [y - x for x, y in zip(v, v[1:])]
    assert max(gaps) < 0.2  # 반복 이음새 타임스탬프 점프 없음
