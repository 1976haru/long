"""④ 내 영상으로 배경 만들기 — 디졸브(xfade) 편집 엔진.

기존 ① 영상 늘리기의 무손실 경로(core.run_concat_copy, FFmpeg -c copy)는 바꾸지 않는다.
재인코딩은 이 신규 경로에서만, 그리고 '한 바퀴(cycle)' 분량만 1번 한다.

제작 순서 (FFmpeg는 항상 1개씩, 입력은 최대 2개 → 메모리 사용량이 클립 수와 무관하게 일정)
1) 클립마다 본문 조각(body)과, 다음 클립으로 넘어가는 디졸브 조각(xfade)을 순서대로 인코딩한다.
   마지막 클립의 디졸브는 첫 클립의 머리로 넘어가므로 cycle 끝과 다음 cycle 시작이 이어진다.
   각 조각은 해상도/FPS/픽셀 포맷/SAR/timebase를 같게 정규화하고 오디오는 넣지 않는다.
2) 조각들을 -c copy로 이어 cycle.mp4 를 만들고 ffprobe + 디코드 검사로 확인한다.
3) cycle.mp4 를 core.run_concat_copy(-c copy)로 목표 길이만큼 반복 연결한다
   (.part.mp4 로 만든 뒤 ffprobe 검증 성공 시 최종 파일로 교체).
"""
from __future__ import annotations

import math
import shutil
import subprocess
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event
from typing import Callable, Sequence

from .core import (
    BuildCancelled, BuildError, BuildPlan, VideoInfo, creationflags_no_window, enough_disk_space,
    format_duration, probe_video, run_concat_copy, write_concat_file,
)

TARGET_MINUTES = (3, 10, 20, 30, 60)
DISSOLVE_CHOICES = (0.5, 1.0, 1.5, 2.0)
DEFAULT_DISSOLVE = 1.0
QUALITY_FHD = "FHD"
QUALITY_4K = "4K"
STANDARD_HEIGHTS = (2160, 1440, 1080, 720, 540, 480, 360)
TEMP_PREFIX = ".PLVM_BG_TEMP_"
GUARD_OWNER = "bg"


@dataclass
class Clip:
    """사용자가 고른 MP4 1개 + 사용할 구간. 원본 파일은 읽기만 한다."""
    info: VideoInfo
    start: float = 0.0
    end: float | None = None  # None = 끝까지
    enabled: bool = True

    @property
    def path(self) -> Path:
        return self.info.path

    @property
    def used_end(self) -> float:
        end = self.info.duration if self.end is None else self.end
        return max(0.0, min(end, self.info.duration))

    @property
    def used_start(self) -> float:
        return max(0.0, min(self.start, self.info.duration))

    @property
    def used_length(self) -> float:
        return max(0.0, self.used_end - self.used_start)


@dataclass(frozen=True)
class OutputSpec:
    width: int
    height: int
    fps: float
    codec: str  # "h264" | "hevc"
    crf: int
    preset: str
    label: str
    notes: tuple[str, ...] = ()

    @property
    def encoder(self) -> str:
        return "libx265" if self.codec == "hevc" else "libx264"

    @property
    def bytes_per_second(self) -> float:
        """예상 용량 계산용 평균 비트레이트(CRF라 실제와 다를 수 있음, 넉넉하게 잡는다)."""
        mbps_h264 = {2160: 45.0, 1440: 20.0, 1080: 12.0, 720: 6.0}
        base = next((v for h, v in sorted(mbps_h264.items()) if self.height <= h), 45.0)
        base *= max(1.0, self.fps / 30.0) ** 0.6
        if self.codec == "hevc":
            base *= 0.55
        return base * 1_000_000 / 8


@dataclass(frozen=True)
class Segment:
    path: Path
    start: float
    length: float


@dataclass(frozen=True)
class Piece:
    kind: str  # "body" | "xfade"
    a: Segment
    b: Segment | None
    length: float
    label: str


@dataclass(frozen=True)
class BgPlan:
    pieces: tuple[Piece, ...]
    dissolve: float
    cycle_duration: float
    target: float
    repeats: int
    spec: OutputSpec
    expected_size: int
    work_size: int  # 임시 조각 + cycle 파일
    clip_count: int
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def required_bytes(self) -> int:
        return self.expected_size + self.work_size


@dataclass(frozen=True)
class BgResult:
    output: Path
    duration: float
    size: int
    width: int
    height: int
    fps: float
    cycle_duration: float
    repeats: int
    elapsed: float


# ---------------- 출력 규격 ----------------

def _even(x: float) -> int:
    v = int(round(x))
    return v - (v % 2)


def _std_fps(fps: float) -> float:
    if fps <= 0:
        return 30.0
    for std in (23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0):
        if abs(fps - std) < 0.015:
            return std
    return min(60.0, round(fps, 3))


def choose_output_spec(infos: Sequence[VideoInfo], quality: str = QUALITY_FHD, *, hevc_available: bool = True) -> OutputSpec:
    """FHD H.264 고화질이 기본. 원본보다 큰 해상도로 무의미하게 키우지 않는다(16:9 기준)."""
    if not infos:
        raise ValueError("영상이 없습니다.")
    notes: list[str] = []
    want = 2160 if quality == QUALITY_4K else 1080
    native = max(min(i.height, i.width * 9 / 16) for i in infos)  # 16:9로 채웠을 때 원본이 가진 세로 화소
    height = next((h for h in STANDARD_HEIGHTS if h <= want and h <= native * 1.05), 360)
    if height < want:
        notes.append(f"원본 최대 해상도가 {want}p보다 낮아 확대하지 않고 {height}p로 만듭니다.")
    width = _even(height * 16 / 9)
    if any(min(i.height, i.width * 9 / 16) < height * 0.9 for i in infos):
        notes.append("일부 영상은 출력 해상도보다 작아 확대됩니다 (선명도가 낮아질 수 있음).")
    if any(abs(i.width / max(i.height, 1) - 16 / 9) > 0.05 for i in infos):
        notes.append("16:9가 아닌 영상은 화면을 채우도록 가장자리가 잘립니다.")
    # FPS: 길이 기준 다수결 (최대 60)
    weight: Counter = Counter()
    for i in infos:
        weight[_std_fps(i.fps)] += max(i.duration, 0.1)
    best = max(weight.values())
    fps = min(f for f, w in weight.items() if w == best)
    fps = min(fps, 60.0)
    if len(weight) > 1:
        notes.append(f"FPS가 서로 달라 {fps:g} FPS로 통일합니다.")
    if height >= 2160 and quality == QUALITY_4K and hevc_available:
        return OutputSpec(width, height, fps, "hevc", 22, "fast", f"4K {width}×{height} H.265 고효율", tuple(notes))
    if height >= 2160:
        return OutputSpec(width, height, fps, "h264", 19, "fast", f"4K {width}×{height} H.264", tuple(notes))
    return OutputSpec(width, height, fps, "h264", 18, "fast", f"{width}×{height} H.264 고화질", tuple(notes))


def _h264_level(spec: OutputSpec) -> str:
    big = spec.height > 1080
    hi = spec.fps > 30.5
    if big:
        return "5.2" if hi else "5.1"
    return "4.2" if hi else "4.1"


# ---------------- 계획 ----------------

def _frames(seconds: float, fps: float) -> int:
    return int(math.floor(seconds * fps + 1e-6))


def build_bg_plan(clips: Sequence[Clip], target: float, dissolve: float, spec: OutputSpec) -> BgPlan:
    active = [c for c in clips if c.enabled]
    if not active:
        raise ValueError("사용할 영상이 없습니다. 영상을 추가하거나 '제외'를 해제하세요.")
    if target <= 0:
        raise ValueError("출력 길이를 선택하세요.")
    if dissolve <= 0:
        raise ValueError("디졸브 시간은 0보다 커야 합니다.")
    fps = spec.fps
    d_frames = max(1, int(round(dissolve * fps)))
    d = d_frames / fps
    lengths: list[tuple[Clip, float, float]] = []  # (clip, start, length) — 프레임 단위로 맞춘 값
    for c in active:
        n = _frames(c.used_length, fps)
        if n < 2 * d_frames + int(fps * 0.5):
            need = 2 * d + 0.5
            raise ValueError(f"'{c.path.name}' 사용 구간이 너무 짧습니다 ({c.used_length:.1f}초). "
                             f"디졸브 {d:g}초에는 최소 {need:.1f}초가 필요합니다.")
        lengths.append((c, c.used_start, n / fps))

    pieces: list[Piece] = []
    count = len(lengths)
    for k, (c, s, L) in enumerate(lengths):
        body_len = L - 2 * d
        pieces.append(Piece("body", Segment(c.path, s + d, body_len), None, body_len, f"장면 {k + 1} 본문"))
        nc, ns, _ = lengths[(k + 1) % count]
        a = Segment(c.path, s + L - d, d)
        b = Segment(nc.path, ns, d)
        nxt = (k + 1) % count + 1
        pieces.append(Piece("xfade", a, b, d, f"장면 {k + 1}→{nxt} 디졸브"))
    cycle = sum(p.length for p in pieces)
    repeats = max(1, math.ceil(target / cycle - 1e-9))
    bps = spec.bytes_per_second
    expected_size = int(bps * target)
    work_size = int(bps * cycle * 2)
    warnings: list[str] = list(spec.notes)
    if count == 1:
        warnings.append("영상 1개를 반복합니다. 반복 지점 분석 결과(적합/확인 필요/부적합)를 꼭 확인하세요.")
    if repeats >= 2 and cycle < 180:
        warnings.append(f"전체 장면 순서가 약 {repeats}번 반복됩니다 (한 바퀴 {format_duration(cycle)}). "
                        "영상을 더 추가하면 반복이 덜 눈에 띕니다.")
    if target < cycle:
        warnings.append("선택한 길이가 한 바퀴보다 짧아 뒤쪽 장면 일부가 빠집니다.")
    return BgPlan(tuple(pieces), d, cycle, float(target), repeats, spec, expected_size, work_size, count, tuple(warnings))


def default_bg_output_name(clips: Sequence[Clip], minutes: int) -> str:
    active = [c for c in clips if c.enabled]
    stem = active[0].path.stem if active else "BACKGROUND"
    extra = f"_외{len(active) - 1}" if len(active) > 1 else ""
    return f"{stem}{extra}_BG_{minutes}M.mp4"


# ---------------- FFmpeg 실행 ----------------

def fps_expr(fps: float) -> str:
    """NTSC 계열은 정확한 분수로 (29.97 → 30000/1001)."""
    for std, expr in ((23.976, "24000/1001"), (29.97, "30000/1001"), (59.94, "60000/1001")):
        if abs(fps - std) < 0.01:
            return expr
    return f"{fps:g}"


def _norm_chain(spec: OutputSpec, length: float) -> str:
    w, h, fps = spec.width, spec.height, spec.fps
    # setpts는 frame_rate 정보를 지우므로 fps를 그 뒤에 둔다 (xfade는 CFR 입력이 필요).
    return (f"setpts=PTS-STARTPTS,trim=duration={length + 0.05:.6f},setpts=PTS-STARTPTS,fps={fps_expr(fps)},"
            f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},setsar=1,"
            f"format=yuv420p,settb=AVTB")


def piece_command(ffmpeg: Path, piece: Piece, spec: OutputSpec, out: Path) -> list[str]:
    cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-nostdin"]
    segs = [piece.a] + ([piece.b] if piece.b is not None else [])
    for seg in segs:
        # 입력 앞 -ss + 재인코딩 = 프레임 단위 정확한 탐색. -t는 약간 여유를 두고 trim으로 정확히 자른다.
        cmd += ["-ss", f"{seg.start:.6f}", "-t", f"{seg.length + 0.5:.6f}", "-i", str(seg.path)]
    if piece.kind == "xfade":
        graph = (f"[0:v]{_norm_chain(spec, piece.a.length)}[a];"
                 f"[1:v]{_norm_chain(spec, piece.b.length)}[b];"
                 f"[a][b]xfade=transition=fade:duration={piece.length:.6f}:offset=0,"
                 f"trim=duration={piece.length:.6f},setpts=PTS-STARTPTS[v]")
    else:
        graph = f"[0:v]{_norm_chain(spec, piece.length)}[v]"
    n_frames = max(1, int(round(piece.length * spec.fps)))
    cmd += ["-filter_complex", graph, "-map", "[v]", "-an", "-sn", "-dn", "-map_metadata", "-1",
            "-frames:v", str(n_frames), "-r", fps_expr(spec.fps), "-c:v", spec.encoder,
            "-preset", spec.preset, "-crf", str(spec.crf), "-pix_fmt", "yuv420p",
            "-g", str(max(1, int(round(spec.fps * 2))))]
    if spec.codec == "hevc":
        cmd += ["-tag:v", "hvc1", "-x265-params", "log-level=error"]
    else:
        cmd += ["-profile:v", "high", "-level:v", _h264_level(spec)]
    cmd += ["-progress", "pipe:1", "-nostats", str(out)]
    return cmd


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def run_ffmpeg_progress(cmd: list[str], cancel_event: Event, on_seconds: Callable[[float], None] | None = None) -> None:
    """FFmpeg 1개 실행. 중지 요청 시 즉시 종료한다. 실패하면 마지막 오류 줄과 함께 BuildError."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace", bufsize=1,
                                creationflags=creationflags_no_window())
    except OSError as e:
        raise BuildError(f"FFmpeg를 실행할 수 없습니다: {e}") from e
    recent: list[str] = []
    try:
        assert proc.stdout is not None
        while True:
            if cancel_event.is_set():
                _terminate(proc)
                raise BuildCancelled("사용자가 작업을 중지했습니다.")
            line = proc.stdout.readline()
            if line == "" and proc.poll() is not None:
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("out_time_us="):
                try:
                    if on_seconds:
                        on_seconds(max(0.0, int(line.split("=", 1)[1]) / 1_000_000))
                except ValueError:
                    pass
            elif "=" not in line or line.startswith(("[", "Error", "error")):
                recent.append(line)
                recent = recent[-8:]
        rc = proc.wait()
        if cancel_event.is_set():
            raise BuildCancelled("사용자가 작업을 중지했습니다.")
        if rc != 0:
            text = "\n".join(recent[-6:])
            if "No space left" in text or "There is not enough space" in text:
                raise BuildError("디스크 공간이 부족해 제작을 멈췄습니다. 저장 폴더의 여유 공간을 확보하세요.\n" + text)
            raise BuildError("FFmpeg 편집에 실패했습니다.\n" + text)
    finally:
        _terminate(proc)


def decode_check(ffmpeg: Path, path: Path, cancel_event: Event) -> None:
    """만든 파일을 실제로 끝까지 디코드해 손상 여부 확인 (프레임은 FFmpeg 안에서만 처리)."""
    cmd = [str(ffmpeg), "-hide_banner", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0",
           "-f", "null", "-progress", "pipe:1", "-nostats", "-"]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             text=True, encoding="utf-8", errors="replace", creationflags=creationflags_no_window())
    except OSError as e:
        raise BuildError(f"FFmpeg를 실행할 수 없습니다: {e}") from e
    try:
        while p.poll() is None:
            if cancel_event.is_set():
                raise BuildCancelled("사용자가 작업을 중지했습니다.")
            try:
                out, err = p.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                continue
        else:
            out, err = p.communicate()
        if p.returncode != 0 or (err or "").strip():
            raise BuildError("디졸브 편집본 디코드 검사 실패:\n" + (err or "").strip()[-600:])
    finally:
        _terminate(p)


def _concat_copy_simple(ffmpeg: Path, files: Sequence[Path], out: Path, cancel_event: Event) -> None:
    lst = out.with_suffix(".txt")
    write_concat_file(files, lst)
    cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-nostdin", "-f", "concat", "-safe", "0",
           "-i", str(lst), "-map", "0:v:0", "-c", "copy", "-progress", "pipe:1", "-nostats", str(out)]
    run_ffmpeg_progress(cmd, cancel_event)


def hevc_available(ffmpeg: Path) -> bool:
    try:
        p = subprocess.run([str(ffmpeg), "-hide_banner", "-encoders"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=20, creationflags=creationflags_no_window())
        return "libx265" in p.stdout
    except Exception:
        return False


def run_background_build(
    *,
    ffmpeg: Path,
    ffprobe: Path,
    plan: BgPlan,
    output: Path,
    cancel_event: Event,
    progress_cb: Callable[[float, str], None] | None = None,
    temp_root: Path | None = None,
) -> BgResult:
    """plan대로 디졸브 편집 → 목표 길이 반복. 원본은 읽기만 하고, 임시파일은 출력 폴더(또는 temp_root)에만 만든다."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    sources = {p.a.path.resolve() for p in plan.pieces} | {p.b.path.resolve() for p in plan.pieces if p.b}
    if output.resolve() in sources:
        raise BuildError("원본 영상과 같은 파일명으로 저장할 수 없습니다.")
    if output.exists():
        raise BuildError(f"출력 파일이 이미 존재합니다: {output.name}")
    ok, free, need = enough_disk_space(output.parent, plan.required_bytes)
    if not ok:
        raise BuildError(f"저장 공간 부족: 남음 {free / 1024**3:.1f} GB / 필요 약 {need / 1024**3:.1f} GB")

    started = time.monotonic()
    temp_dir = Path(temp_root or output.parent) / f"{TEMP_PREFIX}{uuid.uuid4().hex[:10]}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    encode_total = sum(p.length for p in plan.pieces)
    encode_weight = 0.8

    def report(frac: float, text: str) -> None:
        if progress_cb:
            progress_cb(min(0.999, max(0.0, frac)), text)

    try:
        files: list[Path] = []
        done = 0.0
        for idx, piece in enumerate(plan.pieces):
            if cancel_event.is_set():
                raise BuildCancelled("사용자가 작업을 중지했습니다.")
            out = temp_dir / f"p{idx:04d}.mp4"
            base = done

            def on_sec(sec: float, base=base, piece=piece, idx=idx) -> None:
                frac = (base + min(sec, piece.length)) / encode_total * encode_weight
                report(frac, f"디졸브 편집 중 · {piece.label} ({idx + 1}/{len(plan.pieces)})")

            on_sec(0.0)
            run_ffmpeg_progress(piece_command(ffmpeg, piece, plan.spec, out), cancel_event, on_sec)
            if not out.exists() or out.stat().st_size == 0:
                raise BuildError(f"편집 조각이 만들어지지 않았습니다: {piece.label}")
            files.append(out)
            done += piece.length

        report(encode_weight, "한 바퀴 영상 합치는 중 (무손실)")
        cycle = temp_dir / "cycle.mp4"
        _concat_copy_simple(ffmpeg, files, cycle, cancel_event)
        for f in files:
            try:
                f.unlink()
            except OSError:
                pass
        report(encode_weight + 0.02, "한 바퀴 영상 검사 중 (디코드 확인)")
        decode_check(ffmpeg, cycle, cancel_event)
        cinfo = probe_video(cycle, ffprobe)
        if (cinfo.width, cinfo.height) != (plan.spec.width, plan.spec.height):
            raise BuildError("편집본 해상도가 예상과 다릅니다.")
        if abs(cinfo.duration - plan.cycle_duration) > max(0.5, 3 / plan.spec.fps * len(plan.pieces)):
            raise BuildError(f"편집본 길이 검증 실패: 예상 {plan.cycle_duration:.2f}초, 실제 {cinfo.duration:.2f}초")

        repeats = max(1, math.ceil(plan.target / cinfo.duration - 1e-9))
        long_plan = BuildPlan(sequence=(cycle,) * repeats, expected_duration=plan.target,
                              expected_size=int(cinfo.size * plan.target / cinfo.duration),
                              cycles=repeats, trim_to=plan.target, mode="time")
        base = encode_weight + 0.04

        def on_long(frac: float, _text: str) -> None:
            report(base + frac * (1 - base), f"목표 길이로 무손실 반복 연결 중 · {repeats}바퀴")

        r = run_concat_copy(ffmpeg=ffmpeg, ffprobe=ffprobe, infos=[cinfo], plan=long_plan, output=output,
                            cancel_event=cancel_event, progress_cb=on_long)
        final = probe_video(r.output, ffprobe)
        if progress_cb:
            progress_cb(1.0, "완료")
        return BgResult(r.output, final.duration, final.size, final.width, final.height, final.fps,
                        cinfo.duration, repeats, time.monotonic() - started)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def cleanup_stale_temp(folder: Path, max_age_hours: float = 12.0) -> int:
    """이전에 비정상 종료로 남은 이 기능의 임시 폴더만 정리한다 (이름 접두사로 구분, 원본 영상은 건드리지 않음)."""
    removed = 0
    try:
        now = time.time()
        for p in Path(folder).glob(f"{TEMP_PREFIX}*"):
            if p.is_dir() and now - p.stat().st_mtime > max_age_hours * 3600:
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
    except OSError:
        pass
    return removed


def clip_from_info(info: VideoInfo) -> Clip:
    return Clip(info=info)


def with_range(clip: Clip, start: float, end: float | None) -> Clip:
    return replace(clip, start=max(0.0, start), end=end)


__all__ = [
    "Clip", "OutputSpec", "Segment", "Piece", "BgPlan", "BgResult", "TARGET_MINUTES", "DISSOLVE_CHOICES",
    "DEFAULT_DISSOLVE", "QUALITY_FHD", "QUALITY_4K", "GUARD_OWNER", "choose_output_spec", "build_bg_plan",
    "default_bg_output_name", "piece_command", "run_background_build", "run_ffmpeg_progress", "hevc_available",
    "cleanup_stale_temp", "decode_check", "probe_video", "fps_expr",
]
