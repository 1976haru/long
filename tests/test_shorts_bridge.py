"""⑧ 숏츠 만들기 — app.shorts_bridge (가짜 숏츠 엔진으로 프로세스 계약 확인).

진짜 엔진은 SHORTS_ENGINE_DIR(숏츠 엔진 폴더)가 있을 때만 1곡 실제 제작을 해 보고, 없으면 SKIP.
"""
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from app import settings as S
from app import shorts_bridge as SB
from shorts_fakes import configure, make_engine, pid_alive, wait_until


@pytest.fixture(scope="session")
def fake_engine(tmp_path_factory):
    return make_engine(tmp_path_factory.mktemp("fake_shorts_engine"))


@pytest.fixture
def engine(fake_engine):
    configure(fake_engine)
    yield fake_engine
    configure(fake_engine)


def _files(tmp_path, n=3):
    a = []
    for k in range(1, n + 1):
        p = tmp_path / "곡" / f"{k:02d} - 노래 {k}.wav"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"RIFF")
        a.append(p)
    b = tmp_path / "배경" / "bg 1.mp4"
    b.parent.mkdir(parents=True, exist_ok=True)
    b.write_bytes(b"x")
    return a, [b]


class Collector:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def __call__(self, ev):
        with self.lock:
            self.events.append(ev)

    def kinds(self):
        with self.lock:
            return [e["event"] for e in self.events]

    def last(self, kind):
        with self.lock:
            return [e for e in self.events if e["event"] == kind][-1]


# ---------------- 엔진 찾기·버전 ----------------

def test_find_engine_without_venv_says_install_bat(tmp_path):
    (tmp_path / "shorts_cli.py").write_text("", encoding="utf-8")
    with pytest.raises(SB.ShortsEngineError, match="INSTALL.bat"):
        SB.find_engine(tmp_path)
    with pytest.raises(SB.ShortsEngineError, match="폴더"):
        SB.find_engine(tmp_path / "없는 폴더")
    with pytest.raises(SB.ShortsEngineError):
        SB.find_engine("")


def test_find_engine_and_version_080_ok(engine):
    py, cli = SB.find_engine(engine)
    assert py.is_file() and cli.name == "shorts_cli.py"
    assert SB.engine_version(engine) == "0.8.0"
    configure(engine, version="0.9.1")
    assert SB.engine_version(engine) == "0.9.1"


def test_version_071_is_rejected_with_update_message(engine):
    configure(engine, version="0.7.1")
    with pytest.raises(SB.ShortsEngineError, match="숏츠 엔진을 업데이트하세요"):
        SB.engine_version(engine)


# ---------------- 작업 JSON ----------------

def test_write_batch_job_schema_order_options_location(tmp_path):
    audio, bgs = _files(tmp_path)
    audio = [audio[2], audio[0], audio[1]]  # 창에 보이는 순서 그대로 (정렬하지 않음)
    opts = {"output_dir": tmp_path / "출력", "duration": 45, "fit_mode": "blur", "assign": "random", "use_nvenc": True,
            "name_template": "{stem}_short", "start": "auto", "video_offset": "random", "skip_existing": True,
            "seed": None, "engine_dir": "무시되는 키"}
    p = SB.write_batch_job(opts, audio, bgs)
    assert p.parent == S.settings_dir() / "shorts_jobs" and p.name.startswith("batch_") and p.suffix == ".json"
    job = json.loads(p.read_text(encoding="utf-8"))
    assert job["schema"] == "shorts_batch.v1"
    assert job["audio_files"] == [str(a) for a in audio] and job["background_files"] == [str(b) for b in bgs]
    assert "audio_dir" not in job and "background_dir" not in job and "engine_dir" not in job and "seed" not in job
    assert (job["duration"], job["fit_mode"], job["assign"], job["use_nvenc"], job["name_template"]) == \
        (45, "blur", "random", True, "{stem}_short")
    assert job["output_dir"] == str(tmp_path / "출력") and job["start"] == "auto" and job["skip_existing"] is True
    p2 = SB.write_batch_job(opts, audio, bgs)  # 같은 초에 또 저장해도 덮어쓰지 않는다
    assert p2 != p and p.exists() and p2.exists()
    with pytest.raises(SB.ShortsEngineError):
        SB.write_batch_job(opts, [], bgs)
    with pytest.raises(SB.ShortsEngineError):
        SB.write_batch_job({}, audio, bgs)


# ---------------- 실행 ----------------

def _job(tmp_path, n=3):
    audio, bgs = _files(tmp_path, n)
    return SB.write_batch_job({"output_dir": tmp_path / "out", "duration": 45}, audio, bgs), audio


def test_run_events_in_order_and_exit(engine, tmp_path):
    job, audio = _job(tmp_path)
    run, got = SB.ShortsRun(engine), Collector()
    run.start(job, got)
    assert run.running
    assert run.wait(30)
    assert not run.running
    k = got.kinds()
    assert k[0] == "batch_start" and k[-2:] == ["batch_result", "exit"]
    assert "log" in k and got.last("log")["line"] == "plain text line from engine"
    items = [e for e in got.events if e["event"] in ("item_start", "progress", "item_result")]
    seq = [(e["event"], e["index"]) for e in items]
    for i in (1, 2, 3):  # 곡마다 item_start → progress → item_result 순서
        a, b, c = seq.index(("item_start", i)), seq.index(("progress", i)), seq.index(("item_result", i))
        assert a < b < c
    assert [e["index"] for e in got.events if e["event"] == "item_start"] == [1, 2, 3]
    ex = got.last("exit")
    assert ex["code"] == 0 and ex["batch_result"]["done"] == 3 and not ex["cancel_requested"] and not ex["killed"]
    assert json.loads((engine / "last_job.json").read_text(encoding="utf-8"))["audio_files"] == [str(a) for a in audio]


def test_run_partial_failure_exit_6(engine, tmp_path):
    configure(engine, fail=[2])
    job, _ = _job(tmp_path)
    run, got = SB.ShortsRun(engine), Collector()
    run.start(job, got)
    assert run.wait(30)
    r2 = [e for e in got.events if e["event"] == "item_result" and e["index"] == 2][0]
    assert r2["ok"] is False and r2["message"] == "fake failure message 2"
    assert got.last("exit")["code"] == SB.EXIT_PARTIAL


def test_stdin_must_be_pipe(engine, tmp_path):
    """가짜 엔진은 stdin이 PIPE가 아니면 exit 9. ShortsRun은 통과해야 하고, DEVNULL로 직접 띄우면 실패해야 한다."""
    job, _ = _job(tmp_path, 1)
    run, got = SB.ShortsRun(engine), Collector()
    run.start(job, got)
    assert run.wait(30) and got.last("exit")["code"] == 0 and "error" not in got.kinds()
    py, cli = SB.find_engine(engine)
    p = subprocess.run([str(py), str(cli), "batch", "--job", str(job)], stdin=subprocess.DEVNULL, capture_output=True,
                       timeout=30)
    assert p.returncode == 9 and b"fake_stdin_not_pipe" in p.stdout


def test_cancel_line_exit_5(engine, tmp_path):
    configure(engine, mode="slow")
    job, _ = _job(tmp_path)
    run, got = SB.ShortsRun(engine), Collector()
    run.start(job, got)
    assert wait_until(lambda: "item_start" in got.kinds(), 20)
    run.cancel()
    assert run.wait(10), "cancel 줄을 받은 엔진이 끝나지 않았습니다"
    ex = got.last("exit")
    assert ex["code"] == SB.EXIT_CANCELLED and ex["cancel_requested"] and not ex["killed"]
    assert run.proc.poll() == 5 and got.last("batch_result")["cancelled"] is True


def test_cancel_ignored_kills_process_tree(engine, tmp_path):
    configure(engine, mode="ignore")
    job, _ = _job(tmp_path)
    run, got = SB.ShortsRun(engine, cancel_timeout=1.5), Collector()
    run.start(job, got)
    assert wait_until(lambda: (engine / "child.pid").exists() and "item_start" in got.kinds(), 20)
    child = int((engine / "child.pid").read_text(encoding="utf-8"))
    assert pid_alive(child)
    run.cancel()
    assert run.wait(15), "강제 종료되지 않았습니다"
    ex = got.last("exit")
    assert ex["killed"] and ex["code"] != 0 and run.proc.poll() is not None
    assert wait_until(lambda: not pid_alive(child), 10), "자식 프로세스(가짜 ffmpeg)가 남았습니다"


def test_cancel_before_start_never_launches(engine, tmp_path):
    job, _ = _job(tmp_path, 1)
    run, got = SB.ShortsRun(engine), Collector()
    run.cancel()
    run.start(job, got)
    assert run.wait(1) and run.proc is None and not (engine / "ran.txt").exists()
    assert got.kinds() == ["exit"] and got.last("exit")["code"] == SB.EXIT_CANCELLED


# ---------------- 실제 엔진 (선택) ----------------

@pytest.mark.skipif(not os.environ.get("SHORTS_ENGINE_DIR"), reason="실제 숏츠 엔진 시험: SHORTS_ENGINE_DIR 미지정")
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg 없음")
def test_real_engine_one_song(tmp_path):
    eng = Path(os.environ["SHORTS_ENGINE_DIR"])
    assert SB.engine_version(eng)
    ff = shutil.which("ffmpeg")
    song, bg = tmp_path / "곡 1.wav", tmp_path / "배경.mp4"
    subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=20", str(song)], check=True)
    subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=6", "-pix_fmt", "yuv420p",
                    str(bg)], check=True)
    job = SB.write_batch_job({"output_dir": tmp_path / "out", "duration": 15, "start": 0, "fit_mode": "blur",
                              "use_nvenc": False}, [song], [bg])
    run, got = SB.ShortsRun(eng), Collector()
    run.start(job, got)
    assert run.wait(300)
    ex = got.last("exit")
    assert ex["code"] == 0, ex
    out = Path(got.last("item_result")["output"])
    assert out.is_file() and out.stat().st_size > 10_000
    assert not list((tmp_path / "out").glob("*.part.mp4"))
