"""여러 채널 동시 Cloud LIVE (worker v4) — 실제 OCI/YouTube 없이 검증.

- worker: 채널별 경로/잠금, 서버 전체 동시 송출 2개, 세 번째 거부, 독립 중지/재접속, 자원 guard
- scheduler: 다른 채널 같은 시각 허용 / 같은 채널 겹침 차단 / 전체 2개
- CloudClient: 채널별 key 파일·서비스·상태 명령, 기존 1채널 호출은 그대로
- PC: 채널 Profile 저장/migration, 채널별 Stream Key·OAuth token 분리, 대역폭 계산
실제 FFmpeg가 있으면 로컬 FLV 출력으로 2채널을 동시에 돌려 본다 (YouTube 송출 없음).
"""
import hashlib
import json
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cloud"))
import long_live_worker as w  # noqa: E402

from app import cloud_client as cc  # noqa: E402
from app.cloud_model import CONCURRENT_BUSY, REMOTE_MEDIA, REMOTE_WORKER  # noqa: E402
from cloud_fakes import FakeRemote, make_client  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
real = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
KEYS = {"default": "legacy-key-0000-not-real", "senior": "senior-key-1111-not-real",
        "chili": "chili-key-2222-not-real", "third": "third-key-3333-not-real"}
PLENTY = {"mem_available_bytes": 4 * 1024**3, "disk_free_bytes": 50 * 1024**3, "load1": 0.1, "cpus": 2,
          "ffmpeg_count": 0}


def wait(cond, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def make_media(path: Path, seconds=1, freq=440):
    if FFMPEG:
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", f"testsrc=s=320x180:r=30:d={seconds}",
                        "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=44100:duration={seconds}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "30", "-c:a", "aac", "-ac", "2",
                        "-shortest", str(path)], check=True)
    else:
        path.write_bytes(b"x" * 100)


class Server:
    """tmp 폴더 = 서버 /etc/long-live, /opt/long-live/{media,state,logs}."""

    def __init__(self, root: Path, profiles=("senior", "chili", "third"), media=("shared_LIVE_READY.mp4",)):
        self.etc, self.media, self.state, self.logs = root / "etc", root / "media", root / "state", root / "logs"
        for d in (self.etc, self.media, self.state, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        for n, name in enumerate(media):
            make_media(self.media / name, freq=440 + 100 * n)
        self.media_names = list(media)
        for p in ("default",) + tuple(profiles):
            self.write_channel(p, list(media))

    def paths(self, p):
        return w.profile_paths(p, etc_dir=self.etc, state_dir=self.state, log_dir=self.logs)

    def write_channel(self, p, media, key=None):
        paths = self.paths(p)
        paths["config"].parent.mkdir(parents=True, exist_ok=True)
        paths["config"].write_text(json.dumps({"schema_version": 2, "media": media[0] if len(media) == 1 else media,
                                               "ingest_url": "rtmps://a.rtmps.youtube.com/live2", "mode": "copy",
                                               "session_id": f"s_{p}"}), encoding="utf-8")
        paths["key"].write_text((key or KEYS[p]) + "\n", encoding="utf-8")

    def worker(self, p, out: Path, **kw):
        paths = self.paths(p)
        kw.setdefault("resources", lambda media_dir: dict(PLENTY))
        return w.Worker(config_path=paths["config"], key_path=paths["key"], media_dir=self.media,
                        state_dir=paths["state"], lock_path=paths["lock"], ffmpeg=FFMPEG or "ffmpeg",
                        debug_output=str(out), retry_delays=(0.3,), profile=p, slot_dir=self.state / "slots", **kw)

    def status(self, p):
        return json.loads((self.paths(p)["state"] / "status.json").read_text(encoding="utf-8"))


class Running:
    def __init__(self, worker):
        self.worker = worker
        self.rc = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        self.rc = self.worker.run()

    def streaming(self):
        wk = self.worker
        return wk.state == "RUNNING" and wk.proc is not None and wk.progress.get("out_time_us", "0") not in ("0", "N/A")

    def alive(self):
        """재접속 뒤: 로컬 테스트 출력 파일은 FFmpeg가 덮어쓰기 확인을 기다리므로 진행값 대신 프로세스 생존만 본다."""
        wk = self.worker
        proc = wk.proc
        return wk.state == "RUNNING" and proc is not None and proc.poll() is None

    def stop(self):
        self.worker.request_stop()
        self.thread.join(20)
        return not self.thread.is_alive()


# ---------------- 경로 / ID ----------------

def test_profile_paths_isolated_and_default_is_legacy():
    d = w.profile_paths("default")
    assert str(d["config"]).replace("\\", "/") == "/etc/long-live/live.json"
    assert str(d["key"]).replace("\\", "/") == "/etc/long-live/stream.key"
    assert str(d["lock"]).replace("\\", "/") == "/opt/long-live/state/worker.lock"
    s, c = w.profile_paths("senior"), w.profile_paths("chili")
    assert str(s["key"]).replace("\\", "/") == "/etc/long-live/channels/senior/stream.key"
    assert str(s["state"]).replace("\\", "/") == "/opt/long-live/state/channels/senior"
    assert str(s["logs"]).replace("\\", "/") == "/opt/long-live/logs/channels/senior"
    for k in ("config", "key", "state", "lock", "logs", "jobs"):
        assert s[k] != c[k] and s[k] != d[k]
    assert w.service_name("default") == "long-live.service" and w.service_name("senior") == "long-live@senior.service"
    for bad in ("../x", "Senior", "1abc", "a-b", "a" * 33, "a b", "a/b"):
        with pytest.raises(w.ConfigError):
            w.profile_paths(bad)


def test_resource_guard_rules():
    assert w.resource_problem({}, others_live=0) == ""  # 첫 번째 LIVE는 막지 않는다 (기존 동작)
    assert w.resource_problem(dict(PLENTY), others_live=1) == ""
    assert w.resource_problem(dict(PLENTY), others_live=2) == w.CONCURRENT_BUSY
    assert "메모리" in w.resource_problem(dict(PLENTY, mem_available_bytes=50 * 1024**2), others_live=1)
    assert "저장 공간" in w.resource_problem(dict(PLENTY, disk_free_bytes=10), others_live=1)
    assert "부하" in w.resource_problem(dict(PLENTY, load1=9.0), others_live=1)
    assert w.resource_problem(dict(PLENTY, ffmpeg_count=2), others_live=1) == w.CONCURRENT_BUSY
    assert "최대 2개" in w.CONCURRENT_BUSY and "LIVE 2개가 실행 중" in w.CONCURRENT_BUSY


def test_slot_selection_has_no_false_rejection_under_race(tmp_path):
    """같은 시각 예약 2개가 동시에 시작해도 둘 다 자리를 얻는다 (guard 잠금으로 순서를 맞춤)."""
    for n in range(15):
        slot_dir = tmp_path / f"slots{n}"
        got, barrier = [], threading.Barrier(2)

        def take(p):
            barrier.wait()
            got.append(w.acquire_live_slot(slot_dir, profile=p, media_dir=tmp_path, resources=lambda m: dict(PLENTY)))
        ts = [threading.Thread(target=take, args=(p,)) for p in ("senior", "chili")]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        assert all(lk is not None for lk, _ in got), got
        third, msg = w.acquire_live_slot(slot_dir, profile="third", media_dir=tmp_path, resources=lambda m: dict(PLENTY))
        assert third is None and msg == w.CONCURRENT_BUSY
        assert sorted(o["profile_id"] for o in w.slot_owners(slot_dir)) == ["chili", "senior"]
        for lk, _ in got:
            lk.release()
        assert w.slot_owners(slot_dir) == []


# ---------------- 실제 FFmpeg (로컬 FLV, YouTube 없음) ----------------

@real
def test_two_channels_concurrent_third_blocked_and_independent_stop(tmp_path, caplog):
    srv = Server(tmp_path)
    a = Running(srv.worker("senior", tmp_path / "a.flv"))
    b = Running(srv.worker("chili", tmp_path / "b.flv"))
    try:
        assert wait(a.streaming) and wait(b.streaming)  # A+B 동시 LIVE
        assert a.worker.proc.pid != b.worker.proc.pid
        # 세 번째 채널: 시작 거부, A/B는 그대로
        c = srv.worker("third", tmp_path / "c.flv")
        assert c.run() == w.EXIT_CONFIG and c.state == "FAILED" and c.last_error == w.CONCURRENT_BUSY
        st = srv.status("third")
        assert st["state"] == "FAILED" and "최대 2개" in st["last_error"] and st["profile_id"] == "third"
        assert a.streaming() and b.streaming()
        # A stop → B 유지
        b_pid = b.worker.proc.pid
        assert a.stop() and a.worker.state == "STOPPED" and a.rc == w.EXIT_OK
        time.sleep(1.0)
        assert b.streaming() and b.worker.proc.pid == b_pid and b.worker.reconnects == 0
        # 자리가 비면 세 번째 채널 시작 가능
        c2 = Running(srv.worker("third", tmp_path / "c2.flv"))
        assert wait(c2.streaming)
        # B stop → C 유지
        c_pid = c2.worker.proc.pid
        assert b.stop()
        time.sleep(1.0)
        assert c2.streaming() and c2.worker.proc.pid == c_pid
        assert c2.stop()
    finally:
        for r in (a, b):
            r.stop()
    for p, out in (("senior", "a.flv"), ("chili", "b.flv"), ("third", "c2.flv")):
        assert (tmp_path / out).stat().st_size > 0
        st = srv.status(p)
        assert st["state"] == "STOPPED" and st["profile_id"] == p
        assert not any(k in json.dumps(st) for k in KEYS.values())
    assert w.slot_owners(srv.state / "slots") == []


@real
def test_crash_isolation_and_reconnect_counts_per_channel(tmp_path):
    srv = Server(tmp_path)
    a = Running(srv.worker("senior", tmp_path / "a.flv"))
    b = Running(srv.worker("chili", tmp_path / "b.flv"))
    try:
        assert wait(a.streaming) and wait(b.streaming)
        a_pid, b_pid = a.worker.proc.pid, b.worker.proc.pid
        a.worker.proc.kill()  # A FFmpeg crash → A만 재접속
        assert wait(lambda: a.worker.reconnects == 1 and a.alive() and a.worker.proc.pid != a_pid)
        assert b.worker.reconnects == 0 and b.worker.proc.pid == b_pid and b.streaming()
        a_pid = a.worker.proc.pid
        b.worker.proc.kill()  # B crash → B만 재접속
        assert wait(lambda: b.worker.reconnects == 1 and b.alive() and b.worker.proc.pid != b_pid)
        assert a.worker.reconnects == 1 and a.worker.proc.pid == a_pid and a.alive()
        assert srv.status("senior")["reconnects"] == 1 and srv.status("chili")["reconnects"] == 1  # 채널별 기록
    finally:
        assert a.stop() and b.stop()


@real
def test_same_channel_duplicate_blocked_other_keeps_running(tmp_path):
    srv = Server(tmp_path)
    a = Running(srv.worker("senior", tmp_path / "a.flv"))
    try:
        assert wait(a.streaming)
        dup = srv.worker("senior", tmp_path / "dup.flv")
        assert dup.run() == w.EXIT_CONFIG and "다른 LIVE" in dup.last_error
        assert a.streaming()
        # 자원 부족이면 두 번째 채널만 거부 (첫 번째는 계속)
        low = srv.worker("chili", tmp_path / "b.flv", resources=lambda m: dict(PLENTY, mem_available_bytes=10 * 1024**2))
        assert low.run() == w.EXIT_CONFIG and "메모리" in low.last_error and "첫 번째 LIVE는 계속" in low.last_error
        assert a.streaming() and a.worker.reconnects == 0
    finally:
        assert a.stop()


@real
def test_state_and_manifest_isolated_per_channel_shared_media(tmp_path):
    names = ("shared_A_LIVE_READY.mp4", "shared_B_LIVE_READY.mp4")
    srv = Server(tmp_path, media=names)
    srv.write_channel("chili", [names[1], names[0]])  # 같은 공용 영상, 다른 순서
    a = Running(srv.worker("senior", tmp_path / "a.flv"))
    b = Running(srv.worker("chili", tmp_path / "b.flv"))
    try:
        assert wait(a.streaming) and wait(b.streaming)
        ma = (srv.paths("senior")["state"] / "playlist.ffconcat").read_text(encoding="utf-8")
        mb = (srv.paths("chili")["state"] / "playlist.ffconcat").read_text(encoding="utf-8")
        assert ma.index(names[0]) < ma.index(names[1]) and mb.index(names[1]) < mb.index(names[0])
        assert sorted(x.name for x in srv.media.iterdir()) == sorted(names)  # 공용 media, 채널별 복사 없음
        sa, sb = srv.paths("senior")["state"], srv.paths("chili")["state"]
        assert (sa / "session.json").is_file() and (sb / "session.json").is_file() and sa != sb
        assert not (srv.state / "status.json").exists()  # 기본 채널 상태 파일은 건드리지 않음
    finally:
        assert a.stop() and b.stop()


@real
def test_cli_profile_run_uses_channel_paths(tmp_path):
    """`--run --profile senior` = long-live@senior.service 의 ExecStart (FFmpeg 대신 설정 오류로 빠르게 확인)."""
    srv = Server(tmp_path, profiles=("senior",))
    srv.paths("senior")["key"].unlink()  # key 없음 → 이 채널만 설정 오류 (exit 3, systemd 재시작 안 함)
    p = subprocess.run([sys.executable, str(ROOT / "cloud" / "long_live_worker.py"), "--run", "--profile", "senior",
                        "--etc-dir", str(srv.etc), "--key-file", str(srv.etc / "stream.key"), "--media-dir",
                        str(srv.media), "--state-dir", str(srv.state), "--log-dir", str(srv.logs), "--ffmpeg", FFMPEG],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert p.returncode == w.EXIT_CONFIG
    st = srv.status("senior")
    assert st["state"] == "FAILED" and "Stream Key" in st["last_error"] and st["profile_id"] == "senior"
    assert (srv.logs / "channels" / "senior" / "worker.log").is_file()
    assert not (srv.state / "status.json").exists()
    p = subprocess.run([sys.executable, str(ROOT / "cloud" / "long_live_worker.py"), "--run", "--profile", "../x"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert p.returncode == w.EXIT_CONFIG


# ---------------- scheduler ----------------

T0 = datetime(2026, 10, 10, 21, 0, tzinfo=timezone.utc).timestamp()  # 2026-10-11 06:00 KST


def zulu(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sched_env(tmp_path, profiles=("default", "senior", "chili", "third")):
    srv = Server(tmp_path, profiles=tuple(p for p in profiles if p != "default"))
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    data = (srv.media / srv.media_names[0]).read_bytes()
    item = {"name": srv.media_names[0], "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    return SimpleNamespace(srv=srv, jobs=jobs, store=w.JobStore(jobs, srv.state), item=item)


def sjob(e, jid, profile, start=T0, minutes=360, key=None):
    d = {"schema": 1, "job_id": jid, "broadcast_id": "b" + jid[-4:], "stream_id": "s" + jid[-4:],
         "scheduled_at_utc": zulu(start), "stop_at_utc": zulu(start + minutes * 60), "playlist": [e.item],
         "ingest_url": "rtmps://a.rtmps.youtube.com/live2", "ingest_mode": "copy",
         "key_fingerprint": w.key_fingerprint(key or KEYS[profile]), "grace_seconds": 300, "created_at": zulu(T0 - 3600)}
    if profile != "default":
        d["profile_id"] = profile
    return d


class FakeRunner:
    log: list = []

    def __init__(self, job, clock, fail_profiles=()):
        self.job, self.clock = job, clock
        self.fail = job.get("profile_id") in fail_profiles
        self.stop_requested, self.error, self._done = False, "", False
        self.worker = SimpleNamespace(state="STARTING", request_stop=self._halt)

    def start(self):
        FakeRunner.log.append(("start", self.job["job_id"], self.job["profile_id"]))
        self.worker.state = "FAILED" if self.fail else "RUNNING"
        self._done = self.fail

    def _halt(self):
        self.worker.state, self._done = "STOPPED", True

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
        return "FFmpeg crash (테스트)" if self.fail else ""

    def stop(self):
        self.stop_requested = True
        self._halt()

    def join(self, timeout=None):
        pass


def make_sched(e, clock, **kw):
    FakeRunner.log = []
    return w.Scheduler(e.store, media_dir=e.srv.media, key_path=e.srv.etc / "stream.key", wall=clock,
                       etc_dir=e.srv.etc, runner_factory=lambda job: FakeRunner(job, clock, **kw))


def jstate(e, jid):
    return e.store.read_state(jid).get("state", "PENDING")


def test_schedule_overlap_rules(tmp_path):
    e = sched_env(tmp_path)
    e.store.add(sjob(e, "sj_sen1", "senior"), e.srv.media)  # senior 06:00~12:00
    with pytest.raises(w.ConfigError, match="같은 채널"):
        e.store.add(sjob(e, "sj_sen2", "senior", start=T0 + 3 * 3600, minutes=120), e.srv.media)  # 09:00~11:00 겹침
    e.store.add(sjob(e, "sj_chi1", "chili"), e.srv.media)  # 다른 채널 같은 시각 → 허용
    with pytest.raises(w.ConfigError, match="최대 2개"):
        e.store.add(sjob(e, "sj_thr1", "third", start=T0 + 3600, minutes=60), e.srv.media)  # 세 번째 동시 → 거부
    e.store.add(sjob(e, "sj_thr2", "third", start=T0 + 6 * 3600, minutes=60), e.srv.media)  # 12:00 이후 → 허용
    e.store.add(sjob(e, "sj_sen3", "senior", start=T0 + 6 * 3600, minutes=60), e.srv.media)  # 같은 채널 연속(경계) 허용
    e.store.request_cancel("sj_chi1")  # 취소된 예약은 겹침 계산에서 빠짐
    e.store.add(sjob(e, "sj_thr3", "third", start=T0 + 3600, minutes=60), e.srv.media)
    listing = {j["job_id"]: j for j in e.store.listing(e.srv.media)}
    assert listing["sj_sen1"]["profile_id"] == "senior" and listing["sj_thr3"]["profile_id"] == "third"
    stored = (e.jobs / "sj_sen1.json").read_text(encoding="utf-8")
    assert json.loads(stored)["profile_id"] == "senior" and KEYS["senior"] not in stored


def test_scheduler_runs_two_channels_at_same_time_independently(tmp_path):
    e = sched_env(tmp_path)
    e.store.add(sjob(e, "sj_sen1", "senior"), e.srv.media)
    e.store.add(sjob(e, "sj_chi1", "chili", minutes=120), e.srv.media)
    clock = SimpleNamespace(t=T0)
    s = make_sched(e, lambda: clock.t)
    s.tick()
    assert sorted(x[2] for x in FakeRunner.log) == ["chili", "senior"] and len(s.runs) == 2
    clock.t = T0 + 60
    s.tick()
    assert jstate(e, "sj_sen1") == "LIVE" and jstate(e, "sj_chi1") == "LIVE"
    assert sorted(r.job["profile_id"] for r in s.runs.values()) == ["chili", "senior"]
    # chili 예약 종료(08:00) → senior는 계속
    clock.t = T0 + 120 * 60
    s.tick()
    assert jstate(e, "sj_chi1") == "COMPLETE" and jstate(e, "sj_sen1") == "LIVE" and list(s.runs) == ["sj_sen1"]
    # senior 취소 → senior만 중지
    e.store.add(sjob(e, "sj_chi2", "chili", start=T0 + 3 * 3600, minutes=60), e.srv.media)
    clock.t = T0 + 3 * 3600
    s.tick()
    assert jstate(e, "sj_chi2") in ("STARTING", "LIVE")
    e.store.request_cancel("sj_sen1")
    clock.t += 10
    s.tick()
    assert jstate(e, "sj_sen1") == "CANCELLED" and jstate(e, "sj_chi2") == "LIVE"


def test_scheduler_failure_isolation_and_third_runtime_block(tmp_path):
    e = sched_env(tmp_path)
    e.store.add(sjob(e, "sj_sen1", "senior"), e.srv.media)
    e.store.add(sjob(e, "sj_chi1", "chili"), e.srv.media)
    # v3 시절/수동으로 들어간 세 번째 동시 job (저장 단계 검사를 거치지 않음) → 실행 단계에서 막힘
    (e.jobs / "sj_thr1.json").write_text(json.dumps(sjob(e, "sj_thr1", "third")), encoding="utf-8")
    clock = SimpleNamespace(t=T0)
    s = make_sched(e, lambda: clock.t, fail_profiles=("senior",))
    s.tick()
    clock.t += 5
    s.tick()
    assert jstate(e, "sj_sen1") == "FAILED"  # senior crash
    assert jstate(e, "sj_chi1") == "LIVE"  # chili 계속
    st = e.store.read_state("sj_thr1")
    assert st["state"] == "FAILED" and "최대 2개" in st["message"]


def test_scheduler_uses_channel_key_and_wrong_key_fails_only_that_channel(tmp_path):
    e = sched_env(tmp_path)
    e.store.add(sjob(e, "sj_sen1", "senior"), e.srv.media)
    e.store.add(sjob(e, "sj_chi1", "chili", key="some-other-key"), e.srv.media)  # 지문이 chili key와 다름
    clock = SimpleNamespace(t=T0 - 30)
    s = make_sched(e, lambda: clock.t)
    s.tick()
    st = e.store.read_state("sj_chi1")
    assert st["state"] == "FAILED" and "Stream Key" in st["message"]
    assert KEYS["chili"] not in json.dumps(st)
    clock.t = T0
    s.tick()
    assert [x[2] for x in FakeRunner.log] == ["senior"]  # senior는 자기 key로 정상 시작


def test_scheduler_status_lists_running_channels(tmp_path):
    e = sched_env(tmp_path)
    e.store.add(sjob(e, "sj_sen1", "senior"), e.srv.media)
    e.store.add(sjob(e, "sj_def1", "default"), e.srv.media)
    clock = SimpleNamespace(t=T0)
    s = make_sched(e, lambda: clock.t)
    s.status_path = e.srv.state / "scheduler.json"
    s.tick()
    d = json.loads(s.status_path.read_text(encoding="utf-8"))
    assert sorted(j["profile_id"] for j in d["running_jobs"]) == ["default", "senior"] and d["worker_version"] == "4"
    assert d["running_job"] in ("sj_sen1", "sj_def1")  # 기존 필드 호환


@real
def test_real_scheduler_two_channel_jobs_complete_at_deadline(tmp_path):
    e = sched_env(tmp_path, profiles=("default", "senior", "chili"))
    now = time.time()
    for jid, p in (("sj_sen1", "senior"), ("sj_chi1", "chili")):
        e.store.add(sjob(e, jid, p, start=now - 1, minutes=1), e.srv.media)
        spec = json.loads((e.jobs / f"{jid}.json").read_text(encoding="utf-8"))
        spec["stop_at_utc"] = zulu(now + 5)
        (e.jobs / f"{jid}.json").write_text(json.dumps(spec), encoding="utf-8")
    secrets = []
    args = SimpleNamespace(state_dir=str(e.srv.state), jobs_dir=str(e.jobs), media_dir=str(e.srv.media),
                           key_file=str(e.srv.etc / "stream.key"), etc_dir=str(e.srv.etc), ffmpeg=FFMPEG)
    store = e.store

    def factory(job):
        p = w.profile_paths(job["profile_id"], etc_dir=e.srv.etc, state_dir=e.srv.state)
        return w.JobRunner(job, store=store, media_dir=e.srv.media, key_path=p["key"], lock_path=p["lock"],
                           ffmpeg=FFMPEG, secrets=secrets,
                           worker_kwargs={"slot_dir": e.srv.state / "slots",
                                          "debug_output": str(tmp_path / f"{job['profile_id']}.flv")})
    s = w.Scheduler(store, media_dir=args.media_dir, key_path=args.key_file, etc_dir=args.etc_dir,
                    runner_factory=factory)
    end = time.monotonic() + 45
    both_live = False
    try:
        while time.monotonic() < end and not all(jstate(e, j) in w.JOB_TERMINAL for j in ("sj_sen1", "sj_chi1")):
            s.tick()
            both_live = both_live or len(w.slot_owners(e.srv.state / "slots")) == 2
            time.sleep(0.2)
    finally:
        s.shutdown()
    assert both_live
    for jid, p in (("sj_sen1", "senior"), ("sj_chi1", "chili")):
        st = e.store.read_state(jid)
        assert st["state"] == "COMPLETE", st
        assert (tmp_path / f"{p}.flv").stat().st_size > 0
        assert (e.srv.state / "channels" / p / "jobs" / jid / "status.json").is_file()  # 채널 state/jobs 분리
        assert not any(k in json.dumps(st) for k in KEYS.values())
    assert set(secrets) == {KEYS["senior"], KEYS["chili"]}  # 두 key 모두 로그에서 가려짐 (덮어쓰기 없음)
    assert w.slot_owners(e.srv.state / "slots") == []


def test_admin_live_status_and_profile_fingerprint(tmp_path, monkeypatch, capsys):
    e = sched_env(tmp_path, profiles=("default", "senior", "chili"))
    monkeypatch.setattr(w, "service_state", lambda name: "active" if name == "long-live@senior.service" else "inactive")
    (e.srv.paths("senior")["state"]).mkdir(parents=True, exist_ok=True)
    (e.srv.paths("senior")["state"] / "status.json").write_text(json.dumps({"state": "RUNNING", "runtime_seconds": 61.0,
                                                                          "bitrate": "6300.0kbits/s", "reconnects": 2}))
    base = ["--jobs-dir", str(e.jobs), "--state-dir", str(e.srv.state), "--media-dir", str(e.srv.media),
            "--key-file", str(e.srv.etc / "stream.key")]
    assert w.main(["--live-status", *base]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    lives = {c["profile_id"]: c for c in out["lives"]}
    assert set(lives) >= {"default", "senior", "chili"} and out["max_concurrent"] == 2
    assert lives["senior"]["live"] and lives["senior"]["reconnects"] == 2 and not lives["chili"]["live"]
    assert lives["senior"]["key_set"] and not any(k in json.dumps(out) for k in KEYS.values())
    assert w.main(["--key-fingerprint", "--profile", "chili", *base]) == 0
    assert json.loads(capsys.readouterr().out.strip())["fingerprint"] == w.key_fingerprint(KEYS["chili"])
    assert w.main(["--key-fingerprint", *base]) == 0  # 기본 채널 = 기존 stream.key
    assert json.loads(capsys.readouterr().out.strip())["fingerprint"] == w.key_fingerprint(KEYS["default"])
    assert w.main(["--key-fingerprint", "--profile", "../etc", *base]) == w.EXIT_CONFIG


# ---------------- CloudClient (가짜 SSH 서버) ----------------

class MultiRemote(FakeRemote):
    """채널별 서비스/key/설정 파일을 따로 기록하는 가짜 서버."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.installed = True
        self.worker_version = 4
        self.template_installed = True
        self.keys: dict[str, str] = {}
        self.configs: dict[str, str] = {}
        self.services: dict[str, str] = {}  # service → state

    def handle(self, cmd, input):
        for p in ("senior", "chili", "third"):
            if cmd == cc.key_write_cmd(p):
                self.keys[p] = input
                return "", 0, ""
            if cmd == cc.config_write_cmd(p):
                self.configs[p] = input
                return "", 0, ""
        if cmd == cc.KEY_WRITE_CMD:
            self.keys["default"] = input
        if cmd.startswith("bash -s") and input == cc.PROFILE_STATUS_SCRIPT.replace("\r\n", "\n"):
            svc = shlex.split(cmd)[3]
            state = self.services.get(svc, "STOPPED")
            return json.dumps({"active": "active" if state == "RUNNING" else "inactive", "enabled": "enabled",
                               "installed": True, "status": {"state": state, "media": "x.mp4", "mode": "DIRECT COPY",
                                                             "reconnects": 0, "last_error": ""},
                               "disk_free_bytes": self.free}), 0, ""
        p = shlex.split(cmd)
        if p[:4] == ["sudo", "-n", "python3", REMOTE_WORKER] and "--live-status" in p:
            running = [s for s, st in self.services.items() if st == "RUNNING"]
            if self.state == "RUNNING":
                running.append("long-live.service")
            lives = [{"profile_id": s.split("@")[1].split(".")[0] if "@" in s else "default", "live": True,
                      "state": "RUNNING", "active": "active"} for s in running]
            return json.dumps({"ok": True, "lives": lives, "slots_used": len(lives), "max_concurrent": 2,
                               "scheduled_running": []}) + "\n", 0, ""
        if "systemctl cat long-live@.service" in cmd:
            return ("OK\n" if self.template_installed else ""), 0, ""
        if "systemctl restart long-live@" in cmd:
            svc = cmd.rsplit(" ", 1)[1]
            self.services[svc] = "RUNNING"
            return "", 0, ""
        if "systemctl disable --now long-live@" in cmd:
            self.services[cmd.rsplit(" ", 1)[1]] = "STOPPED"
            return "", 0, ""
        return super().handle(cmd, input)


@pytest.fixture
def mremote(tmp_path):
    r = MultiRemote()
    r.files[f"{REMOTE_MEDIA}/shared.mp4"] = b"data"
    return r


def start(client, profile, key):
    return client.start_live(remote_media="shared.mp4", ingest_url="rtmps://a.rtmps.youtube.com/live2",
                             stream_key=key, profile_id=profile, sleep=lambda s: None, wait_seconds=1)


def test_client_channel_start_stop_status_isolated(tmp_path, mremote):
    c = make_client(tmp_path, mremote)
    st = start(c, "senior", KEYS["senior"])
    assert st.state == "RUNNING" and st.profile_id == "senior"
    start(c, "chili", KEYS["chili"])
    assert mremote.services == {"long-live@senior.service": "RUNNING", "long-live@chili.service": "RUNNING"}
    assert mremote.keys == {"senior": KEYS["senior"] + "\n", "chili": KEYS["chili"] + "\n"}  # 다른 파일
    assert "channels/senior/stream.key" in cc.key_write_cmd("senior") and "0750" in cc.key_write_cmd("senior")
    assert json.loads(mremote.configs["senior"])["media"] == "shared.mp4"
    # 세 번째 채널: PC에서 먼저 거부 (서버 worker도 slot 잠금으로 거부)
    with pytest.raises(cc.CloudError) as ei:
        start(c, "third", KEYS["third"])
    assert str(ei.value) == CONCURRENT_BUSY and "third" not in mremote.keys
    # senior 중지 → chili 서비스는 그대로
    c.stop_live(profile_id="senior")
    assert mremote.services == {"long-live@senior.service": "STOPPED", "long-live@chili.service": "RUNNING"}
    assert c.status(profile_id="chili").live and not c.status(profile_id="senior").live
    start(c, "third", KEYS["third"])  # 자리가 비면 가능
    # 이미 송출 중인 채널을 다시 시작(재시작)하는 것은 개수 제한에 걸리지 않음
    start(c, "chili", KEYS["chili"])
    # 어떤 Stream Key도 ssh 명령줄/작업 기록에 없음
    joined = " ".join(" ".join(x["args"]) for x in mremote.calls)
    assert not any(k in joined for k in KEYS.values())
    assert not any(k in "\n".join(c.detail) for k in KEYS.values())
    assert c.logs(profile_id="chili") and any("journalctl -u long-live@chili.service" in x["args"][-1]
                                               for x in mremote.calls)


def test_client_legacy_single_channel_unchanged(tmp_path, mremote):
    c = make_client(tmp_path, mremote)
    c.start_live(remote_media="shared.mp4", ingest_url="rtmps://a.rtmps.youtube.com/live2", stream_key=KEYS["default"],
                 sleep=lambda s: None, wait_seconds=1)
    cmds = [x["args"][-1] for x in mremote.calls]
    assert cc.KEY_WRITE_CMD in cmds and cc.CONFIG_WRITE_CMD in cmds
    assert any(x.endswith("sudo -n systemctl restart long-live.service") for x in cmds)
    assert not any("--live-status" in x or "long-live@" in x for x in cmds)  # 기존 경로: 새 명령 없음
    assert c.status().live
    c.stop_live()
    assert "sudo -n systemctl disable --now long-live.service" in [x["args"][-1] for x in mremote.calls]


def test_client_channel_needs_worker_v4(tmp_path, mremote):
    mremote.template_installed = False
    c = make_client(tmp_path, mremote)
    with pytest.raises(cc.CloudError, match="업데이트"):
        start(c, "senior", KEYS["senior"])
    mremote.template_installed, mremote.worker_version = True, 3
    with pytest.raises(cc.CloudError, match="업데이트"):
        start(c, "senior", KEYS["senior"])
    assert mremote.keys == {}  # key를 쓰기 전에 멈춤


def test_client_channel_key_fingerprint_and_ensure(tmp_path, mremote):
    c = make_client(tmp_path, mremote)
    calls = []
    c._worker_admin = lambda *a, **k: calls.append(a) or {"ok": True, "fingerprint": ""}
    assert c.key_fingerprint("senior") == "" and calls[-1] == ("--key-fingerprint", "--profile", "senior")
    assert c.key_fingerprint() == "" and calls[-1] == ("--key-fingerprint",)
    assert c.ensure_stream_key(KEYS["senior"], "rtmps://a.rtmps.youtube.com/live2", profile_id="senior")
    assert mremote.keys["senior"] == KEYS["senior"] + "\n" and "default" not in mremote.keys


def test_media_sha_reused_across_channels(tmp_path, mremote):
    c = make_client(tmp_path, mremote)
    f = tmp_path / "SET_LIVE_READY.mp4"
    f.write_bytes(b"same video " * 500)
    first = c.upload_many([f])
    n = sum(1 for x in mremote.calls if x.get("input") == "<stream>")
    second = c.upload_many([f])  # 다른 채널에서 같은 영상 → SHA256 같으면 다시 보내지 않음
    assert not first[0].skipped and second[0].skipped and second[0].sha256 == first[0].sha256
    assert sum(1 for x in mremote.calls if x.get("input") == "<stream>") == n


def test_controller_passes_profile_only_for_channels(tmp_path, mremote):
    seen = []

    class Spy:
        def status(self, **kw):
            seen.append(("status", kw))
            return cc.CloudStatus(reachable=True)

        def stop_live(self, **kw):
            seen.append(("stop", kw))
    for pid, expect in ((None, {}), ("default", {}), ("senior", {"profile_id": "senior"})):
        seen.clear()
        ctl = cc.CloudLiveController(lambda: Spy(), profile_id=pid)
        assert ctl.stop_blocking() and ctl._refresh().reachable
        assert seen == [("stop", expect), ("status", expect)]


def test_overview_counts():
    ov = cc.CloudLiveOverview(lives=[cc.ChannelLive("senior", live=True), cc.ChannelLive("chili")],
                              slots_used=1, scheduled_running=["chili"])
    assert ov.live_count == 2 and ov.is_live("chili") and ov.is_live("senior") and not ov.is_live("third")


# ---------------- PC 채널 Profile ----------------

def test_channel_store_default_migration_backup_and_isolation(_isolated_settings):
    from app.live_channels import BACKUP_SUFFIX, ChannelError, LiveChannelStore, key_store_for
    from app.settings import load_settings, update_settings
    update_settings(youtube={"stream_mode": "MANUAL_STREAM_KEY"}, cloud={"host": "1.2.3.4"}, queue=[1, 2])
    before = (_isolated_settings / "settings.json").read_text(encoding="utf-8")
    s = LiveChannelStore()
    assert [p.channel_profile_id for p in s.all()] == ["default"] and not s.migrated()  # 읽기만으로는 쓰지 않음
    senior = s.add("시니어 채널", profile_id="senior")
    chili = s.add("CHILI LAB")
    assert chili.channel_profile_id == "chili_lab" and s.migrated()
    backup = _isolated_settings / ("settings.json" + BACKUP_SUFFIX)
    assert backup.read_text(encoding="utf-8") == before  # 원본 백업 (1번만)
    data = load_settings()
    assert data["youtube"] == {"stream_mode": "MANUAL_STREAM_KEY"} and data["cloud"] == {"host": "1.2.3.4"}
    assert [p.channel_profile_id for p in s.all()] == ["default", "senior", "chili_lab"]
    s.add("일본 채널")  # 한글 이름 → ch_ ID
    assert any(p.channel_profile_id.startswith("ch_") for p in s.all())
    with pytest.raises(ChannelError):
        s.add("시니어 채널")  # 같은 이름
    with pytest.raises(ChannelError):
        s.delete("default")
    # Stream Key 분리 (Windows 외: 메모리, Windows: DPAPI 파일) — 다른 채널 key를 덮어쓰지 않음
    ks, kc = key_store_for("senior", is_windows=False), key_store_for("chili_lab", is_windows=False)
    ks.set(KEYS["senior"])
    kc.set(KEYS["chili"])
    assert ks.get() == KEYS["senior"] and kc.get() == KEYS["chili"]
    assert not any(k in json.dumps(load_settings(), ensure_ascii=False) for k in KEYS.values())
    s.select("senior")
    assert s.selected_id() == "senior"
    s.delete("senior")
    assert s.get("senior") is None and s.selected_id() == "default"
    assert key_store_for("chili_lab", is_windows=False).get() == KEYS["chili"]
    assert senior.key_store_id == "senior"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_channel_stream_keys_in_separate_dpapi_files(_isolated_settings):
    from app.live_channels import key_store_for
    d, s, c = (key_store_for(p, is_windows=True) for p in ("default", "senior", "chili"))
    d.set(KEYS["default"])
    s.set(KEYS["senior"])
    c.set(KEYS["chili"])
    names = sorted(x.name for x in _isolated_settings.iterdir() if x.suffix == ".dat")
    assert names == ["live_secret.dat", "live_secret_chili.dat", "live_secret_senior.dat"]
    for x in _isolated_settings.iterdir():
        if x.suffix == ".dat":
            assert not any(k.encode() in x.read_bytes() for k in KEYS.values())  # 평문 없음
    assert (d.get(), s.get(), c.get()) == (KEYS["default"], KEYS["senior"], KEYS["chili"])
    s.clear()
    assert d.get() == KEYS["default"] and c.get() == KEYS["chili"] and s.get() is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_two_oauth_tokens_isolated_and_legacy_migration(_isolated_settings, monkeypatch):
    from app import youtube_accounts as ya
    from app.live_channels import LiveChannelStore, migrate_legacy_youtube_token, oauth_connected
    from app.settings import load_settings, update_settings
    from app.youtube_oauth import YouTubeAuthStore
    profiles = ya.ProfileStore()
    a = profiles.add(ya.ChannelProfile(ya.new_profile_id(), "시니어", channel_id="UCseniorAAAA", client_file="c.json"))
    b = profiles.add(ya.ChannelProfile(ya.new_profile_id(), "일본", channel_id="UCjapanBBBB", client_file="c.json"))
    profiles.token_store(a.profile_id).save("refresh-A-not-real", client_id="cid")  # 같은 OAuth Client JSON
    profiles.token_store(b.profile_id).save("refresh-B-not-real", client_id="cid")
    assert profiles.token_store(a.profile_id).path != profiles.token_store(b.profile_id).path
    assert profiles.token_store(a.profile_id).load()["refresh_token"] == "refresh-A-not-real"
    assert profiles.token_store(b.profile_id).load()["refresh_token"] == "refresh-B-not-real"
    store = LiveChannelStore()
    sen = store.add("시니어 채널", profile_id="senior", oauth_profile_id=a.profile_id)
    jp = store.add("일본 채널", profile_id="japan", oauth_profile_id=b.profile_id)
    assert oauth_connected(sen, profiles) and oauth_connected(jp, profiles)
    assert not oauth_connected(store.add("새 채널", profile_id="newch"), profiles)
    # 기존 youtube_token.dat → 기본 채널 프로필로 복사 (원본/설정 유지)
    legacy = YouTubeAuthStore()
    legacy.save("refresh-legacy-not-real", client_id="cid")
    update_settings(youtube={"client_file": "c.json", "channel_id": "UClegacyCCCC", "channel_title": "Old Pop Lounge",
                             "stream_id": "st1", "stream_mode": "MANUAL_STREAM_KEY"})
    pid = migrate_legacy_youtube_token(store, profiles)
    assert pid and legacy.has_saved() and legacy.load()["refresh_token"] == "refresh-legacy-not-real"
    assert profiles.token_store(pid).load()["refresh_token"] == "refresh-legacy-not-real"
    assert store.get("default").oauth_profile_id == pid and profiles.get(pid).stream_id == "st1"
    assert load_settings()["youtube"]["channel_id"] == "UClegacyCCCC"  # 기존 설정 그대로
    assert migrate_legacy_youtube_token(store, profiles) == ""  # 한 번만
    raw = json.dumps(load_settings(), ensure_ascii=False)
    assert "refresh-" not in raw


def test_bandwidth_and_concurrency_helpers():
    from app.live_channels import estimate_bandwidth, concurrency_problem, parse_kbps, playlist_kbps
    est = estimate_bandwidth(["6172.0kbits/s", 6172.0])
    assert est.total_mbps == pytest.approx(13.6, abs=0.1) and not est.warn and "약 13.6 Mbps" in est.text
    assert estimate_bandwidth([15000, 15000]).warn
    assert parse_kbps("N/A") is None and parse_kbps(None) is None
    assert playlist_kbps([SimpleNamespace(video_kbps=6000, audio_kbps=None), None]) == 6128
    assert concurrency_problem(2) == CONCURRENT_BUSY and concurrency_problem(1) == ""
    assert concurrency_problem(2, starting_is_live=True) == ""


def test_pc_schedule_conflict_rules():
    from app.scheduled_live import build_job, schedule_conflict
    t = datetime(2026, 10, 10, 21, 0, tzinfo=timezone.utc)

    def j(profile, h0, h1, state="PENDING", **kw):
        d = {"profile_id": profile, "scheduled_at_utc": (t + timedelta(hours=h0)).isoformat(),
             "stop_at_utc": (t + timedelta(hours=h1)).isoformat(), "state": state}
        d.update(kw)
        return d
    listing = [j("senior", 0, 6)]
    assert "같은 채널" in schedule_conflict(t + timedelta(hours=3), t + timedelta(hours=5), listing, "senior")
    assert schedule_conflict(t, t + timedelta(hours=6), listing, "chili") == ""
    listing.append(j("chili", 0, 6))
    assert "최대 2개" in schedule_conflict(t + timedelta(hours=1), t + timedelta(hours=2), listing, "third")
    assert schedule_conflict(t + timedelta(hours=6), t + timedelta(hours=7), listing, "third") == ""
    listing[1]["cancel_requested"] = True
    assert schedule_conflict(t + timedelta(hours=1), t + timedelta(hours=2), listing, "third") == ""
    old = [{"scheduled_at_utc": t.isoformat(), "stop_at_utc": (t + timedelta(hours=2)).isoformat()}]  # v3 (필드 없음)
    assert "같은 채널" in schedule_conflict(t, t + timedelta(hours=1), old, None)
    job = build_job(job_id="sj_1", broadcast_id="b", stream_id="s", start_utc=t, end_utc=t + timedelta(hours=1),
                    playlist=[{"name": "a.mp4", "sha256": "0" * 64, "size": 1}], ingest_url="rtmps://x/live2",
                    key_fp="0" * 16, now=t, profile_id="senior")
    assert job["profile_id"] == "senior" and w.parse_job(job)["profile_id"] == "senior"
    legacy = build_job(job_id="sj_2", broadcast_id="b", stream_id="s", start_utc=t, end_utc=t + timedelta(hours=1),
                       playlist=[{"name": "a.mp4", "sha256": "0" * 64, "size": 1}], ingest_url="rtmps://x/live2",
                       key_fp="0" * 16, now=t)
    assert "profile_id" not in legacy and w.parse_job(legacy)["profile_id"] == "default"
