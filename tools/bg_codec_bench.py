"""C07 — ④ 배경 만들기 인코딩 방식 비교 (같은 원본 구간으로 크기·화질·인코딩 시간).

사용:
    py -3 tools/bg_codec_bench.py <원본.mp4> --start 0 --seconds 60 --out D:\\...\\bench_dir

- 각 방식은 제작 경로와 똑같은 명령(app.bg_core.piece_command)으로 인코딩한다.
- 화질은 FFmpeg libvmaf(VMAF·PSNR·SSIM)와 CAMBI(그라데이션 띠 검출, 낮을수록 좋음)로 잰다.
  원본(기준)은 같은 구간을 같은 정규화(FPS·해상도·yuv420p)로 무손실(-qp 0)로 만든 파일이다.
- 영역별: '세밀' = 위쪽(나뭇가지·눈송이), '평탄' = 아래쪽(눈밭·안개 그라데이션). 영상 내용에 맞게 --detail/--smooth로 바꾼다.
- 원본 파일은 읽기만 한다. 결과 JSON/표는 --out 폴더에만 쓴다.
- Python은 프레임을 디코딩하지 않는다 (모든 계산은 FFmpeg 안에서).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import bg_core as B  # noqa: E402
from app.core import probe_video  # noqa: E402


def variants(base: B.OutputSpec) -> list[tuple[str, B.OutputSpec]]:
    """비교 대상 A~E (+참고 변형). 실제 제작 프로필과 같은 값을 쓴다."""
    out = [
        ("A_h264_crf18_fast", replace(base, codec="h264", crf=18, preset="fast")),
        ("B_h264_crf21_medium", replace(base, codec="h264", crf=21, preset="medium")),
        ("C_h264_crf24_medium", replace(base, codec="h264", crf=24, preset="medium")),
        ("D_h264_crf19_slow_cap8M", replace(base, codec="h264", crf=19, preset="slow", maxrate_kbps=8000, bufsize_kbps=16000)),
        ("D2_h264_crf19_medium_cap6M", replace(base, codec="h264", crf=19, preset="medium", maxrate_kbps=6000, bufsize_kbps=12000)),
        ("E_hevc_crf22_medium", replace(base, codec="hevc", crf=22, preset="medium")),
        ("E2_hevc_crf24_medium", replace(base, codec="hevc", crf=24, preset="medium")),
        ("E3_hevc_crf20_medium", replace(base, codec="hevc", crf=20, preset="medium")),
        ("G_h264_crf24_medium_cap8M", replace(base, codec="h264", crf=24, preset="medium", maxrate_kbps=8000, bufsize_kbps=16000)),
        ("F_h264_crf20_slow_cap8M", replace(base, codec="h264", crf=20, preset="slow", maxrate_kbps=8000, bufsize_kbps=16000)),
    ]
    for p in B.ENCODE_PROFILES:  # 프로그램에 실제로 들어간 선택지 (FHD H.264 / H.265)
        for codec in ("h264", "hevc"):
            s = B.choose_output_spec([_fake_info(base)], B.QUALITY_FHD, profile=p.key, codec=codec, hevc_available=True)
            out.append((f"APP_{codec}_{p.key}", s))
    return out


def _fake_info(spec: B.OutputSpec):
    from app.core import VideoInfo
    return VideoInfo(Path("x.mp4"), 600.0, 0, spec.width, spec.height, spec.fps, "h264", "", "yuv420p", "High", 0, 0)


def run(cmd: list[str], cwd: Path | None = None) -> float:
    t = time.monotonic()
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd)
    if p.returncode != 0:
        raise SystemExit("FFmpeg 실패:\n" + " ".join(cmd) + "\n" + p.stderr[-2000:])
    return time.monotonic() - t


def vmaf(ffmpeg: str, dist: Path, ref: Path, log: Path, crop: str | None = None) -> dict:
    pre = f",crop={crop}" if crop else ""
    feats = "feature=name=psnr|name=float_ssim|name=cambi"
    graph = (f"[0:v]setpts=PTS-STARTPTS{pre}[d];[1:v]setpts=PTS-STARTPTS{pre}[r];"
             f"[d][r]libvmaf={feats}:n_threads=8:log_fmt=json:log_path={log.name}")
    run([ffmpeg, "-hide_banner", "-v", "error", "-i", str(dist), "-i", str(ref), "-lavfi", graph, "-f", "null", "-"],
        cwd=log.parent)
    data = json.loads(log.read_text(encoding="utf-8"))
    pm = data["pooled_metrics"]
    frames = [f["metrics"]["vmaf"] for f in data["frames"]]
    frames.sort()
    low = frames[max(0, len(frames) // 100 - 1)] if frames else 0.0
    return {"vmaf": round(pm["vmaf"]["mean"], 2), "vmaf_1pct_low": round(low, 2),
            "psnr_y": round(pm["psnr_y"]["mean"], 2), "ssim": round(pm["float_ssim"]["mean"], 4),
            "cambi": round(pm["cambi"]["mean"], 3)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--detail", default="1920:540:0:0", help="세밀한 영역 crop w:h:x:y")
    ap.add_argument("--smooth", default="1920:300:0:780", help="평탄한 영역 crop w:h:x:y")
    ap.add_argument("--only", default="", help="쉼표로 구분한 방식 이름 접두사만 실행")
    ap.add_argument("--target-minutes", type=float, default=10.0)
    a = ap.parse_args()
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SystemExit("ffmpeg/ffprobe 없음")
    out = a.out
    out.mkdir(parents=True, exist_ok=True)
    info = probe_video(a.source, Path(ffprobe))
    base = B.choose_output_spec([info], B.QUALITY_FHD)
    piece = B.Piece("body", B.Segment(a.source.resolve(), a.start, a.seconds), None, a.seconds, "bench")
    ref = out / "REF_lossless.mkv"
    if not ref.exists():
        lossless = replace(base, codec="h264", crf=0, preset="ultrafast")
        cmd = B.piece_command(Path(ffmpeg), piece, lossless, ref)
        cmd = [x for x in cmd]
        i = cmd.index("-crf")
        cmd[i:i + 2] = ["-qp", "0"]
        i = cmd.index("-profile:v")
        cmd[i:i + 4] = ["-profile:v", "high444"]  # 무손실은 High 4:4:4 Predictive 필요 (화소 형식은 yuv420p 유지)
        run(cmd)
    results = []
    only = [x for x in a.only.split(",") if x]
    for name, spec in variants(base):
        if only and not any(name.startswith(o) for o in only):
            continue
        dst = out / f"{name}.mp4"
        cmd = B.piece_command(Path(ffmpeg), piece, spec, dst)
        el = run(cmd)
        size = dst.stat().st_size
        row = {"name": name, "codec": spec.codec, "crf": spec.crf, "preset": spec.preset,
               "maxrate_kbps": spec.maxrate_kbps, "bufsize_kbps": spec.bufsize_kbps,
               "seconds": a.seconds, "size": size, "mbps": round(size * 8 / a.seconds / 1e6, 2),
               "proj_10min_mb": round(size / a.seconds * a.target_minutes * 60 / 1024**2, 1),
               "encode_s": round(el, 1), "speed_x": round(a.seconds / el, 2),
               "est_bps_upper": round(spec.bytes_per_second * 8 / 1e6, 2)}
        row["full"] = vmaf(ffmpeg, dst, ref, out / f"{name}.full.json")
        row["detail"] = vmaf(ffmpeg, dst, ref, out / f"{name}.detail.json", a.detail)
        row["smooth"] = vmaf(ffmpeg, dst, ref, out / f"{name}.smooth.json", a.smooth)
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    ref_cambi = vmaf(ffmpeg, ref, ref, out / "REF.smooth.json", a.smooth)["cambi"]
    (out / "results.json").write_text(json.dumps({"source": str(a.source), "start": a.start, "seconds": a.seconds,
                                                   "ref_smooth_cambi": ref_cambi, "rows": results},
                                                  ensure_ascii=False, indent=1), encoding="utf-8")
    print("REF smooth CAMBI", ref_cambi)


if __name__ == "__main__":
    main()
