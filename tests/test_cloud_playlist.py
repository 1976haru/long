"""Gate 5: Cloud Playlist 업로드/설정 v2/구버전 호환 + worker 세션 엔진 (실제 OCI 접속 없음)."""
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from cloud_fakes import FakeRemote, make_client

from app.cloud_client import CONFIG_WRITE_CMD, KEY_WRITE_CMD, CloudError, CloudLiveController
from app.cloud_model import REMOTE_MEDIA

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cloud"))
import long_live_worker as w  # noqa: E402

FAKE_KEY = "dummy-cloudpl-0000-not-real"
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
INGEST = "rtmps://a.rtmps.youtube.com:443/live2"


def files(tmp_path, n=3, size=4000):
    out = []
    for i in range(n):
        p = tmp_path / f"{i + 1:02d}_LIVE_READY.mp4"
        p.write_bytes(bytes([i]) * size)
        out.append(p)
    return out


def test_upload_many_sha_skip_and_progress(tmp_path):
    remote = FakeRemote()
    c = make_client(tmp_path, remote)
    fs = files(tmp_path)
    c.upload_media(fs[1])  # 2번은 이미 서버에 있음
    seen = []
    res = c.upload_many(fs, progress_cb=lambda i, n, f, t: seen.append(t))
    assert [r.skipped for r in res] == [False, True, False]
    assert any(t == "2/3 이미 있음" for t in seen) and seen[-1] == "3/3 검증 완료"
    assert any(t.startswith("1/3 Cloud로 보내는 중") for t in seen)
    for f, r in zip(fs, res):
        assert remote.files[f"{REMOTE_MEDIA}/{r.remote_name}"] == f.read_bytes()
    assert not any(k.endswith(".part") for k in remote.files)
    n = len(remote.calls)
    res2 = c.upload_many(fs)
    assert all(r.skipped for r in res2)
    assert not any(x["input"] == "<stream>" for x in remote.calls[n:])  # 전부 SHA로 생략


def test_upload_many_checks_total_space_first(tmp_path):
    remote = FakeRemote()
    c = make_client(tmp_path, remote)
    fs = files(tmp_path, size=10_000)
    remote.free = 256 * 1024**2 + 15_000  # 1개는 되지만 3개 합계 + 여유는 안 됨
    with pytest.raises(CloudError, match="저장 공간"):
        c.upload_many(fs)
    assert not any(k.startswith(REMOTE_MEDIA) for k in remote.files)  # 하나도 올리지 않음


def test_upload_many_never_deletes_existing_server_files(tmp_path):
    remote = FakeRemote()
    remote.files[f"{REMOTE_MEDIA}/old_LIVE_READY.mp4"] = b"keep me"
    c = make_client(tmp_path, remote)
    c.upload_many(files(tmp_path))
    assert remote.files[f"{REMOTE_MEDIA}/old_LIVE_READY.mp4"] == b"keep me"
    assert not any(("rm " in x["args"][-1]) and "old_LIVE" in x["args"][-1] for x in remote.calls)


def test_start_live_playlist_config_v2_and_secret(tmp_path):
    remote = FakeRemote()
    c = make_client(tmp_path, remote)
    c.prepare()
    ups = c.upload_many(files(tmp_path))
    names = [u.remote_name for u in ups]
    c.start_live(remote_media=names, ingest_url=INGEST, stream_key=FAKE_KEY, sleep=lambda s: None,
                 session_mode="archive_safe", session_id="abc123")
    cfg = json.loads(remote.config)
    assert cfg == {"schema_version": 2, "media": names, "play_mode": "sequential", "ingest_url": INGEST,
                   "mode": "copy", "session_mode": "archive_safe", "session_id": "abc123"}
    assert all(FAKE_KEY not in a for x in remote.calls for a in x["args"])
    assert FAKE_KEY not in remote.config and FAKE_KEY not in "\n".join(c.detail)


def test_old_v1_worker_keeps_working_for_single_but_blocks_playlist(tmp_path):
    remote = FakeRemote()
    c = make_client(tmp_path, remote)
    c.prepare()
    remote.worker_version = 1  # 현재 운영 서버처럼 구버전 worker
    ups = c.upload_many(files(tmp_path))
    with pytest.raises(CloudError, match="업데이트"):
        c.start_live(remote_media=[u.remote_name for u in ups], ingest_url=INGEST, stream_key=FAKE_KEY)
    with pytest.raises(CloudError, match="업데이트"):
        c.start_live(remote_media=ups[0].remote_name, ingest_url=INGEST, stream_key=FAKE_KEY, session_mode="archive_safe")
    assert remote.key is None and remote.config is None  # 시도하다 서버 설정을 바꾸지 않음
    st = c.start_live(remote_media=ups[0].remote_name, ingest_url=INGEST, stream_key=FAKE_KEY, sleep=lambda s: None)
    assert st.live
    assert isinstance(json.loads(remote.config)["media"], str)  # v1 worker가 읽는 형식


def test_controller_start_async_playlist(tmp_path):
    remote = FakeRemote()
    c = make_client(tmp_path, remote)
    c.prepare()
    ctl = CloudLiveController(lambda: c)
    ctl.start_async(local=files(tmp_path), ingest_url=INGEST, stream_key=FAKE_KEY, session_mode="continuous",
                    session_id="s1")
    ctl._op.join(10)
    evs = ctl.drain_events()
    assert evs[-1][:3] == ("op", "start", True)
    assert len(json.loads(remote.config)["media"]) == 3
    assert any(e[0] == "progress" and "3/3" in e[3] for e in evs)
    assert FAKE_KEY not in repr(evs)


# ---------------- worker 설정 호환 ----------------

def test_worker_parse_config_v1_and_v2():
    v1 = w.parse_config({"media": "a.mp4", "ingest_url": INGEST, "mode": "copy"})
    assert v1["media"] == ["a.mp4"] and v1["session_mode"] == "continuous" and v1["session_id"]
    assert w.parse_config({"media": "a.mp4", "ingest_url": INGEST})["session_id"] == v1["session_id"]  # 결정적
    v2 = w.parse_config({"schema_version": 2, "media": ["a.mp4", "b.mp4"], "play_mode": "sequential",
                         "ingest_url": INGEST, "mode": "copy", "session_mode": "archive_safe", "session_id": "x1"})
    assert v2["media"] == ["a.mp4", "b.mp4"] and v2["session_mode"] == "archive_safe" and v2["session_id"] == "x1"


@pytest.mark.parametrize("bad", [
    {"media": "../etc/passwd"}, {"media": ["a.mp4", "/abs/b.mp4"]}, {"media": ["a.mp4", "b.mp4; rm -rf /"]},
    {"media": []}, {"media": [f"{i}.mp4" for i in range(21)]}, {"media": "a.mp4", "play_mode": "shuffle"},
    {"media": "a.mp4", "session_mode": "forever"}, {"media": "a.mp4", "session_id": "../x"},
    {"media": "a.mp4", "mode": "transcode"}, {"media": ["a.mp4", "it's.mp4"]},
])
def test_worker_rejects_unsafe_config(bad):
    with pytest.raises(w.ConfigError):
        w.parse_config({"ingest_url": INGEST, **bad})


def test_worker_ffconcat_escaping():
    text = w.build_ffconcat([("/opt/long-live/media/a.mp4", 10.02322)])
    assert text == "ffconcat version 1.0\nfile '/opt/long-live/media/a.mp4'\nduration 10.023220\n"
    with pytest.raises(w.ConfigError):
        w.build_ffconcat([("/x/a.mp4\nfile '/etc/shadow'", 1.0)])
    assert w.playlist_position(25.0, [10.0, 10.0]) == (0, 2)


# ---------------- worker 세션 (실제 FFmpeg + 가짜 wall clock) ----------------

class Wall:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def worker_env(tmp_path, *, session_mode="archive_safe", session_id="sessA"):
    media, state, etc = tmp_path / "media", tmp_path / "state", tmp_path / "etc"
    for d in (media, state, etc):
        d.mkdir(exist_ok=True)
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x180:r=30:d=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-g", "30", "-c:a", "aac", "-ac", "2", "-shortest",
                    str(media / "EP_LIVE_READY.mp4")], check=True)
    (etc / "stream.key").write_text(FAKE_KEY + "\n")

    def write_cfg(sid):
        (etc / "live.json").write_text(json.dumps({"schema_version": 2, "media": "EP_LIVE_READY.mp4",
                                                   "ingest_url": INGEST, "mode": "copy",
                                                   "session_mode": session_mode, "session_id": sid}))
    write_cfg(session_id)
    return dict(config_path=etc / "live.json", key_path=etc / "stream.key", media_dir=media, state_dir=state), write_cfg


def wait(cond, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        time.sleep(0.1)
    return cond()


@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg not installed")
def test_worker_archive_safe_limit_graceful_no_restart(tmp_path):
    paths, write_cfg = worker_env(tmp_path)
    wall = Wall()
    worker = w.Worker(**paths, ffmpeg=FFMPEG, ffprobe=FFPROBE, debug_output=str(tmp_path / "o.flv"), wall=wall,
                      retry_delays=(0.2,))
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("rc", worker.run()), daemon=True)
    t.start()
    assert wait(lambda: worker.state == "RUNNING")
    wall.t += w.ARCHIVE_SAFE_SECONDS - 5
    time.sleep(1.5)
    assert worker.state == "RUNNING"  # 42600 이전: 계속
    st = worker.snapshot()  # status.json은 5초 간격으로 갱신되므로 현재 값은 snapshot으로 확인
    assert st["session_mode"] == "archive_safe" and st["session_remaining"] <= 5
    wall.t += 5  # 정확히 42600
    t.join(15)
    assert result["rc"] == w.EXIT_SESSION_COMPLETE == 0  # systemd Restart=on-failure 재시작 없음
    assert worker.state == "SESSION_LIMIT_REACHED" and worker.reconnects == 0
    assert worker.proc is None and worker.last_exit_code == 0  # q 정상 종료
    sess = json.loads((paths["state_dir"] / "session.json").read_text())
    assert sess["complete"] is True and sess["session_id"] == "sessA"
    st = json.loads((paths["state_dir"] / "status.json").read_text())
    assert st["state"] == "SESSION_LIMIT_REACHED" and FAKE_KEY not in json.dumps(st)
    # systemd/재부팅으로 같은 세션이 다시 시작돼도 송출하지 않음 (12시간 초과 방지)
    again = w.Worker(**paths, ffmpeg=FFMPEG, ffprobe=FFPROBE, debug_output=str(tmp_path / "o2.flv"), wall=wall)
    assert again.run() == 0 and again.state == "SESSION_LIMIT_REACHED" and again.proc is None
    # PC의 [다음 세션 시작] = 새 session_id → 새 세션
    write_cfg("sessB")
    nxt = w.Worker(**paths, ffmpeg=FFMPEG, ffprobe=FFPROBE, debug_output=str(tmp_path / "o3.flv"), wall=wall)
    t2 = threading.Thread(target=nxt.run, daemon=True)
    t2.start()
    assert wait(lambda: nxt.state == "RUNNING")
    assert nxt.session_remaining() == pytest.approx(w.ARCHIVE_SAFE_SECONDS, abs=2)
    nxt.request_stop()
    t2.join(15)
    assert nxt.state == "STOPPED"


@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg not installed")
def test_worker_crash_restart_resumes_session_clock(tmp_path):
    """worker crash 후 systemd 재시작: 같은 세션의 시작 시각을 이어 써서 11:50이 리셋되지 않는다."""
    paths, _ = worker_env(tmp_path)
    wall = Wall()
    first = w.Worker(**paths, ffmpeg=FFMPEG, ffprobe=FFPROBE, debug_output=str(tmp_path / "a.flv"), wall=wall)
    t = threading.Thread(target=first.run, daemon=True)
    t.start()
    assert wait(lambda: first.state == "RUNNING")
    first.request_stop()
    t.join(15)
    wall.t += 10_000
    second = w.Worker(**paths, ffmpeg=FFMPEG, ffprobe=FFPROBE, debug_output=str(tmp_path / "b.flv"), wall=wall)
    t = threading.Thread(target=second.run, daemon=True)
    t.start()
    assert wait(lambda: second.state == "RUNNING")
    assert second.session_remaining() == pytest.approx(w.ARCHIVE_SAFE_SECONDS - 10_000, abs=2)
    second.request_stop()
    t.join(15)


@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg not installed")
def test_worker_continuous_ignores_timer(tmp_path):
    paths, _ = worker_env(tmp_path, session_mode="continuous")
    wall = Wall()
    worker = w.Worker(**paths, ffmpeg=FFMPEG, ffprobe=FFPROBE, debug_output=str(tmp_path / "c.flv"), wall=wall)
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    assert wait(lambda: worker.state == "RUNNING")
    wall.t += 3 * 24 * 3600
    time.sleep(1.5)
    assert worker.state == "RUNNING" and worker.session_remaining() is None
    worker.request_stop()
    t.join(15)
    assert worker.state == "STOPPED"
