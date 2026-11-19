"""⑧ 숏츠 만들기 — 숏츠 엔진(ShortsMaker Studio) 연결 (화면 없음).

숏츠 엔진 코드는 import하지 않는다. 항상 별도 프로세스로만 실행한다:
    <엔진 폴더>\\.venv\\Scripts\\python.exe shorts_cli.py batch --job <batch.json>
계약: https://github.com/1976haru/shorts docs/CLI.md (shorts_batch.v1, stdout JSON Lines)

- stdin은 반드시 PIPE로 연다. 엔진은 stdin EOF(DEVNULL 포함)를 취소로 본다.
- 취소: stdin에 "cancel" 한 줄 → 기다려도 끝나지 않으면 프로세스 트리(FFmpeg 포함) 강제 종료.
- on_event는 읽기 스레드에서 불린다. 화면 갱신은 호출하는 쪽이 이벤트 큐로 Tk main thread에서 한다.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

from . import settings as _settings

MIN_ENGINE_VERSION = (0, 8, 0)
VERSION_TIMEOUT = 20.0
CANCEL_TIMEOUT = 10.0
BATCH_SCHEMA = "shorts_batch.v1"
JOBS_DIRNAME = "shorts_jobs"
CLI_NAME = "shorts_cli.py"
# shorts_batch.v1에서 창이 넘길 수 있는 설정 키 (목록·폴더 키는 write_batch_job이 직접 채운다)
OPTION_KEYS = ("output_dir", "duration", "start", "highlight_rank", "fit_mode", "fade_sec", "use_nvenc",
               "assign", "seed", "video_offset", "name_template", "skip_existing")
EXIT_OK, EXIT_CANCELLED, EXIT_PARTIAL = 0, 5, 6
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class ShortsEngineError(RuntimeError):
    """화면에 그대로 보여 줄 수 있는 한국어 오류."""


def find_engine(engine_dir) -> tuple[Path, Path]:
    """(python_exe, cli_path). 엔진 폴더의 .venv 파이썬과 shorts_cli.py가 모두 있어야 한다."""
    if not str(engine_dir or "").strip():
        raise ShortsEngineError("숏츠 엔진 폴더를 지정하세요 (예: D:\\03_shorts).")
    d = Path(engine_dir)
    if not d.is_dir():
        raise ShortsEngineError(f"숏츠 엔진 폴더가 없습니다: {d}")
    py = d / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.is_file():
        raise ShortsEngineError(f"숏츠 엔진 폴더에서 INSTALL.bat을 먼저 실행하세요.\n(없는 파일: {py})")
    cli = d / CLI_NAME
    if not cli.is_file():
        raise ShortsEngineError(f"{CLI_NAME}이 없습니다 — 숏츠 엔진을 업데이트하세요 (v0.8.0 이상).\n(폴더: {d})")
    return py, cli


def _version_tuple(text: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(x) for x in str(text).strip().split("."))
    except ValueError:
        return None


def engine_version(engine_dir, timeout: float = VERSION_TIMEOUT) -> str:
    """`shorts_cli.py version` 실행. 0.8.0 미만이거나 응답이 없으면 ShortsEngineError."""
    py, cli = find_engine(engine_dir)
    try:
        p = subprocess.run([str(py), str(cli), "version"], cwd=str(cli.parent), stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise ShortsEngineError(f"숏츠 엔진이 {timeout:g}초 안에 응답하지 않았습니다.") from None
    except OSError as e:
        raise ShortsEngineError(f"숏츠 엔진을 실행하지 못했습니다: {e}") from None
    version = None
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("event") == "version":
            version = str(ev.get("version", ""))
    if not version:
        tail = p.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [f"종료 코드 {p.returncode}"]
        raise ShortsEngineError(f"숏츠 엔진 버전을 확인하지 못했습니다: {tail[0]}")
    vt = _version_tuple(version)
    if vt is None or vt < MIN_ENGINE_VERSION:
        need = ".".join(map(str, MIN_ENGINE_VERSION))
        raise ShortsEngineError(f"숏츠 엔진을 업데이트하세요 (지금 v{version}, 필요 v{need} 이상).")
    return version


def jobs_dir() -> Path:
    d = _settings.settings_dir() / JOBS_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_batch_job(options: dict, audio_files, background_files) -> Path:
    """shorts_batch.v1 JSON을 설정 폴더/shorts_jobs/batch_YYYYMMDD_HHMMSS.json에 저장 (목록 순서 그대로)."""
    audio = [str(Path(p)) for p in audio_files]
    bgs = [str(Path(p)) for p in background_files]
    if not audio:
        raise ShortsEngineError("곡을 1개 이상 추가하세요.")
    if not bgs:
        raise ShortsEngineError("배경 영상을 1개 이상 추가하세요.")
    job = {"schema": BATCH_SCHEMA, "audio_files": audio, "background_files": bgs}
    for k in OPTION_KEYS:
        if k in options and options[k] is not None:
            job[k] = str(Path(options[k])) if k == "output_dir" else options[k]
    if "output_dir" not in job:
        raise ShortsEngineError("저장 폴더를 지정하세요.")
    d = jobs_dir()
    stem = "batch_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    path, n = d / f"{stem}.json", 0
    while path.exists():
        n += 1
        path = d / f"{stem}_{n}.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def kill_tree(pid: int) -> None:
    """엔진과 그 아래 FFmpeg까지 강제 종료."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30,
                       creationflags=_NO_WINDOW)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)  # start_new_session=True로 띄웠으므로 pid = 프로세스 그룹
        except OSError:
            pass


class ShortsRun:
    """숏츠 엔진 batch 1회 실행. start → (읽기 스레드) on_event(dict) … → on_event({"event":"exit", ...})."""

    def __init__(self, engine_dir, *, cancel_timeout: float = CANCEL_TIMEOUT):
        self.engine_dir = Path(engine_dir)
        self.cancel_timeout = float(cancel_timeout)
        self.proc: subprocess.Popen | None = None
        self.last_result: dict | None = None
        self.cancel_requested = False
        self.killed = False
        self._stderr: deque[str] = deque(maxlen=40)
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    @property
    def running(self) -> bool:
        return self.proc is not None and not self._done.is_set()

    def start(self, job_path, on_event) -> None:
        """엔진을 띄운다. 시작 전에 cancel()이 불렸으면 띄우지 않고 exit(code 5)만 알린다."""
        py, cli = find_engine(self.engine_dir)
        cmd = [str(py), str(cli), "batch", "--job", str(Path(job_path))]
        kw = {"creationflags": _NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        self._on_event = on_event
        with self._lock:
            if self.proc is not None or self._done.is_set():
                raise RuntimeError("ShortsRun은 한 번만 시작할 수 있습니다.")
            if self.cancel_requested:
                self._done.set()
                launched = False
            else:
                try:
                    # stdin=PIPE 필수: DEVNULL이면 엔진이 EOF를 취소로 보고 바로 끝낸다 (docs/CLI.md)
                    self.proc = subprocess.Popen(cmd, cwd=str(cli.parent), stdin=subprocess.PIPE,
                                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
                except OSError as e:
                    raise ShortsEngineError(f"숏츠 엔진을 실행하지 못했습니다: {e}") from None
                launched = True
        if not launched:
            self._emit({"event": "exit", "code": EXIT_CANCELLED, "batch_result": None, "cancel_requested": True,
                        "killed": False, "stderr_tail": []})
            return
        err = threading.Thread(target=self._drain_stderr, name="shorts-stderr", daemon=True)
        out = threading.Thread(target=self._read_stdout, name="shorts-stdout", daemon=True)
        self._threads += [err, out]
        err.start()
        out.start()

    def _emit(self, ev: dict) -> None:
        try:
            self._on_event(ev)
        except Exception:  # 화면 쪽 오류로 읽기 스레드가 멈추면 엔진 stdout이 막힌다
            pass

    def _drain_stderr(self) -> None:
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._stderr.append(line)

    def _read_stdout(self) -> None:
        proc = self.proc
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    ev = None
                if not isinstance(ev, dict) or "event" not in ev:
                    ev = {"event": "log", "line": line}
                if ev.get("event") == "batch_result":
                    self.last_result = ev
                self._emit(ev)
            code = proc.wait()
            self._threads[0].join(timeout=5)
            self.close_stdin()
            self._emit({"event": "exit", "code": code, "batch_result": self.last_result,
                        "cancel_requested": self.cancel_requested, "killed": self.killed,
                        "stderr_tail": list(self._stderr)})
        finally:
            self._done.set()

    def cancel(self) -> None:
        """기다리지 않고 돌아온다. cancel_timeout 안에 끝나지 않으면 별도 스레드가 프로세스 트리를 종료한다."""
        with self._lock:
            if self.cancel_requested or self._done.is_set():
                return
            self.cancel_requested = True
            if self.proc is None:  # 아직 시작 전: start()가 엔진을 띄우지 않는다
                return
        try:
            self.proc.stdin.write(b"cancel\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass
        # daemon이 아님: 프로그램이 바로 종료되더라도 엔진/FFmpeg를 남기지 않도록 끝까지 확인한다
        t = threading.Thread(target=self._kill_after_timeout, name="shorts-cancel")
        self._threads.append(t)
        t.start()

    def _kill_after_timeout(self) -> None:
        if self._done.wait(self.cancel_timeout):
            return
        if self.proc.poll() is None:
            self.killed = True
            try:
                kill_tree(self.proc.pid)
            except Exception:
                pass
            if self.proc.poll() is None:
                try:
                    self.proc.kill()
                except OSError:
                    pass

    def wait(self, timeout: float | None = None) -> bool:
        """exit 이벤트까지 기다린다 (작업 스레드용 — Tk main thread에서 부르지 않는다)."""
        return self._done.wait(timeout)

    def close_stdin(self) -> None:
        try:
            if self.proc is not None and self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
