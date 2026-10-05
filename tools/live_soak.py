"""LIVE 장시간 안정성(soak) 측정기 — 실제 YouTube/OCI에 연결하지 않는다 (로컬 sink로 DIRECT COPY).

예:
    python tools/live_soak.py --hours 24 --mode playlist-copy
    python tools/live_soak.py --hours 2 --mode worker-playlist --inputs a.mp4 b.mp4 c.mp4
    python tools/live_soak.py --seconds 60 --interval 5 --mode single-copy     (짧은 smoke)

모드
    single-copy      : 내 PC LIVE와 같은 경로 (LiveSupervisor + LiveProcess), LIVE READY 1개
    playlist-copy    : 내 PC Playlist (ffconcat + stream_loop + copy)
    worker-playlist  : Cloud worker(cloud/long_live_worker.py)를 그대로 실행 (debug 출력)

측정 (interval마다 CSV 1줄, 끝에 JSON 요약)
    Python/worker RSS, FFmpeg RSS, thread 수, handle(Windows)/fd(Linux) 수, 재접속 횟수, 송출 위치,
    Playlist 회차, 마지막 오류, 출력 폴더 크기, 로그 크기
요약의 memory_growth: 앞 10% 구간 중앙값 대비 마지막 10% 구간 중앙값이 20% 그리고 20MB 넘게 늘면 SUSPECT.
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "cloud"))

GROWTH_RATIO = 0.20
GROWTH_MIN_BYTES = 20 * 1024 * 1024


# ---------------- process metrics (외부 패키지 없이) ----------------

def rss_bytes(pid: int) -> int | None:
    if os.name == "nt":
        try:
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            h = k32.OpenProcess(0x1000 | 0x0010, False, pid)  # QUERY_LIMITED_INFORMATION | VM_READ
            if not h:
                return None
            try:
                pmc = PMC()
                pmc.cb = ctypes.sizeof(PMC)
                if not psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                    return None
                return int(pmc.WorkingSetSize)
            finally:
                k32.CloseHandle(h)
        except Exception:
            return None
    try:
        for line in open(f"/proc/{pid}/status", encoding="ascii", errors="ignore"):
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def handle_count(pid: int) -> int | None:
    if os.name == "nt":
        try:
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            h = k32.OpenProcess(0x1000, False, pid)
            if not h:
                return None
            try:
                n = wintypes.DWORD()
                return int(n.value) if k32.GetProcessHandleCount(h, ctypes.byref(n)) else None
            finally:
                k32.CloseHandle(h)
        except Exception:
            return None
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return None


def dir_size(path: Path) -> int:
    total = 0
    for p in Path(path).rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def analyze_growth(values: list[float], min_abs: float = GROWTH_MIN_BYTES) -> dict:
    """앞 10% vs 마지막 10% 중앙값 비교 (일시적 튐에 덜 민감). 시작 직후(warm-up) 표본은 호출자가 뺀다.
    min_abs: 메모리는 bytes(20MB), handle/fd 개수는 개수(50) 기준."""
    vals = [v for v in values if v is not None]
    if len(vals) < 10:
        return {"samples": len(vals), "verdict": "NOT_ENOUGH_SAMPLES"}
    k = max(1, len(vals) // 10)
    head, tail = statistics.median(vals[:k]), statistics.median(vals[-k:])
    grew = tail - head
    suspect = head > 0 and grew > min_abs and grew / head > GROWTH_RATIO
    return {"samples": len(vals), "head_median": head, "tail_median": tail, "growth_bytes": grew,
            "growth_ratio": (grew / head) if head else None, "max": max(vals),
            "verdict": "SUSPECT" if suspect else "OK"}


# ---------------- sample media ----------------

def make_samples(out: Path, ffmpeg: str, count: int = 3, seconds: int = 10, bitrate: str = "2000k") -> list[Path]:
    srcs = ["testsrc2", "smptebars", "rgbtestsrc"]
    paths = []
    for i in range(count):
        p = out / f"soak_{i + 1:02d}_LIVE_READY.mp4"
        if not p.exists():
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", f"{srcs[i % 3]}=s=1920x1080:r=30:d={seconds}",
                            "-f", "lavfi", "-i", f"sine=frequency={440 + 110 * i}:sample_rate=44100:duration={seconds}",
                            "-c:v", "libx264", "-preset", "veryfast", "-b:v", bitrate, "-maxrate", bitrate,
                            "-bufsize", bitrate, "-pix_fmt", "yuv420p", "-g", "60", "-keyint_min", "60",
                            "-sc_threshold", "0", "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                            "-shortest", str(p)], check=True)
        paths.append(p)
    return paths


# ---------------- runners ----------------

class LocalRunner:
    """내 PC LIVE 경로 그대로 (LiveSupervisor + LiveProcess). sink는 로컬 null/파일."""

    def __init__(self, ffmpeg: str, ffprobe: str, inputs: list[Path], workdir: Path, sink: str):
        from app.live_core import LiveProcess, build_stream_command
        from app.live_playlist import entry_durations, write_ffconcat
        from app.live_profile import MODE_COPY, LiveConfig
        from app.live_ready import analyze_live_ready
        from app.live_supervisor import LiveSupervisor
        from app.tooling import FfmpegExecutionGuard
        reports = [analyze_live_ready(p, Path(ffprobe)) for p in inputs]
        bad = [f"{p.name}: {r.issues[0].message}" for p, r in zip(inputs, reports) if not r.ready]
        if bad:
            raise SystemExit("LIVE READY 아님: " + "; ".join(bad))
        if len(inputs) > 1:
            self.durations = entry_durations(reports)
            manifest = write_ffconcat(workdir / "soak.ffconcat", list(zip(inputs, self.durations)))
            config = LiveConfig(input_path=manifest, ingest_url="rtmp://127.0.0.1/soak", stream_key="soak-dummy",
                                mode=MODE_COPY, input_format="concat")
        else:
            self.durations = []
            config = LiveConfig(input_path=inputs[0], ingest_url="rtmp://127.0.0.1/soak", stream_key="soak-dummy",
                                mode=MODE_COPY)
        target = os.devnull if sink == "null" else str(workdir / "soak_out.flv")

        def factory():
            return LiveProcess(build_stream_command(ffmpeg=Path(ffmpeg), config=config, output_target=target),
                               secrets=["soak-dummy"])
        self.sup = LiveSupervisor(factory, guard=FfmpegExecutionGuard())
        self.log_dir = None

    def start(self):
        self.sup.start()
        self.sup.run_in_background(interval=1.0)

    def stop(self):
        self.sup.stop()

    def sample(self) -> dict:
        from app.live_session import playlist_position
        proc = getattr(self.sup, "_proc", None)
        p = getattr(proc, "_proc", None)
        stats = self.sup.stats()
        pos = playlist_position(stats.out_time_seconds if stats else None, self.durations) if self.durations else None
        return {"state": self.sup.state.value, "ffmpeg_pid": getattr(p, "pid", None),
                "reconnects": self.sup.reconnect_count,
                "out_time": stats.out_time_seconds if stats else None,
                "speed": stats.speed if stats else None,
                "playlist_round": pos[1] if pos else None,
                "last_error": self.sup.last_error}


class WorkerRunner:
    """Cloud worker를 그대로 실행 (설정/키/미디어는 임시 폴더, 출력은 로컬 sink)."""

    def __init__(self, ffmpeg: str, ffprobe: str, inputs: list[Path], workdir: Path, sink: str):
        import long_live_worker as w
        media = workdir / "media"; state = workdir / "state"; etc = workdir / "etc"; logs = workdir / "logs"
        for d in (media, state, etc, logs):
            d.mkdir(parents=True, exist_ok=True)
        names = []
        for p in inputs:
            dst = media / p.name
            if not dst.exists():
                shutil.copy2(p, dst)
            names.append(p.name)
        (etc / "live.json").write_text(json.dumps({"schema_version": 2, "media": names if len(names) > 1 else names[0],
                                                   "play_mode": "sequential", "ingest_url": "rtmp://127.0.0.1/soak",
                                                   "mode": "copy", "session_mode": "continuous",
                                                   "session_id": f"soak{int(time.time())}"}))
        (etc / "stream.key").write_text("soak-dummy\n")
        w.setup_logging(str(logs), [])
        target = os.devnull if sink == "null" else str(workdir / "soak_out.flv")
        self.worker = w.Worker(config_path=etc / "live.json", key_path=etc / "stream.key", media_dir=media,
                               state_dir=state, ffmpeg=ffmpeg, ffprobe=ffprobe, debug_output=target)
        self.log_dir = logs
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.worker.run, name="soak-worker", daemon=True)
        self.thread.start()

    def stop(self):
        self.worker.request_stop()
        if self.thread:
            self.thread.join(30)

    def sample(self) -> dict:
        snap = self.worker.snapshot()
        p = self.worker.proc
        return {"state": snap["state"], "ffmpeg_pid": getattr(p, "pid", None), "reconnects": snap["reconnects"],
                "out_time": snap["out_time_seconds"], "speed": snap["speed"],
                "playlist_round": snap["playlist_round"], "last_error": snap["last_error"]}


# ---------------- main ----------------

FIELDS = ["t", "elapsed_s", "state", "py_rss", "py_threads", "py_handles", "ffmpeg_pid", "ffmpeg_rss",
          "ffmpeg_handles", "reconnects", "out_time", "speed", "playlist_round", "out_dir_bytes", "log_bytes", "last_error"]


def run(args) -> dict:
    ffmpeg = args.ffmpeg or shutil.which("ffmpeg")
    ffprobe = args.ffprobe or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SystemExit("ffmpeg/ffprobe를 찾을 수 없습니다.")
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    inputs = [Path(x).resolve() for x in args.inputs] if args.inputs else make_samples(
        out, ffmpeg, 1 if args.mode == "single-copy" else 3, args.sample_seconds)
    if args.mode == "single-copy":
        inputs = inputs[:1]
    runner = (WorkerRunner if args.mode == "worker-playlist" else LocalRunner)(ffmpeg, ffprobe, inputs, out, args.sink)
    duration = args.seconds if args.seconds else args.hours * 3600
    csv_path, summary_path = out / "soak_samples.csv", out / "soak_summary.json"
    rows: list[dict] = []
    t0 = time.monotonic()
    runner.start()
    me = os.getpid()
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=FIELDS)
            wr.writeheader()
            while True:
                elapsed = time.monotonic() - t0
                s = runner.sample()
                pid = s.get("ffmpeg_pid")
                row = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "elapsed_s": round(elapsed, 1), "state": s["state"],
                       "py_rss": rss_bytes(me), "py_threads": threading.active_count(), "py_handles": handle_count(me),
                       "ffmpeg_pid": pid, "ffmpeg_rss": rss_bytes(pid) if pid else None,
                       "ffmpeg_handles": handle_count(pid) if pid else None, "reconnects": s["reconnects"],
                       "out_time": s["out_time"], "speed": s["speed"], "playlist_round": s["playlist_round"],
                       "out_dir_bytes": dir_size(out) if args.sink == "file" else None,
                       "log_bytes": dir_size(runner.log_dir) if runner.log_dir else None,
                       "last_error": (s.get("last_error") or "")[:200]}
                rows.append(row)
                wr.writerow(row)
                f.flush()
                if elapsed >= duration:
                    break
                time.sleep(min(args.interval, max(0.1, duration - elapsed)))
    except KeyboardInterrupt:
        print("중지 요청 — 정리 중")
    finally:
        runner.stop()
    warmup = max(10.0, 0.02 * duration)  # FFmpeg 시작 직후 표본 제외
    steady = [r for r in rows if r["elapsed_s"] >= warmup] or rows
    summary = {
        "mode": args.mode, "inputs": [p.name for p in inputs], "requested_seconds": duration,
        "warmup_seconds_excluded": warmup,
        "measured_seconds": rows[-1]["elapsed_s"] if rows else 0, "samples": len(rows),
        "final_state": rows[-1]["state"] if rows else None,
        "reconnects": rows[-1]["reconnects"] if rows else None,
        "max_playlist_round": max((r["playlist_round"] or 0) for r in rows) if rows else None,
        "last_out_time": rows[-1]["out_time"] if rows else None,
        "python_rss": analyze_growth([r["py_rss"] for r in steady]),
        "ffmpeg_rss": analyze_growth([r["ffmpeg_rss"] for r in steady]),
        "python_threads_max": max((r["py_threads"] or 0) for r in rows) if rows else None,
        "python_handles": analyze_growth([r["py_handles"] for r in steady], min_abs=50),
        "ffmpeg_handles": analyze_growth([r["ffmpeg_handles"] for r in steady], min_abs=50),
        "csv": str(csv_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LIVE soak (실제 YouTube/OCI 연결 없음)")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--seconds", type=float, default=0.0, help="짧은 smoke용 (지정 시 --hours 무시)")
    ap.add_argument("--mode", choices=("single-copy", "playlist-copy", "worker-playlist"), default="playlist-copy")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--inputs", nargs="*")
    ap.add_argument("--sample-seconds", type=int, default=10)
    ap.add_argument("--sink", choices=("null", "file"), default="null")
    ap.add_argument("--out", default="soak_out")
    ap.add_argument("--ffmpeg")
    ap.add_argument("--ffprobe")
    args = ap.parse_args(argv)
    summary = run(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    bad = [k for k in ("python_rss", "ffmpeg_rss", "python_handles", "ffmpeg_handles")
           if summary[k].get("verdict") == "SUSPECT"]
    return 1 if bad or summary["final_state"] in ("FAILED",) else 0


if __name__ == "__main__":
    sys.exit(main())
