"""여러 채널 동시 Cloud LIVE soak — Cloud worker v4 2채널을 동시에 DIRECT COPY (로컬 null 출력, YouTube/OCI 없음).

예:
    python tools/multi_live_soak.py --minutes 30
    python tools/multi_live_soak.py --seconds 60 --interval 5      (짧은 smoke)

확인
    - 채널 A/B FFmpeg 2개가 동시에 계속 송출 (speed≈1.0, 재접속 0)
    - 세 번째 채널은 시작 거부 (서버 전체 동시 2개)
    - 채널별 CPU(초당 CPU 시간), RAM(RSS), handle 수 증가 여부 (live_soak.analyze_growth)
    - 종료 후 남은 FFmpeg(고아/zombie) 0개, slot 잠금 해제
요약은 --out 폴더의 multi_soak_summary.json, 표본은 multi_soak_samples.csv.
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cloud"))
sys.path.insert(0, str(ROOT / "tools"))
import long_live_worker as w  # noqa: E402
from live_soak import analyze_growth, handle_count, make_samples, rss_bytes  # noqa: E402

PROFILES = ("senior", "chili")


def cpu_seconds(pid: int) -> float | None:
    """프로세스 누적 CPU 시간 (user+kernel)."""
    if os.name == "nt":
        try:
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            h = k32.OpenProcess(0x1000, False, pid)
            if not h:
                return None
            try:
                c, e, k, u = (wintypes.FILETIME() for _ in range(4))
                if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                    return None
                ft = lambda t: (t.dwHighDateTime << 32 | t.dwLowDateTime) / 1e7  # noqa: E731
                return ft(k) + ft(u)
            finally:
                k32.CloseHandle(h)
        except Exception:
            return None
    try:
        parts = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return (int(parts[11]) + int(parts[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def ffmpeg_children() -> list[int]:
    """이 프로세스가 띄운 ffmpeg (끝난 뒤 남아 있으면 고아/zombie)."""
    sys.path.insert(0, str(ROOT / "tests"))
    from conftest import _ffmpeg_children_of
    return _ffmpeg_children_of(os.getpid())


def setup(out: Path, ffmpeg: str, ffprobe: str, inputs: list[Path]):
    etc, media, state, logs = out / "etc", out / "media", out / "state", out / "logs"
    for d in (etc, media, state, logs):
        d.mkdir(parents=True, exist_ok=True)
    names = []
    for p in inputs:
        if not (media / p.name).exists():
            shutil.copy2(p, media / p.name)  # 공용 media 1벌 (채널별 복사 없음)
        names.append(p.name)
    workers = {}
    for n, pid in enumerate(PROFILES + ("third",)):
        paths = w.profile_paths(pid, etc_dir=etc, state_dir=state, log_dir=logs)
        paths["config"].parent.mkdir(parents=True, exist_ok=True)
        order = names if n % 2 == 0 else list(reversed(names))  # 채널마다 다른 순서
        paths["config"].write_text(json.dumps({"schema_version": 2, "media": order if len(order) > 1 else order[0],
                                               "ingest_url": "rtmp://127.0.0.1/soak", "mode": "copy",
                                               "session_id": f"soak_{pid}"}), encoding="utf-8")
        paths["key"].write_text(f"soak-{pid}-dummy\n", encoding="utf-8")
        workers[pid] = w.Worker(config_path=paths["config"], key_path=paths["key"], media_dir=media,
                                state_dir=paths["state"], lock_path=paths["lock"], ffmpeg=ffmpeg, ffprobe=ffprobe,
                                debug_output=os.devnull, profile=pid, slot_dir=state / "slots")
    return workers, state


def run(args) -> dict:
    ffmpeg = args.ffmpeg or shutil.which("ffmpeg")
    ffprobe = args.ffprobe or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SystemExit("ffmpeg/ffprobe를 찾을 수 없습니다.")
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    inputs = [Path(x).resolve() for x in args.inputs] if args.inputs else make_samples(
        out, ffmpeg, 2, args.sample_seconds, bitrate=args.bitrate)
    workers, state = setup(out, ffmpeg, ffprobe, inputs)
    threads = {pid: threading.Thread(target=workers[pid].run, name=f"soak-{pid}", daemon=True) for pid in PROFILES}
    for t in threads.values():
        t.start()
    end_wait = time.monotonic() + 30
    while time.monotonic() < end_wait and not all(workers[p].state == "RUNNING" for p in PROFILES):
        time.sleep(0.2)
    third = workers["third"]
    third_rc = third.run()  # 세 번째 채널: 거부되어야 함
    third_result = {"rc": third_rc, "state": third.state, "error": third.last_error}
    duration = args.seconds if args.seconds else args.minutes * 60
    rows: list[dict] = []
    fields = ["elapsed_s"] + [f"{p}_{k}" for p in PROFILES for k in
                              ("state", "rss", "handles", "cpu_pct", "speed", "reconnects", "out_time")] + ["py_rss",
                                                                                                         "ffmpeg_count"]
    last_cpu: dict[str, tuple[float, float]] = {}
    t0 = time.monotonic()
    try:
        with open(out / "multi_soak_samples.csv", "w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=fields)
            wr.writeheader()
            while True:
                elapsed = time.monotonic() - t0
                row = {"elapsed_s": round(elapsed, 1), "py_rss": rss_bytes(os.getpid()),
                       "ffmpeg_count": len(ffmpeg_children())}
                for p in PROFILES:
                    wk = workers[p]
                    snap = wk.snapshot()
                    proc = wk.proc
                    pid = getattr(proc, "pid", None)
                    cpu = cpu_seconds(pid) if pid else None
                    pct = None
                    prev = last_cpu.get(p)
                    if cpu is not None and prev is not None and prev[0] == pid and elapsed > prev[2]:
                        pct = round(100 * (cpu - prev[1]) / (elapsed - prev[2]), 2)
                    if cpu is not None:
                        last_cpu[p] = (pid, cpu, elapsed)
                    row.update({f"{p}_state": snap["state"], f"{p}_rss": rss_bytes(pid) if pid else None,
                                f"{p}_handles": handle_count(pid) if pid else None, f"{p}_cpu_pct": pct,
                                f"{p}_speed": snap["speed"], f"{p}_reconnects": snap["reconnects"],
                                f"{p}_out_time": snap["out_time_seconds"]})
                rows.append(row)
                wr.writerow(row)
                f.flush()
                if elapsed >= duration:
                    break
                time.sleep(min(args.interval, max(0.1, duration - elapsed)))
    except KeyboardInterrupt:
        print("중지 요청 — 정리 중")
    finally:
        for p in PROFILES:
            workers[p].request_stop()
        for t in threads.values():
            t.join(30)
    time.sleep(1.0)
    leftover = ffmpeg_children()
    warmup = max(10.0, 0.02 * duration)
    steady = [r for r in rows if r["elapsed_s"] >= warmup] or rows
    summary = {"requested_seconds": duration, "measured_seconds": rows[-1]["elapsed_s"] if rows else 0,
               "samples": len(rows), "inputs": [p.name for p in inputs], "bitrate": args.bitrate,
               "both_running_all_samples": all(r[f"{p}_state"] == "RUNNING" for r in steady for p in PROFILES),
               "max_ffmpeg_count": max((r["ffmpeg_count"] for r in rows), default=0),
               "third_channel": third_result,
               "python_rss": analyze_growth([r["py_rss"] for r in steady]),
               "leftover_ffmpeg_after_stop": leftover,
               "slots_after_stop": w.slot_owners(state / "slots")}
    for p in PROFILES:
        cpus = [r[f"{p}_cpu_pct"] for r in steady if r[f"{p}_cpu_pct"] is not None]
        speeds = [r[f"{p}_speed"] for r in steady if r[f"{p}_speed"] is not None]
        summary[p] = {"final_state": workers[p].state, "reconnects": rows[-1][f"{p}_reconnects"] if rows else None,
                      "last_out_time": rows[-1][f"{p}_out_time"] if rows else None,
                      "cpu_pct_avg": round(sum(cpus) / len(cpus), 2) if cpus else None,
                      "cpu_pct_max": max(cpus) if cpus else None,
                      "speed_min": min(speeds) if speeds else None,
                      "rss": analyze_growth([r[f"{p}_rss"] for r in steady]),
                      "handles": analyze_growth([r[f"{p}_handles"] for r in steady], min_abs=50)}
    (out / "multi_soak_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="2채널 동시 Cloud worker soak (실제 YouTube/OCI 연결 없음)")
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--seconds", type=float, default=0.0, help="짧은 smoke용 (지정 시 --minutes 무시)")
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--inputs", nargs="*")
    ap.add_argument("--sample-seconds", type=int, default=20)
    ap.add_argument("--bitrate", default="6000k", help="샘플 영상 비트레이트 (실제 채널 6.3 Mbps 근사)")
    ap.add_argument("--out", default="multi_soak_out")
    ap.add_argument("--ffmpeg")
    ap.add_argument("--ffprobe")
    summary = run(ap.parse_args(argv))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    bad = [p for p in PROFILES if summary[p]["rss"].get("verdict") == "SUSPECT"
           or summary[p]["handles"].get("verdict") == "SUSPECT" or summary[p]["final_state"] == "FAILED"
           or summary[p]["reconnects"]]
    ok = (not bad and summary["both_running_all_samples"] and summary["third_channel"]["rc"] == w.EXIT_CONFIG
          and not summary["leftover_ffmpeg_after_stop"] and not summary["slots_after_stop"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
