"""Cloud worker headless 검증 (실제 서버/네트워크 없음: 로컬 FLV 출력)."""
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cloud"))
import long_live_worker as w  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
FAKE_KEY = "dummy-worker-0000-not-real"
real = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")


def setup(tmp_path, *, media="EP001_LIVE_READY.mp4", key=FAKE_KEY, ingest="rtmps://a.rtmps.youtube.com:443/live2",
          mode="copy", make_media=True):
    etc, med, state = tmp_path / "etc", tmp_path / "media", tmp_path / "state"
    for d in (etc, med, state):
        d.mkdir(exist_ok=True)
    (etc / "live.json").write_text(json.dumps({"media": media, "ingest_url": ingest, "mode": mode}))
    if key is not None:
        (etc / "stream.key").write_text(key + "\n")
    if make_media and FFMPEG:
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc=s=320x180:r=30:d=1",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=1",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "30", "-c:a", "aac", "-ac", "2",
                        "-shortest", str(med / media)], check=True)
    elif make_media:
        (med / media).write_bytes(b"x")
    return dict(config_path=etc / "live.json", key_path=etc / "stream.key", media_dir=med, state_dir=state)


def status(paths):
    return json.loads((paths["state_dir"] / "status.json").read_text(encoding="utf-8"))


def wait(cond, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_copy_command_no_encoder():
    cmd = w.build_copy_command("ffmpeg", Path("m.mp4"), "rtmps://x/live2/k")
    assert cmd[cmd.index("-c:v") + 1] == "copy" and cmd[cmd.index("-c:a") + 1] == "copy"
    assert "-re" in cmd and cmd[cmd.index("-stream_loop") + 1] == "-1"
    assert not {"libx264", "-r", "-g", "-b:v", "-vf"} & set(cmd)


def test_retry_delays():
    assert [w.retry_delay(i) for i in range(6)] == [5, 10, 30, 60, 60, 60]


@pytest.mark.parametrize("change,msg", [
    (dict(media="../etc/passwd", make_media=False), "이름"),
    (dict(media="missing_LIVE_READY.mp4", make_media=False), "서버에 없습니다"),
    (dict(key=None), "Stream Key"),
    (dict(key="   "), "Stream Key"),
    (dict(ingest="http://x/live2"), "rtmp"),
    (dict(mode="transcode"), "DIRECT COPY"),
])
def test_config_errors_fail_without_restart_loop(tmp_path, change, msg):
    paths = setup(tmp_path, **change)
    worker = w.Worker(**paths, ffmpeg=FFMPEG or "ffmpeg")
    assert worker.run() == w.EXIT_CONFIG  # systemd RestartPreventExitStatus=3
    st = status(paths)
    assert st["state"] == "FAILED" and msg in st["last_error"]
    assert FAKE_KEY not in json.dumps(st)


@real
def test_worker_streams_reconnects_and_stops_gracefully(tmp_path, caplog):
    paths = setup(tmp_path)
    out = tmp_path / "out.flv"
    worker = w.Worker(**paths, ffmpeg=FFMPEG, retry_delays=(0.3,), debug_output=str(out))
    w.log.addFilter(w.RedactFilter(worker.secrets))
    t = threading.Thread(target=worker.run, daemon=True)
    with caplog.at_level(logging.INFO, logger="long-live"):
        t.start()
        try:
            assert wait(lambda: worker.state == "RUNNING" and worker.progress.get("out_time_us", "0") not in ("0", "N/A"))
            st = status(paths)
            assert st["state"] == "RUNNING" and st["mode"] == "DIRECT COPY" and st["media"] == "EP001_LIVE_READY.mp4"
            first_pid = worker.proc.pid
            worker.proc.kill()  # YouTube 연결 끊김/FFmpeg 비정상 종료 시뮬레이션
            assert wait(lambda: worker.reconnects >= 1 and worker.state == "RUNNING" and worker.proc and worker.proc.pid != first_pid)
            assert len(worker.reconnect_history) == 1
        finally:  # 실패해도 FFmpeg를 남기지 않는다
            worker.request_stop()
            t.join(15)
    assert not t.is_alive()
    assert worker.proc is None
    st = status(paths)
    assert st["state"] == "STOPPED" and st["reconnects"] == 1
    assert FAKE_KEY not in json.dumps(st)
    assert FAKE_KEY not in caplog.text
    assert [h[1] for h in worker.state_history][-2:] == ["STOPPING", "STOPPED"]
    assert out.exists()


def test_bounded_histories():
    worker = w.Worker(config_path="x", key_path="y", media_dir=".", state_dir=".")
    for i in range(500):
        worker.errors.append(str(i))
        worker.state_history.append(i)
        worker.reconnect_history.append(i)
    assert len(worker.errors) == 30 and len(worker.state_history) == 100 and len(worker.reconnect_history) == 100


def test_status_redacts_key_even_if_injected(tmp_path):
    paths = setup(tmp_path, make_media=False)
    worker = w.Worker(**paths)
    worker.secrets[:] = [FAKE_KEY]
    worker.last_error = f"rtmps://a/live2/{FAKE_KEY}: broken pipe"
    worker.errors.append(f"x {FAKE_KEY} y")
    worker.write_status(force=True)
    assert FAKE_KEY not in (paths["state_dir"] / "status.json").read_text()


@real
def test_self_check_cli(tmp_path):
    for d in ("media", "state", "logs"):
        (tmp_path / d).mkdir()
    p = subprocess.run([sys.executable, str(ROOT / "cloud" / "long_live_worker.py"), "--self-check", "--ffmpeg", FFMPEG,
                        "--media-dir", str(tmp_path / "media"), "--state-dir", str(tmp_path / "state"),
                        "--log-dir", str(tmp_path / "logs")], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    info = json.loads(p.stdout)
    assert info["ffmpeg"].startswith("ffmpeg version")
