"""Cloud 예약 LIVE scheduler (cloud/long_live_worker.py) — 가짜 시계 + 가짜 runner로 상태 기계 검증.
실제 OCI/YouTube 접속 없음. 실제 FFmpeg가 있으면 로컬 FLV 출력으로 예약 종료(stop_at)까지 확인한다."""
import hashlib
import io
import json
import shutil
import subprocess
import sys
import threading
import time
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cloud"))
import long_live_worker as w  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
KEY = "sched-test-key-0000-not-real"
T0 = datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc).timestamp()  # 2026-10-10 19:00 KST


def hms(h, m, s=0):
    """KST 시각 → epoch (2026-10-10)."""
    return T0 + ((h - 19) * 3600 + m * 60 + s)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeRunner:
    """Worker 대신: start/stop 기록, stop_at(시계)에 도달하면 정상 종료(SESSION_LIMIT_REACHED)."""
    log: list = []

    def __init__(self, job, clock, *, fail=False):
        self.job, self.clock, self.fail = job, clock, fail
        self.stop_requested = False
        self.error = ""
        self._done = False
        self.worker = SimpleNamespace(state="STARTING", request_stop=self._stop_worker)

    def start(self):
        FakeRunner.log.append(("start", self.job["job_id"], self.clock()))
        self.worker.state = "FAILED" if self.fail else "RUNNING"
        self._done = self.fail

    def _stop_worker(self):
        self.worker.state = "STOPPED"
        self._done = True

    @property
    def done(self):
        if not self._done and self.clock() >= self.job["stop"]:
            self.worker.state, self._done = "SESSION_LIMIT_REACHED", True
        return self._done

    @property
    def worker_state(self):
        return self.worker.state

    @property
    def last_error(self):
        return w.LOCK_BUSY if self.fail else ""

    def stop(self):
        self.stop_requested = True
        FakeRunner.log.append(("stop", self.job["job_id"], self.clock()))
        self._stop_worker()

    def join(self, timeout=None):
        pass


def env(tmp_path, *, key=KEY, media=("girl_01_LIVE_READY.mp4", "man_001_LIVE_READY.mp4")):
    jobs, state, med = tmp_path / "jobs", tmp_path / "state", tmp_path / "media"
    for d in (jobs, state, med):
        d.mkdir(parents=True, exist_ok=True)
    (tmp_path / "stream.key").write_text(key + "\n")
    items = []
    for n, name in enumerate(media):
        data = f"video-{n}".encode() * 100
        (med / name).write_bytes(data)
        items.append({"name": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    return SimpleNamespace(jobs=jobs, state=state, media=med, key=tmp_path / "stream.key", items=items,
                           store=w.JobStore(jobs, state))


def job_dict(e, *, job_id="sj_0001", start=T0, minutes=120, key=KEY, **over):
    def z(t):
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    d = {"schema": 1, "job_id": job_id, "broadcast_id": "bcast0001", "stream_id": "stream0001",
         "scheduled_at_utc": z(start), "stop_at_utc": z(start + minutes * 60), "playlist": e.items,
         "ingest_url": "rtmps://a.rtmps.youtube.com/live2", "ingest_mode": "copy",
         "key_fingerprint": w.key_fingerprint(key), "grace_seconds": 300, "created_at": z(start - 86400)}
    d.update(over)
    return d


def scheduler(e, clock, **kw):
    FakeRunner.log = []
    return w.Scheduler(e.store, media_dir=e.media, key_path=e.key, wall=clock,
                       runner_factory=lambda job: FakeRunner(job, clock, **kw))


def state(e, jid="sj_0001"):
    return e.store.read_state(jid).get("state", "PENDING")


def starts():
    return [x for x in FakeRunner.log if x[0] == "start"]


# ---------------- job 형식 ----------------

def test_parse_job_rejects_secret_bad_names_and_long_duration(tmp_path):
    e = env(tmp_path)
    ok = w.parse_job(job_dict(e))
    assert ok["stop"] - ok["start"] == 7200 and ok["grace"] == 300 and len(ok["playlist"]) == 2
    for bad, msg in [
        (dict(stream_key=KEY), "Stream Key"),
        (dict(job_id="../x"), "ID"),
        (dict(playlist=[{"name": "../etc/passwd", "sha256": "", "size": 1}]), "이름"),
        (dict(minutes=12 * 60 + 1), "12시간"),
        (dict(ingest_url="http://x/live2"), "rtmp"),
        (dict(ingest_mode="transcode"), "DIRECT COPY"),
        (dict(scheduled_at_utc="2026-10-10 19:00"), "시간대"),
        (dict(key_fingerprint=KEY), "지문"),
    ]:
        minutes = bad.pop("minutes", 120)
        with pytest.raises(w.ConfigError, match=msg):
            w.parse_job(job_dict(e, minutes=minutes, **bad))


def test_admin_cli_add_list_cancel_and_no_secret_output(tmp_path, monkeypatch):
    e = env(tmp_path)

    def cli(*args, stdin=""):
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = w.main([*args, "--jobs-dir", str(e.jobs), "--state-dir", str(e.state), "--media-dir", str(e.media),
                         "--key-file", str(e.key)])
        return rc, json.loads(buf.getvalue().strip().splitlines()[-1])
    rc, out = cli("--add-job", stdin=json.dumps(job_dict(e)))
    assert rc == 0 and out["job"]["state"] == "PENDING" and out["job"]["media_count"] == 2 and out["job"]["manifest_ok"]
    stored = (e.jobs / "sj_0001.json").read_text(encoding="utf-8")
    assert KEY not in stored and "stream_key" not in stored
    rc, out = cli("--list-jobs")
    assert rc == 0 and [j["job_id"] for j in out["jobs"]] == ["sj_0001"] and out["worker_version"] == w.WORKER_VERSION
    rc, out = cli("--key-fingerprint")
    assert out["fingerprint"] == w.key_fingerprint(KEY) and KEY not in json.dumps(out)
    rc, out = cli("--add-job", stdin=json.dumps(job_dict(e, job_id="sj_0002", playlist=[
        {"name": "missing.mp4", "sha256": "", "size": 5}])))
    assert rc == w.EXIT_CONFIG and not out["ok"] and "Cloud에 영상이 없습니다" in out["error"]
    rc, out = cli("--cancel-job", "sj_0001")
    assert rc == 0 and out["job"]["cancel_requested"]
    rc, out = cli("--cancel-job", "nope")
    assert rc == w.EXIT_CONFIG and not out["ok"]


# ---------------- 가짜 시계 시나리오 ----------------

def test_fake_clock_full_day(tmp_path):
    e = env(tmp_path)
    e.store.add(job_dict(e), e.media)
    clock = Clock(hms(18, 55))
    s = scheduler(e, clock)
    wait = s.tick()
    assert state(e) == "PENDING" and not starts() and 0.5 <= wait <= 30  # 18:55 → 송출 안 함, 30초 이하 sleep
    clock.t = hms(18, 58, 30)
    s.tick()
    assert state(e) == "PREPARING" and e.store.read_state("sj_0001")["prepared"] and not starts()
    clock.t = hms(18, 59, 30)
    wait = s.tick()
    st = e.store.read_state("sj_0001")
    assert st["preflight"] and not starts() and wait == 30  # T-30: 최종 확인만, 송출은 예약 시각에
    clock.t = hms(18, 59, 59)
    assert s.tick() == pytest.approx(1.0) and not starts()
    clock.t = hms(19, 0)
    s.tick()
    assert len(starts()) == 1 and starts()[0][2] == hms(19, 0)
    clock.t = hms(19, 0, 5)
    s.tick()
    assert state(e) == "LIVE" and len(starts()) == 1  # 한 번만 시작
    clock.t = hms(20, 59, 59)
    assert s.tick() == w.SCHED_RUNNING_SECONDS and state(e) == "LIVE"
    clock.t = hms(21, 0)
    s.tick()
    st = e.store.read_state("sj_0001")
    assert st["state"] == "COMPLETE" and "정상 종료" in st["message"] and len(starts()) == 1
    clock.t = hms(21, 5)
    s.tick()
    assert len(starts()) == 1  # 완료 job은 다시 시작하지 않음


def test_reboot_recovers_pending_and_resumes_live_once(tmp_path):
    e = env(tmp_path)
    e.store.add(job_dict(e), e.media)
    e.store.add(job_dict(e, job_id="sj_0002", start=T0 + 86400), e.media)
    clock = Clock(hms(19, 0))
    s = scheduler(e, clock)
    s.tick()
    clock.t = hms(19, 30)
    s.tick()
    assert state(e) == "LIVE"
    # 서버 재부팅: 프로세스가 사라짐 (상태 파일만 남음) → 2분 뒤 새 scheduler
    clock.t = hms(19, 32)
    s2 = scheduler(e, clock)
    s2.tick()
    st = e.store.read_state("sj_0001")
    assert len(starts()) == 1 and st["retry_count"] == 1 and st["state"] in ("STARTING", "LIVE")
    assert state(e, "sj_0002") == "PENDING"  # 내일 예약은 그대로 대기
    # 오래 멈춘 뒤(5분 초과)에는 이어서 송출하지 않음
    clock.t = hms(19, 50)
    s3 = scheduler(e, clock)
    s3.tick()
    assert state(e) == "FAILED" and not starts()


def test_late_start_within_grace_and_missed_after(tmp_path):
    e = env(tmp_path)
    e.store.add(job_dict(e, minutes=60), e.media)  # 19:00~20:00 (같은 채널 예약은 겹칠 수 없음, v4)
    e.store.add(job_dict(e, job_id="sj_late", start=T0 + 3600), e.media)
    clock = Clock(hms(19, 3))  # 19:00 예약, 서버가 19:03에 켜짐
    s = scheduler(e, clock)
    s.tick()
    st = e.store.read_state("sj_0001")
    assert len(starts()) == 1 and st["late_seconds"] == 180 and "늦게" in st["message"]
    clock.t = hms(20, 6)  # 20:00 예약을 6분 늦게 → MISSED (자동 시작 안 함)
    s2 = scheduler(e, clock)
    s2.tick()
    assert state(e, "sj_late") == "MISSED" and all(x[1] != "sj_late" for x in starts())


def test_cancel_before_start_and_while_live(tmp_path):
    e = env(tmp_path)
    e.store.add(job_dict(e), e.media)
    e.store.add(job_dict(e, job_id="sj_0002", start=T0 + 86400), e.media)
    e.store.request_cancel("sj_0002")
    clock = Clock(hms(19, 0))
    s = scheduler(e, clock)
    s.tick()
    assert state(e, "sj_0002") == "CANCELLED" and len(starts()) == 1
    e.store.request_cancel("sj_0001")  # 송출 중 [방송 중지]
    clock.t = hms(19, 10)
    s.tick()
    assert ("stop", "sj_0001", hms(19, 10)) in FakeRunner.log and state(e) == "CANCELLED"
    clock.t = hms(19, 11)
    s.tick()
    assert len(starts()) == 1


def test_preflight_failures_do_not_start(tmp_path):
    e = env(tmp_path, key="different-key-0000")
    e.store.add(job_dict(e), e.media)  # key_fingerprint는 KEY 기준
    clock = Clock(hms(19, 0))
    s = scheduler(e, clock)
    s.tick()
    st = e.store.read_state("sj_0001")
    assert st["state"] == "FAILED" and "Stream Key" in st["message"] and not starts()
    assert "different-key-0000" not in json.dumps(st)
    # SHA256이 바뀐 영상 → T-120 확인에서 실패
    e2 = env(tmp_path / "b")
    e2.store.add(job_dict(e2), e2.media)
    (e2.media / "man_001_LIVE_READY.mp4").write_bytes(b"y" * len(b"video-1") * 100)
    clock.t = hms(18, 58, 30)
    s2 = scheduler(e2, clock)
    s2.tick()
    assert state(e2) == "FAILED" and "SHA256" in e2.store.read_state("sj_0001")["message"]


def test_other_live_holding_lock_marks_failed(tmp_path):
    e = env(tmp_path)
    e.store.add(job_dict(e), e.media)
    clock = Clock(hms(19, 0))
    s = scheduler(e, clock, fail=True)  # runner의 Worker가 worker.lock을 못 잡음
    s.tick()
    clock.t = hms(19, 0, 5)
    s.tick()
    st = e.store.read_state("sj_0001")
    assert st["state"] == "FAILED" and "다른 LIVE" in st["message"]


def test_overlapping_jobs_second_is_not_started(tmp_path):
    e = env(tmp_path)
    e.store.add(job_dict(e), e.media)
    # v4부터 같은 채널 겹침은 저장 단계에서 거부된다. v3 시절에 이미 저장된 겹침 job도 실행 단계에서 막히는지 확인
    with pytest.raises(w.ConfigError, match="겹치는"):
        e.store.add(job_dict(e, job_id="sj_0002", start=T0 + 1800), e.media)
    (e.jobs / "sj_0002.json").write_text(json.dumps(job_dict(e, job_id="sj_0002", start=T0 + 1800)), encoding="utf-8")
    clock = Clock(hms(19, 0))
    s = scheduler(e, clock)
    s.tick()
    clock.t = hms(19, 30)
    s.tick()
    assert len(starts()) == 1 and state(e, "sj_0002") == "FAILED"


def test_duplicate_guards(tmp_path):
    a, b = w.FileLock(tmp_path / "worker.lock"), w.FileLock(tmp_path / "worker.lock")
    assert a.acquire() and not b.acquire()
    a.release()
    assert b.acquire()
    b.release()
    # scheduler 두 번째 인스턴스는 바로 종료 (exit 3, systemd 재시작 안 함)
    args = SimpleNamespace(state_dir=str(tmp_path / "state"), jobs_dir=str(tmp_path / "jobs"), media_dir=str(tmp_path),
                           key_file=str(tmp_path / "k"), ffmpeg="ffmpeg")
    stop = threading.Event()
    t = threading.Thread(target=w.run_scheduler, args=(args,), kwargs={"stop_event": stop}, daemon=True)
    t.start()
    end = time.monotonic() + 5
    while not (tmp_path / "state" / "scheduler.json").exists() and time.monotonic() < end:
        time.sleep(0.05)
    try:
        assert w.run_scheduler(args, stop_event=threading.Event()) == w.EXIT_CONFIG
    finally:
        stop.set()
        t.join(10)
    assert not t.is_alive()


def test_worker_deadline_uses_wall_clock(tmp_path):
    clock = Clock(1000.0)
    wk = w.Worker(config_path=tmp_path / "c.json", key_path=tmp_path / "k", media_dir=tmp_path, state_dir=tmp_path,
                  wall=clock, deadline_wall=1060.0)
    wk.session_wall_start = 1000.0
    assert wk.session_remaining() == 60 and not wk._session_over()
    clock.t = 1060.0
    assert wk.session_remaining() == 0 and wk._session_over()
    plain = w.Worker(config_path=tmp_path / "c.json", key_path=tmp_path / "k", media_dir=tmp_path, state_dir=tmp_path)
    assert plain.session_remaining() is None  # 기존 계속 방송: 종료 시각 없음


def test_manual_worker_blocked_while_lock_held(tmp_path):
    etc, med, st = tmp_path / "etc", tmp_path / "media", tmp_path / "state"
    for d in (etc, med, st):
        d.mkdir()
    (med / "a.mp4").write_bytes(b"x")
    (etc / "live.json").write_text(json.dumps({"media": "a.mp4", "ingest_url": "rtmps://a.rtmps.youtube.com/live2"}))
    (etc / "stream.key").write_text(KEY)
    holder = w.FileLock(st / "worker.lock")
    assert holder.acquire()
    try:
        wk = w.Worker(config_path=etc / "live.json", key_path=etc / "stream.key", media_dir=med, state_dir=st)
        assert wk.run() == w.EXIT_CONFIG and wk.state == "FAILED" and "다른 LIVE" in wk.last_error
    finally:
        holder.release()


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_real_ffmpeg_job_runs_playlist_and_stops_at_deadline(tmp_path, caplog):
    e = env(tmp_path, media=())
    names = ["girl_01_LIVE_READY.mp4", "man_001_LIVE_READY.mp4"]
    items = []
    for n, name in enumerate(names):
        p = e.media / name
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", f"testsrc=s=320x180:r=30:d={1 + n}",
                        "-f", "lavfi", "-i", f"sine=frequency={440 + 100 * n}:sample_rate=44100:duration={1 + n}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "60", "-c:a", "aac", "-ac", "2",
                        "-shortest", str(p)], check=True)
        items.append({"name": name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "size": p.stat().st_size})
    out = tmp_path / "out.flv"
    now = time.time()
    e.store.add(job_dict(e, start=now - 1, minutes=1, playlist=items), e.media)
    # 1분짜리 job을 4초 뒤에 끝나도록 (stop_at만 앞당긴 명세)
    spec = json.loads((e.jobs / "sj_0001.json").read_text(encoding="utf-8"))
    spec["stop_at_utc"] = datetime.fromtimestamp(now + 4, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (e.jobs / "sj_0001.json").write_text(json.dumps(spec), encoding="utf-8")
    secrets = []
    w.log.addFilter(w.RedactFilter(secrets))

    def factory(job):
        return w.JobRunner(job, store=e.store, media_dir=e.media, key_path=e.key, lock_path=e.state / "worker.lock",
                           ffmpeg=FFMPEG, secrets=secrets, worker_kwargs={"debug_output": str(out)})
    s = w.Scheduler(e.store, media_dir=e.media, key_path=e.key, runner_factory=factory)
    end = time.monotonic() + 40
    try:
        import logging
        with caplog.at_level(logging.INFO, logger="long-live"):
            while time.monotonic() < end and state(e) not in w.JOB_TERMINAL:
                s.tick()
                time.sleep(0.2)
    finally:
        s.shutdown()
    st = e.store.read_state("sj_0001")
    assert st["state"] == "COMPLETE", st
    assert out.exists() and out.stat().st_size > 0
    run_dir = e.state / "jobs" / "sj_0001"
    assert (run_dir / "playlist.ffconcat").exists()  # 2개 → concat 반복 (새 긴 파일을 만들지 않음)
    assert KEY not in caplog.text and KEY not in json.dumps(st)
    lk = w.FileLock(e.state / "worker.lock")
    assert lk.acquire()  # 송출이 끝나면 잠금이 풀려 있어야 함
    lk.release()
