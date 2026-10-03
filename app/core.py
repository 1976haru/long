from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Iterable, Sequence


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    duration: float
    size: int
    width: int
    height: int
    fps: float
    video_codec: str
    audio_codec: str
    pix_fmt: str
    profile: str
    sample_rate: int
    channels: int


@dataclass(frozen=True)
class BuildPlan:
    sequence: tuple[Path, ...]
    expected_duration: float
    expected_size: int
    cycles: int
    trim_to: float | None
    mode: str


@dataclass(frozen=True)
class BuildResult:
    output: Path
    duration: float
    size: int
    elapsed: float


class BuildCancelled(RuntimeError):
    pass


class BuildError(RuntimeError):
    pass


def creationflags_no_window() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def parse_fps(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    if "/" in value:
        a, b = value.split("/", 1)
        try:
            den = float(b)
            return float(a) / den if den else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def target_seconds(hours: int, minutes: int) -> int:
    if hours < 0 or minutes < 0 or minutes > 59:
        raise ValueError("시간 입력값이 올바르지 않습니다.")
    total = hours * 3600 + minutes * 60
    if total <= 0:
        raise ValueError("목표 시간은 0보다 커야 합니다.")
    if total > 12 * 3600:
        raise ValueError("시간 기준은 최대 12시간까지 지원합니다.")
    return total


def probe_video(path: Path, ffprobe: Path) -> VideoInfo:
    path = Path(path)
    cmd = [
        str(ffprobe),
        "-v", "error",
        "-show_entries",
        "format=duration,size:stream=index,codec_type,codec_name,width,height,avg_frame_rate,pix_fmt,profile,sample_rate,channels",
        "-of", "json",
        str(path),
    ]
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=creationflags_no_window(),
        )
    except OSError as e:
        raise BuildError(f"ffprobe를 실행할 수 없습니다: {e}") from e
    if p.returncode != 0:
        raise BuildError(f"영상을 읽을 수 없습니다: {path.name}\n{p.stderr.strip()}")
    try:
        data = json.loads(p.stdout)
        fmt = data.get("format", {})
        streams = data.get("streams", [])
        v = next(s for s in streams if s.get("codec_type") == "video")
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        duration = float(fmt.get("duration") or 0)
        size = int(fmt.get("size") or path.stat().st_size)
        if duration <= 0:
            raise ValueError("duration <= 0")
        return VideoInfo(
            path=path.resolve(),
            duration=duration,
            size=size,
            width=int(v.get("width") or 0),
            height=int(v.get("height") or 0),
            fps=parse_fps(v.get("avg_frame_rate")),
            video_codec=str(v.get("codec_name") or ""),
            audio_codec=str(a.get("codec_name") or "") if a else "",
            pix_fmt=str(v.get("pix_fmt") or ""),
            profile=str(v.get("profile") or ""),
            sample_rate=int(a.get("sample_rate") or 0) if a else 0,
            channels=int(a.get("channels") or 0) if a else 0,
        )
    except (ValueError, KeyError, StopIteration, TypeError) as e:
        raise BuildError(f"영상 정보 분석에 실패했습니다: {path.name}") from e


def strict_copy_compatibility(infos: Sequence[VideoInfo]) -> tuple[bool, str]:
    if not infos:
        return False, "SET 영상이 없습니다."
    base = infos[0]
    attrs = [
        ("video_codec", "영상 코덱"),
        ("audio_codec", "오디오 코덱"),
        ("width", "가로 해상도"),
        ("height", "세로 해상도"),
        ("pix_fmt", "픽셀 형식"),
        ("sample_rate", "오디오 샘플레이트"),
        ("channels", "오디오 채널"),
    ]
    for other in infos[1:]:
        for attr, label in attrs:
            if getattr(base, attr) != getattr(other, attr):
                return False, f"{label}이 서로 다릅니다: {base.path.name} / {other.path.name}"
        if abs(base.fps - other.fps) > 0.01:
            return False, f"FPS가 서로 다릅니다: {base.path.name} / {other.path.name}"
    return True, "모든 SET이 무손실 연결 규격과 호환됩니다."


def build_round_plan(infos: Sequence[VideoInfo], rounds: int) -> BuildPlan:
    if not infos:
        raise ValueError("SET 영상이 없습니다.")
    if rounds < 1 or rounds > 100:
        raise ValueError("회차는 1~100회까지 선택할 수 있습니다.")
    one_duration = sum(x.duration for x in infos)
    one_size = sum(x.size for x in infos)
    seq = tuple(x.path for _ in range(rounds) for x in infos)
    return BuildPlan(
        sequence=seq,
        expected_duration=one_duration * rounds,
        expected_size=one_size * rounds,
        cycles=rounds,
        trim_to=None,
        mode="rounds",
    )


def build_time_plan(infos: Sequence[VideoInfo], target: float) -> BuildPlan:
    if not infos:
        raise ValueError("SET 영상이 없습니다.")
    if target <= 0:
        raise ValueError("목표 시간은 0보다 커야 합니다.")
    one_duration = sum(x.duration for x in infos)
    if one_duration <= 0:
        raise ValueError("SET 영상 길이를 계산할 수 없습니다.")
    cycles = max(1, math.ceil(target / one_duration))
    seq = tuple(x.path for _ in range(cycles) for x in infos)
    avg_bps = sum(x.size for x in infos) / one_duration
    expected_size = int(avg_bps * target)
    return BuildPlan(
        sequence=seq,
        expected_duration=float(target),
        expected_size=expected_size,
        cycles=cycles,
        trim_to=float(target),
        mode="time",
    )


def default_output_name_rounds(input_path: Path, rounds: int) -> str:
    return f"{Path(input_path).stem}_{rounds}R_FINAL.mp4"


def default_output_name_time(input_path: Path, hours: int, minutes: int) -> str:
    suffix = f"{hours}H" if not minutes else f"{hours}H{minutes:02d}M"
    return f"{Path(input_path).stem}_{suffix}_FINAL.mp4"


def ensure_unique_output(path: Path) -> Path:
    path = Path(path)
    if not path.exists():
        return path
    for i in range(2, 10000):
        cand = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not cand.exists():
            return cand
    raise BuildError("사용 가능한 출력 파일명을 만들 수 없습니다.")


def escape_concat_path(path: Path) -> str:
    s = str(path.resolve()).replace("\\", "/")
    return s.replace("'", "'\\''")


def write_concat_file(paths: Iterable[Path], target: Path) -> None:
    lines = [f"file '{escape_concat_path(p)}'" for p in paths]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def free_bytes(path: Path) -> int:
    base = Path(path)
    base.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(base).free


def enough_disk_space(output_dir: Path, expected_size: int) -> tuple[bool, int, int]:
    free = free_bytes(output_dir)
    margin = max(int(expected_size * 0.05), 1024**3)
    required = expected_size + margin
    return free >= required, free, required


def _same_stream_signature(a: VideoInfo, b: VideoInfo) -> bool:
    return (
        a.video_codec == b.video_codec
        and a.audio_codec == b.audio_codec
        and a.width == b.width
        and a.height == b.height
        and abs(a.fps - b.fps) <= 0.01
        and a.pix_fmt == b.pix_fmt
        and a.sample_rate == b.sample_rate
        and a.channels == b.channels
    )


def verify_stream_copy(source: VideoInfo, output: VideoInfo, expected_duration: float, segment_count: int) -> tuple[bool, str]:
    if not _same_stream_signature(source, output):
        return False, "완성 영상의 코덱/해상도/FPS/오디오 규격이 원본과 달라졌습니다."
    tolerance = max(2.0, min(8.0, segment_count * 0.15))
    if abs(output.duration - expected_duration) > tolerance:
        return False, (
            f"완성 영상 길이 검증 실패: 예상 {format_duration(expected_duration)}, "
            f"실제 {format_duration(output.duration)}"
        )
    return True, "무손실 스트림 및 재생시간 검증 완료"


def _terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_concat_copy(
    *,
    ffmpeg: Path,
    ffprobe: Path,
    infos: Sequence[VideoInfo],
    plan: BuildPlan,
    output: Path,
    cancel_event: Event,
    progress_cb: Callable[[float, str], None] | None = None,
) -> BuildResult:
    if not infos:
        raise BuildError("SET 영상이 없습니다.")
    ok, reason = strict_copy_compatibility(infos)
    if not ok:
        raise BuildError(reason + "\n화질 보존을 위해 자동 재인코딩하지 않습니다.")

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise BuildError(f"출력 파일이 이미 존재합니다: {output.name}")

    temp_dir = output.parent / f".PLVM_TEMP_{uuid.uuid4().hex[:10]}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    concat_txt = temp_dir / "concat.txt"
    part = output.with_name(output.name + ".part.mp4")
    started = time.monotonic()
    proc: subprocess.Popen | None = None
    try:
        write_concat_file(plan.sequence, concat_txt)
        cmd = [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_txt),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c", "copy",
        ]
        if plan.trim_to is not None:
            cmd += ["-t", f"{plan.trim_to:.3f}"]
        cmd += [
            "-progress", "pipe:1",
            "-nostats",
            str(part),
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags_no_window(),
        )
        recent: list[str] = []
        assert proc.stdout is not None
        while True:
            if cancel_event.is_set():
                _terminate_process(proc)
                raise BuildCancelled("사용자가 작업을 중지했습니다.")
            line = proc.stdout.readline()
            if line == "" and proc.poll() is not None:
                break
            line = line.strip()
            if not line:
                continue
            recent.append(line)
            recent = recent[-20:]
            if line.startswith("out_time_us="):
                try:
                    done = int(line.split("=", 1)[1]) / 1_000_000
                    frac = min(0.995, max(0.0, done / max(plan.expected_duration, 0.001)))
                    if progress_cb:
                        progress_cb(frac, f"무손실 연결 중 · {format_duration(done)} / {format_duration(plan.expected_duration)}")
                except ValueError:
                    pass
        rc = proc.wait()
        if rc != 0:
            raise BuildError("FFmpeg 무손실 연결에 실패했습니다.\n" + "\n".join(recent[-8:]))
        if cancel_event.is_set():
            raise BuildCancelled("사용자가 작업을 중지했습니다.")
        if not part.exists() or part.stat().st_size == 0:
            raise BuildError("완성 임시파일이 생성되지 않았습니다.")

        if progress_cb:
            progress_cb(0.997, "완성 파일 무손실 검증 중")
        out_info = probe_video(part, ffprobe)
        valid, message = verify_stream_copy(infos[0], out_info, plan.expected_duration, len(plan.sequence))
        if not valid:
            raise BuildError(message)
        os.replace(part, output)
        if progress_cb:
            progress_cb(1.0, "완료")
        return BuildResult(
            output=output,
            duration=out_info.duration,
            size=out_info.size,
            elapsed=time.monotonic() - started,
        )
    finally:
        if proc and proc.poll() is None:
            _terminate_process(proc)
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
        shutil.rmtree(temp_dir, ignore_errors=True)
