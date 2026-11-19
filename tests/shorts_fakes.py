"""⑧ 숏츠 테스트용 가짜 숏츠 엔진 (진짜 엔진·librosa 없음).

엔진 폴더 구조를 그대로 흉내 낸다: <폴더>\\.venv\\Scripts\\python.exe(진짜 venv, --without-pip) + shorts_cli.py(가짜).
동작은 <폴더>\\fake_mode.json으로 바꾼다:
  version  : version 명령이 돌려줄 버전 (기본 "0.8.0")
  mode     : "normal"(곡마다 바로 완료) | "slow"(곡마다 최대 30초, cancel/EOF 받으면 exit 5) |
             "ignore"(cancel을 무시하고 자식 프로세스(가짜 ffmpeg)를 띄운 채 계속 실행)
  fail     : 실패시킬 곡 번호 목록 (normal)
가짜 엔진은 stdin이 PIPE가 아니면 error 이벤트 + exit 9로 끝난다 (DEVNULL 방지 확인).
실행될 때마다 ran.txt에 한 줄, 받은 작업 JSON은 last_job.json에 남긴다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

FAKE_CLI = r'''
import json, os, sys, threading, time, subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
_cfg_path = HERE / "fake_mode.json"
CFG = json.loads(_cfg_path.read_text(encoding="utf-8")) if _cfg_path.exists() else {}


def out(ev):
    sys.stdout.write(json.dumps(ev, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def stdin_is_pipe():
    if os.name == "nt":
        import ctypes, msvcrt
        try:
            h = msvcrt.get_osfhandle(0)
        except OSError:
            return False
        k32 = ctypes.windll.kernel32
        k32.GetFileType.argtypes = [ctypes.c_void_p]
        return k32.GetFileType(h) == 3  # FILE_TYPE_PIPE
    import stat
    try:
        return stat.S_ISFIFO(os.fstat(0).st_mode)
    except OSError:
        return False


def watch_cancel(ev):
    def run():
        for line in sys.stdin:
            if line.strip() == "cancel":
                break
        ev.set()  # cancel line or EOF
    threading.Thread(target=run, daemon=True).start()


def batch(job_path):
    with open(HERE / "ran.txt", "a", encoding="utf-8") as f:
        f.write(f"{os.getpid()}\n")
    if not stdin_is_pipe():
        out({"event": "error", "code": "fake_stdin_not_pipe", "message": "stdin is not a pipe"})
        return 9
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    (HERE / "last_job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    mode = CFG.get("mode", "normal")
    audio = job["audio_files"]
    outdir = Path(job["output_dir"])
    outdir.mkdir(parents=True, exist_ok=True)
    total = len(audio)
    out({"event": "batch_start", "total": total, "output_dir": str(outdir), "assign": job.get("assign", "rotate"),
         "seed": 1, "csv": str(outdir / "fake.csv")})
    print("plain text line from engine", flush=True)  # JSON이 아닌 줄 -> log 이벤트
    if mode == "ignore":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        (HERE / "child.pid").write_text(str(child.pid), encoding="utf-8")
        out({"event": "item_start", "index": 1, "total": total, "audio": audio[0], "background": "x", "video_offset": 0})
        threading.Thread(target=lambda: [None for _ in sys.stdin], daemon=True).start()  # cancel은 읽고 무시
        time.sleep(120)
        return 0
    cancel = threading.Event()
    watch_cancel(cancel)
    done = failed = 0
    for i, a in enumerate(audio, 1):
        bg = job["background_files"][(i - 1) % len(job["background_files"])]
        out({"event": "item_start", "index": i, "total": total, "audio": a, "background": bg, "video_offset": 1.5})
        if mode == "slow":
            end = time.monotonic() + 30
            while time.monotonic() < end and not cancel.is_set():
                time.sleep(0.05)
            if cancel.is_set():
                out({"event": "batch_result", "ok": False, "done": done, "failed": failed, "skipped": 0,
                     "csv": "", "elapsed_sec": 0.1, "warnings": [], "cancelled": True,
                     "cancelled_remaining": total - i + 1, "code": "cancelled"})
                return 5
        out({"event": "progress", "index": i, "value": 0.5, "overall": (i - 0.5) / total})
        if i in CFG.get("fail", []):
            failed += 1
            out({"event": "item_result", "index": i, "ok": False, "code": "invalid_job",
                 "message": "fake failure message " + str(i)})
            continue
        o = outdir / (Path(a).stem + "_short.mp4")
        o.write_bytes(b"fake mp4")
        done += 1
        out({"event": "progress", "index": i, "value": 1.0, "overall": i / total})
        out({"event": "item_result", "index": i, "ok": True, "output": str(o), "start": 10.0, "elapsed_sec": 0.01,
             "warnings": []})
    out({"event": "batch_result", "ok": failed == 0, "done": done, "failed": failed, "skipped": 0, "csv": "",
         "elapsed_sec": 0.1, "warnings": []})
    return 6 if failed else 0


def main():
    args = sys.argv[1:]
    if args[:1] == ["version"]:
        out({"event": "version", "engine": "shorts", "version": CFG.get("version", "0.8.0"),
             "job_schema": "shorts_job.v1"})
        return 0
    if args[:1] == ["batch"]:
        return batch(args[args.index("--job") + 1])
    return 2


sys.exit(main())
'''


def make_engine(root: Path) -> Path:
    """진짜 venv(+가짜 shorts_cli.py)로 엔진 폴더를 만든다 (pip 없이 몇 초)."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root / ".venv")], check=True,
                   capture_output=True, timeout=180)
    (root / "shorts_cli.py").write_text(FAKE_CLI, encoding="utf-8")
    return root


def configure(engine: Path, **cfg) -> None:
    (engine / "fake_mode.json").write_text(json.dumps(cfg), encoding="utf-8")
    for name in ("ran.txt", "last_job.json", "child.pid"):
        try:
            (engine / name).unlink()
        except FileNotFoundError:
            pass


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x00100000 | 0x1000, False, int(pid))  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            return k32.WaitForSingleObject(h, 0) != 0  # 0 = 이미 끝남
        finally:
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_until(cond, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()
