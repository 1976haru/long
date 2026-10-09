"""LIVE READY 분석기 + LIVE READY 파일 만들기.

LIVE READY = 재인코딩 없이(DIRECT COPY) YouTube로 반복 송출할 수 있는 MP4.
- 분석은 ffprobe만 사용한다. Python은 영상 frame을 디코딩하지 않는다.
- keyframe/타임스탬프는 초반 대표 구간(packet 헤더)만 읽는다. 긴 영상 전체를 검사하지 않는다.
- LIVE READY 파일 만들기는 PC에서 딱 한 번 재인코딩한다 (원본은 덮어쓰지 않음).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable

from .core import creationflags_no_window, ensure_unique_output, format_duration, parse_fps

SAMPLE_SECONDS = 60
MAX_KEYFRAME_SECONDS = 4.0  # YouTube 권장 최대
IDEAL_KEYFRAME_SECONDS = 2.0
MAX_WIDTH, MAX_HEIGHT = 3840, 2160
MAX_VIDEO_KBPS = 51000
HIGH_VIDEO_KBPS = 15000
AV_GAP_WARN_SECONDS = 0.5
READY_SUFFIX = "_LIVE_READY"


@dataclass(frozen=True)
class LiveReadyIssue:
    code: str
    message: str
    blocking: bool = True


@dataclass
class LiveReadyReport:
    path: Path
    ok_items: list[str] = field(default_factory=list)
    issues: list[LiveReadyIssue] = field(default_factory=list)
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    video_kbps: float | None = None
    keyframe_max: float | None = None
    keyframe_avg: float | None = None

    @property
    def ready(self) -> bool:
        """DIRECT COPY 가능 여부."""
        return not any(i.blocking for i in self.issues)

    @property
    def warnings(self) -> list[LiveReadyIssue]:
        return [i for i in self.issues if not i.blocking]

    def summary_lines(self) -> list[str]:
        lines = ["✓ LIVE READY" if self.ready else "⚠ LIVE READY 아님"]
        lines += [f"✓ {t}" for t in self.ok_items]
        if self.ready:
            lines.append("✓ 무재인코딩 LIVE 가능 (DIRECT COPY · CPU/RAM 매우 낮음)")
        for i in self.issues:
            lines.append(("✗ " if i.blocking else "⚠ ") + i.message)
        return lines


def _run_json(cmd: list[str], runner) -> dict:
    p = runner(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
               check=False, creationflags=creationflags_no_window())
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "").strip()[-300:] or "ffprobe 실패")
    return json.loads(p.stdout or "{}")


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x


def _kbps_text(kbps: float) -> str:
    return f"{kbps / 1000:.1f} Mbps" if kbps >= 1000 else f"{kbps:.0f} kbps"


def analyze_live_ready(
    path: Path,
    ffprobe: Path,
    *,
    sample_seconds: int = SAMPLE_SECONDS,
    runner: Callable = subprocess.run,
) -> LiveReadyReport:
    path = Path(path)
    r = LiveReadyReport(path=path)
    if not path.is_file():
        r.issues.append(LiveReadyIssue("missing", f"파일을 찾을 수 없습니다: {path.name}"))
        return r
    try:
        meta = _run_json([
            str(ffprobe), "-v", "error",
            "-show_entries",
            "format=duration,bit_rate:stream=index,codec_type,codec_name,profile,pix_fmt,width,height,"
            "avg_frame_rate,r_frame_rate,bit_rate,sample_rate,channels,start_time,duration",
            "-of", "json", str(path),
        ], runner)
    except (RuntimeError, ValueError, OSError) as e:
        r.issues.append(LiveReadyIssue("probe", f"영상 분석 실패: {str(e)[:200]}"))
        return r

    streams = meta.get("streams", [])
    fmt = meta.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    r.duration = _f(fmt.get("duration")) or 0.0
    if v is None:
        r.issues.append(LiveReadyIssue("no_video", "영상 stream이 없습니다."))
        return r
    if r.duration <= 0:
        r.issues.append(LiveReadyIssue("zero", "재생 시간이 0초입니다."))
        return r

    # --- video ---
    r.video_codec = str(v.get("codec_name") or "")
    r.width, r.height = int(v.get("width") or 0), int(v.get("height") or 0)
    r.fps = parse_fps(v.get("avg_frame_rate")) or parse_fps(v.get("r_frame_rate"))
    pix = str(v.get("pix_fmt") or "")
    if r.video_codec == "h264" and pix == "yuv420p":
        r.ok_items.append("H.264 / yuv420p")
    else:
        if r.video_codec != "h264":
            r.issues.append(LiveReadyIssue("vcodec", f"영상 코덱이 H.264가 아닙니다 ({r.video_codec or '알 수 없음'})."))
        if pix != "yuv420p":
            r.issues.append(LiveReadyIssue("pix_fmt", f"픽셀 형식이 yuv420p가 아닙니다 ({pix or '알 수 없음'})."))
    if r.width > MAX_WIDTH or r.height > MAX_HEIGHT or r.width <= 0:
        r.issues.append(LiveReadyIssue("resolution", f"해상도 {r.width}×{r.height}는 지원 범위를 벗어납니다."))
    elif not (1 <= r.fps <= 60):
        r.issues.append(LiveReadyIssue("fps", f"FPS {r.fps:.2f}는 지원 범위(1~60)를 벗어납니다."))
    else:
        r.ok_items.append(f"{r.width}×{r.height} / {r.fps:.2f}fps".replace(".00fps", "fps"))
    avg, rf = parse_fps(v.get("avg_frame_rate")), parse_fps(v.get("r_frame_rate"))
    if avg and rf and abs(avg - rf) > 0.5:
        r.issues.append(LiveReadyIssue("vfr", f"가변 프레임(VFR) 영상으로 보입니다 ({avg:.2f}/{rf:.2f}fps).", blocking=False))

    vb = _f(v.get("bit_rate"))
    if vb is None:
        tb = _f(fmt.get("bit_rate"))
        vb = tb - 128_000 if tb else None
    if vb:
        r.video_kbps = vb / 1000
        if r.video_kbps > MAX_VIDEO_KBPS:
            r.issues.append(LiveReadyIssue("bitrate", f"영상 비트레이트 {r.video_kbps / 1000:.1f} Mbps는 YouTube 최대치를 넘습니다."))
        elif r.video_kbps > HIGH_VIDEO_KBPS:
            r.issues.append(LiveReadyIssue("bitrate_high", f"영상 비트레이트 {r.video_kbps / 1000:.1f} Mbps — 업로드 속도가 충분한지 확인하세요.", blocking=False))
        else:
            r.ok_items.append(f"영상 비트레이트 약 {_kbps_text(r.video_kbps)}")

    # --- audio ---
    if a is None:
        r.issues.append(LiveReadyIssue("no_audio", "오디오가 없습니다 (음악 LIVE에는 오디오가 필요합니다)."))
    else:
        r.audio_codec = str(a.get("codec_name") or "")
        r.sample_rate = int(_f(a.get("sample_rate")) or 0)
        r.channels = int(a.get("channels") or 0)
        bad = []
        if r.audio_codec != "aac":
            bad.append(LiveReadyIssue("acodec", f"오디오 코덱이 AAC가 아닙니다 ({r.audio_codec or '알 수 없음'})."))
        if r.sample_rate not in (44100, 48000):
            bad.append(LiveReadyIssue("sample_rate", f"오디오 샘플레이트 {r.sample_rate}Hz (44.1k/48k 필요)."))
        if r.channels != 2:
            bad.append(LiveReadyIssue("channels", f"오디오 채널 {r.channels}개 (스테레오 필요)."))
        r.issues += bad
        if not bad:
            r.ok_items.append(f"AAC {r.sample_rate / 1000:g}kHz 스테레오")
        vd, ad = _f(v.get("duration")), _f(a.get("duration"))
        if vd and ad and abs(vd - ad) > AV_GAP_WARN_SECONDS:
            r.issues.append(LiveReadyIssue(
                "av_length", f"영상/소리 길이 차이 {abs(vd - ad):.1f}초 — 반복 이음새마다 공백이 생깁니다.", blocking=False))

    # --- keyframe / timestamp (초반 대표 구간 packet 헤더만) ---
    try:
        pk = _run_json([
            str(ffprobe), "-v", "error", "-select_streams", "v:0",
            "-read_intervals", f"%+{int(sample_seconds)}",
            "-show_entries", "packet=pts_time,dts_time,flags",
            "-of", "json", str(path),
        ], runner)
    except (RuntimeError, ValueError, OSError) as e:
        r.issues.append(LiveReadyIssue("packets", f"keyframe 분석 실패: {str(e)[:200]}"))
        return r
    packets = pk.get("packets", [])
    keys = sorted(t for t in (_f(p.get("pts_time")) for p in packets if "K" in str(p.get("flags", ""))) if t is not None)
    dts = [t for t in (_f(p.get("dts_time")) for p in packets) if t is not None]
    if any(b < a_ for a_, b in zip(dts, dts[1:])):
        r.issues.append(LiveReadyIssue("timestamps", "영상 타임스탬프가 뒤로 가는 구간이 있습니다."))
    sampled = min(r.duration, float(sample_seconds))
    if len(keys) >= 2:
        gaps = [b - a_ for a_, b in zip(keys, keys[1:])]
        # 마지막 keyframe 이후 구간도 간격으로 본다 (샘플 끝까지 keyframe이 없으면 간격이 긴 것)
        tail = sampled - keys[-1] if sampled < r.duration else 0.0
        r.keyframe_max = max(gaps + [tail])
        r.keyframe_avg = sum(gaps) / len(gaps)
    elif len(keys) == 1 and sampled > MAX_KEYFRAME_SECONDS:
        r.keyframe_max = r.keyframe_avg = sampled
    if r.keyframe_max is None:
        if r.duration > MAX_KEYFRAME_SECONDS:
            r.issues.append(LiveReadyIssue("keyframe", "keyframe 간격을 확인할 수 없습니다."))
        else:
            r.ok_items.append("Keyframe 정상 (짧은 영상)")
    elif r.keyframe_max > MAX_KEYFRAME_SECONDS + 0.05:
        r.issues.append(LiveReadyIssue("keyframe", f"Keyframe 간격 {r.keyframe_max:.0f}초 (4초 이하 필요, 2초 권장)."))
    else:
        r.ok_items.append(f"Keyframe 약 {r.keyframe_avg:.0f}초" if r.keyframe_avg else "Keyframe 정상")
    return r


# ---------------- LIVE READY 파일 만들기 (PC에서 1회 변환) ----------------

def live_ready_output_path(src: Path) -> Path:
    src = Path(src)
    stem = src.stem if not src.stem.endswith(READY_SUFFIX) else src.stem + "_2"
    return ensure_unique_output(src.with_name(f"{stem}{READY_SUFFIX}.mp4"))


def ready_video_kbps(height: int) -> int:
    return 5000 if 0 < height <= 720 else 8000


def build_live_ready_command(*, ffmpeg: Path, src: Path, dst: Path, height: int, fps: int = 30) -> list[str]:
    """H.264 yuv420p / 30fps / 2초 GOP / AAC 128k 44.1kHz stereo. 1080p 초과만 1080p로 축소."""
    vb = ready_video_kbps(height)
    gop = fps * 2
    vf = ["-vf", "scale=-2:1080"] if height > 1080 else []
    return [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(src),
        "-map", "0:v:0", "-map", "0:a:0",
        *vf,
        "-c:v", "libx264", "-preset", "medium", "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-b:v", f"{vb}k", "-maxrate", f"{vb}k", "-bufsize", f"{vb * 2}k",
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        # 소리를 영상 길이에 맞춘다: 반복 이음새 공백/싱크 어긋남 방지
        "-af", "apad", "-shortest",
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
        "-f", "mp4", str(dst),
    ]


class LiveReadyCancelled(RuntimeError):
    pass


def make_live_ready_file(
    *,
    ffmpeg: Path,
    ffprobe: Path,
    src: Path,
    height: int,
    duration: float,
    cancel: Event,
    progress_cb: Callable[[float, str], None] | None = None,
    popen=subprocess.Popen,
    fps: int = 30,
) -> Path:
    """원본 → *_LIVE_READY.mp4. .part.mp4로 만들고 LIVE READY 검증 성공 후 최종 이름으로 교체."""
    src = Path(src)
    dst = live_ready_output_path(src)
    part = dst.with_name(dst.name + ".part.mp4")
    cmd = build_live_ready_command(ffmpeg=ffmpeg, src=src, dst=part, height=height, fps=fps)
    proc = None
    try:
        proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                     errors="replace", bufsize=1, creationflags=creationflags_no_window())
        assert proc.stdout is not None
        for line in proc.stdout:
            if cancel.is_set():
                break
            if line.startswith("out_time_us=") and progress_cb:
                try:
                    done = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                progress_cb(min(0.99, done / max(duration, 0.001)),
                            f"LIVE READY 변환 중 · {format_duration(done)} / {format_duration(duration)}")
        if cancel.is_set():
            proc.kill()
            proc.wait()
            raise LiveReadyCancelled("사용자가 변환을 중지했습니다.")
        rc = proc.wait()
        err = proc.stderr.read() if proc.stderr else ""
        if rc != 0 or not part.exists():
            raise RuntimeError("LIVE READY 변환 실패\n" + err.strip()[-500:])
        report = analyze_live_ready(part, ffprobe)
        if not report.ready:
            raise RuntimeError("변환 결과 검증 실패: " + "; ".join(i.message for i in report.issues if i.blocking))
        os.replace(part, dst)
        if progress_cb:
            progress_cb(1.0, "LIVE READY 파일 완성")
        return dst
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        for s in ((proc.stdout, proc.stderr) if proc is not None else ()):
            try:
                if s:
                    s.close()
            except OSError:
                pass
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
