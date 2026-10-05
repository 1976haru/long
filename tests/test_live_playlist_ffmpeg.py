"""Gate 3: 실제 FFmpeg Playlist DIRECT COPY (3개 × 3회 이상, 타임스탬프/싱크/zombie)."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.live_core import LiveProcess, build_live_copy_command
from app.live_playlist import entry_durations, write_ffconcat
from app.live_profile import MODE_COPY, LiveConfig
from app.live_ready import analyze_live_ready

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")
SECONDS = 8
FAKE_KEY = "dummy-plff-0000-not-real"


@pytest.fixture(scope="module")
def three_files(tmp_path_factory):
    d = tmp_path_factory.mktemp("pl")
    out = []
    for i, (src, f) in enumerate((("testsrc2", 440), ("smptebars", 660), ("rgbtestsrc", 880))):
        p = d / f"{chr(65 + i)}_LIVE_READY.mp4"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", f"{src}=s=1920x1080:r=30:d={SECONDS}",
                        "-f", "lavfi", "-i", f"sine=frequency={f}:sample_rate=44100:duration={SECONDS}",
                        "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "1500k", "-pix_fmt", "yuv420p",
                        "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
                        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", "-shortest", str(p)], check=True)
        out.append(p)
    reports = [analyze_live_ready(p, Path(FFPROBE)) for p in out]
    assert all(r.ready for r in reports), [r.summary_lines() for r in reports]
    return d, out, reports


def manifest(d, files, reports):
    return write_ffconcat(d / "pl.ffconcat", list(zip(files, entry_durations(reports))))


def packets(path, stream):
    o = subprocess.run([FFPROBE, "-v", "error", "-select_streams", stream, "-show_entries",
                        "packet=dts_time,duration_time", "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    rows = []
    for l in o.split():
        a = l.split(",")
        try:
            rows.append((float(a[0]), float(a[0]) + float(a[1] or 0)))
        except (ValueError, IndexError):
            pass
    return rows


def test_three_files_three_rounds_clean_timestamps(three_files, tmp_path):
    d, files, reports = three_files
    m = manifest(d, files, reports)
    seg = sum(entry_durations(reports))
    total = seg * 3.4  # 3회 이상 전체 Playlist
    out = tmp_path / "pl.flv"
    cfg = LiveConfig(input_path=m, ingest_url="rtmps://x/live2", stream_key=FAKE_KEY, mode=MODE_COPY, input_format="concat")
    cmd = build_live_copy_command(ffmpeg=Path(FFMPEG), config=cfg, output_target=str(out))
    assert "libx264" not in cmd and cmd[cmd.index("-c:v") + 1] == "copy"
    fast = [a for a in cmd if a != "-re"]  # 타임스탬프 검증은 실시간 대기 없이 (pacing은 아래 테스트)
    fast = fast[:fast.index("-progress")] + ["-t", f"{total:.3f}"] + fast[fast.index("-progress"):]
    p = subprocess.run(fast, capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr
    assert "Non-monotonic" not in p.stderr and "non monotonically" not in p.stderr.lower(), p.stderr
    assert p.stderr.strip() == "", p.stderr  # warning 0
    v, a = packets(out, "v:0"), packets(out, "a:0")
    assert v[-1][0] >= seg * 3 and a[-1][0] >= seg * 3  # 3회 이상 반복
    for rows in (v, a):
        assert all(y[0] > x[0] for x, y in zip(rows, rows[1:]))  # strictly increasing DTS
    assert max(y[0] - x[0] for x, y in zip(v, v[1:])) < 0.1  # 파일 경계 점프 없음 (1프레임 33ms + pad)
    assert max(y[0] - x[0] for x, y in zip(a, a[1:])) < 0.05
    # 파일 경계마다 A/V 끝 차이: 누적되지 않음
    diffs = []
    k = 1
    while (k + 1) * (seg / 3) < total:
        t = k * seg / 3
        diffs.append(max(e for x, e in a if x < t) - max(e for x, e in v if x < t))
        k += 1
    assert len(diffs) >= 8
    assert max(abs(x) for x in diffs) < 0.06, diffs
    assert abs(diffs[-1] - diffs[0]) < 0.03, diffs  # drift 누적 없음


def _ffmpeg_alive(pid):
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
        return f'"{pid}"' in out
    return Path(f"/proc/{pid}").exists()


def test_realtime_playlist_crosses_boundary_and_stops_gracefully(three_files, tmp_path):
    d, files, reports = three_files
    m = manifest(d, files, reports)
    out = tmp_path / "rt.flv"
    cfg = LiveConfig(input_path=m, ingest_url="rtmps://x/live2", stream_key=FAKE_KEY, mode=MODE_COPY, input_format="concat")
    proc = LiveProcess(build_live_copy_command(ffmpeg=Path(FFMPEG), config=cfg, output_target=str(out)), secrets=[FAKE_KEY])
    proc.start()
    pid = proc._proc.pid
    end = time.monotonic() + 30
    while time.monotonic() < end:
        s = proc.stats()
        if s.out_time_seconds and s.out_time_seconds >= SECONDS + 1.5:  # A → B 경계 통과
            break
        time.sleep(0.2)
    s = proc.stats()
    assert proc.is_running(), proc.recent_errors()  # 파일 경계에서 FFmpeg가 끝나지 않음
    assert s.out_time_seconds >= SECONDS + 1.5
    assert s.out_time_seconds <= proc.uptime() + 1.5  # -re 실시간 pacing
    assert proc.stop() == 0 and proc.last_stop_method == "graceful"
    assert not _ffmpeg_alive(pid)
    assert not [e for e in proc.recent_errors() if "Non-monotonic" in e]


def test_cloud_worker_playlist_real_ffmpeg(three_files, tmp_path):
    """서버 worker: 검증된 이름으로 manifest를 state 폴더에 만들고 A→B→C 송출, 상태에 현재 영상/회차."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cloud"))
    import long_live_worker as w
    d, files, reports = three_files
    media, state, etc = tmp_path / "media", tmp_path / "state", tmp_path / "etc"
    for x in (media, state, etc):
        x.mkdir()
    for f in files:
        shutil.copy2(f, media / f.name)
    (etc / "live.json").write_text(json.dumps({"schema_version": 2, "media": [f.name for f in files],
                                               "play_mode": "sequential", "ingest_url": "rtmps://a.rtmps.youtube.com:443/live2",
                                               "mode": "copy", "session_mode": "continuous", "session_id": "plff1"}))
    (etc / "stream.key").write_text(FAKE_KEY + "\n")
    worker = w.Worker(config_path=etc / "live.json", key_path=etc / "stream.key", media_dir=media, state_dir=state,
                      ffmpeg=FFMPEG, ffprobe=FFPROBE, debug_output=str(tmp_path / "w.flv"))
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    try:
        end = time.monotonic() + 40
        while time.monotonic() < end:
            snap = worker.snapshot()
            if snap["current_playlist_index"] == 1:  # B 재생 중 = 경계 통과
                break
            time.sleep(0.3)
        assert snap["state"] == "RUNNING" and snap["current_playlist_index"] == 1
        assert snap["playlist_count"] == 3 and snap["current_media"] == files[1].name and snap["playlist_round"] == 1
        text = (state / "playlist.ffconcat").read_text(encoding="utf-8")
        assert text.startswith("ffconcat version 1.0\n") and text.count("file '") == 3
        assert all(str(media.resolve() / f.name) in text for f in files)  # 검증된 서버 경로만
        assert FAKE_KEY not in text
        pid = worker.proc.pid
    finally:
        worker.request_stop()
        t.join(20)
    assert worker.state == "STOPPED" and not _ffmpeg_alive(pid)
    assert FAKE_KEY not in (state / "status.json").read_text(encoding="utf-8")
