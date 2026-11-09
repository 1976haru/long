"""④ 배경 만들기 — 반복 지점 / 중복 / 품질 자동 분석 (휴리스틱 참고 지표).

메모리 규칙: Python은 영상 프레임을 디코딩하지 않는다. FFmpeg가 디코딩해 아주 작은 저해상도
(48×27 RGB, 초당 최대 4장) 원시 바이트만 파이프로 넘기고, Python은 그 작은 숫자 배열만 계산한다.

무엇을 하지 않는가: AI 인물/로고/장면 인식은 없다. 결과는 참고용이며 최종 판단은 사용자가 한다.
원본 파일은 읽기만 하며 중복/품질 경고가 있어도 삭제하지 않는다.
"""
from __future__ import annotations

import base64
import hashlib
import math
import statistics
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Sequence

from .core import creationflags_no_window

SW, SH = 48, 27        # 샘플 프레임 크기 (RGB)
GW, GH = 16, 9         # 반복 지점 비교용 격자 (3×3 평균)
MAX_SAMPLES = 480
SHARP_W, SHARP_H = 256, 144

GOOD_SCORE = 6.0       # 반복 경계 차이(0~255) 이하면 '반복 적합'
CHECK_SCORE = 14.0     # 이하면 '확인 필요', 초과면 '반복 부적합'

VERDICT_GOOD = "반복 적합"
VERDICT_CHECK = "확인 필요"
VERDICT_BAD = "반복 부적합"

CAMERA_STATIC = "고정 촬영"
CAMERA_GENTLE = "고정 촬영 · 움직임 있음"
CAMERA_MOVING = "이동 촬영 (산책·드론 등)"


class AnalysisError(RuntimeError):
    pass


@dataclass
class ClipAnalysis:
    path: Path
    duration: float
    sample_fps: float
    grids: list[bytes]                     # 프레임별 16×9 회색 격자
    sha256: str = ""
    dhash: tuple[int, ...] = ()            # 10/30/50/70/90% 지점 64bit 해시
    mean_rgb: tuple[float, float, float] = (0.0, 0.0, 0.0)
    brightness: float = 0.0
    contrast: float = 0.0
    motion: float = 0.0                    # 0.5초 간격 평균 변화량 (0~255)
    drift: float = 0.0                     # 4초 간격 평균 변화량 (카메라 이동이면 커짐)
    camera: str = CAMERA_STATIC
    sharpness: float | None = None
    black_seconds: float = 0.0
    head_luma: float = 0.0                 # 처음 1초 / 마지막 1초 평균 밝기 (전환 경고용)
    tail_luma: float = 0.0
    quality_warnings: list[str] = field(default_factory=list)

    @property
    def quality_label(self) -> str:
        return "양호" if not self.quality_warnings else f"주의 {len(self.quality_warnings)}"


@dataclass(frozen=True)
class LoopAdvice:
    start: float
    end: float
    score: float          # 반복 경계 두 구간의 평균 차이 (낮을수록 자연스러움)
    full_score: float     # 전체 구간 그대로 반복할 때
    verdict: str
    reasons: tuple[str, ...]

    @property
    def trimmed(self) -> bool:
        return self.start > 0.05 or self.end < self._dur - 0.05

    _dur: float = 0.0


# ---------------- FFmpeg 호출 ----------------

def _run(cmd: list[str], timeout: float) -> bytes:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           creationflags=creationflags_no_window())
    except subprocess.TimeoutExpired as e:
        raise AnalysisError("영상 분석 시간이 초과되었습니다.") from e
    except OSError as e:
        raise AnalysisError(f"FFmpeg를 실행할 수 없습니다: {e}") from e
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise AnalysisError("영상을 분석할 수 없습니다 (손상되었거나 지원하지 않는 형식).\n" + "\n".join(err))
    return p.stdout


def thumbnail_png(ffmpeg: Path, path: Path, duration: float, w: int = 112, h: int = 63) -> bytes:
    t = max(0.0, duration * 0.4)
    return _run([str(ffmpeg), "-hide_banner", "-v", "error", "-nostdin", "-ss", f"{t:.3f}", "-i", str(path),
                 "-frames:v", "1", "-an",
                 "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
                 "-f", "image2pipe", "-c:v", "png", "pipe:1"], timeout=60)


def thumbnail_b64(ffmpeg: Path, path: Path, duration: float) -> str:
    return base64.b64encode(thumbnail_png(ffmpeg, path, duration)).decode("ascii")


def sample_frames(ffmpeg: Path, path: Path, duration: float) -> tuple[float, list[bytes], bool]:
    """(샘플 FPS, 48×27 RGB 프레임들, 디코딩 오류 여부)."""
    sf = 4.0 if duration <= MAX_SAMPLES / 4 else MAX_SAMPLES / duration
    cmd = [str(ffmpeg), "-hide_banner", "-v", "error", "-nostdin", "-i", str(path), "-an", "-sn",
           "-vf", f"fps={sf:.5f},scale={SW}:{SH}:flags=area,format=rgb24", "-f", "rawvideo", "pipe:1"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=max(120.0, duration * 4), stdin=subprocess.DEVNULL,
                           creationflags=creationflags_no_window())
    except subprocess.TimeoutExpired as e:
        raise AnalysisError("영상 분석 시간이 초과되었습니다.") from e
    except OSError as e:
        raise AnalysisError(f"FFmpeg를 실행할 수 없습니다: {e}") from e
    raw = p.stdout
    size = SW * SH * 3
    frames = [raw[i:i + size] for i in range(0, len(raw) - size + 1, size)]
    if p.returncode != 0 and len(frames) < 2:
        err = p.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise AnalysisError("영상을 분석할 수 없습니다 (손상되었거나 지원하지 않는 형식).\n" + "\n".join(err))
    if len(frames) < 2:
        raise AnalysisError("영상에서 분석할 프레임을 읽지 못했습니다.")
    return sf, frames, bool(p.stderr.strip()) or p.returncode != 0


def sharp_frames(ffmpeg: Path, path: Path, duration: float, count: int = 4) -> list[bytes]:
    """여러 지점의 회색 프레임(256×144)을 한 번의 FFmpeg 호출로 (입력 앞 -ss 탐색이라 빠름)."""
    times = [duration * (i + 1) / (count + 1) for i in range(count)]
    cmd = [str(ffmpeg), "-hide_banner", "-v", "error", "-nostdin"]
    for t in times:
        cmd += ["-ss", f"{t:.3f}", "-i", str(path)]
    parts = [f"[{i}:v]trim=end_frame=1,scale={SHARP_W}:{SHARP_H}:flags=area,format=gray,setpts=PTS-STARTPTS[s{i}]"
             for i in range(count)]
    graph = ";".join(parts) + ";" + "".join(f"[s{i}]" for i in range(count)) + f"vstack=inputs={count}[v]"
    raw = _run(cmd + ["-filter_complex", graph, "-map", "[v]", "-frames:v", "1", "-f", "rawvideo", "pipe:1"],
               timeout=120)
    size = SHARP_W * SHARP_H
    return [raw[i * size:(i + 1) * size] for i in range(count) if len(raw) >= (i + 1) * size]


def file_sha256(path: Path, cancel: Event | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel.is_set():
                raise AnalysisError("분석이 중지되었습니다.")
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------- 계산 (작은 바이트 배열만) ----------------

def _luma(frame: bytes) -> list[int]:
    return [(77 * frame[i] + 150 * frame[i + 1] + 29 * frame[i + 2]) >> 8 for i in range(0, len(frame), 3)]


def _grid(luma: Sequence[int]) -> bytes:
    sx, sy = SW // GW, SH // GH
    out = bytearray()
    for gy in range(GH):
        for gx in range(GW):
            s = 0
            for y in range(gy * sy, gy * sy + sy):
                row = y * SW
                for x in range(gx * sx, gx * sx + sx):
                    s += luma[row + x]
            out.append(s // (sx * sy))
    return bytes(out)


def _resize(luma: Sequence[int], w: int, h: int, ow: int, oh: int) -> list[float]:
    out = []
    for oy in range(oh):
        y0, y1 = oy * h // oh, max(oy * h // oh + 1, (oy + 1) * h // oh)
        for ox in range(ow):
            x0, x1 = ox * w // ow, max(ox * w // ow + 1, (ox + 1) * w // ow)
            vals = [luma[y * w + x] for y in range(y0, y1) for x in range(x0, x1)]
            out.append(sum(vals) / len(vals))
    return out


def dhash(luma: Sequence[int]) -> int:
    small = _resize(luma, SW, SH, 9, 8)
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (1 if small[y * 9 + x] < small[y * 9 + x + 1] else 0)
    return bits


def grid_diff(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def laplacian_var(gray: bytes, w: int = SHARP_W, h: int = SHARP_H) -> float:
    vals = []
    for y in range(1, h - 1):
        r = y * w
        for x in range(1, w - 1):
            i = r + x
            vals.append(gray[i - 1] + gray[i + 1] + gray[i - w] + gray[i + w] - 4 * gray[i])
    if not vals:
        return 0.0
    m = sum(vals) / len(vals)
    return sum((v - m) ** 2 for v in vals) / len(vals)


def _centered(grid: bytes) -> bytes:
    """격자 평균 밝기를 128로 맞춘다 → 전체 밝기 변화(노출·해 질 녘)는 '움직임'으로 세지 않는다."""
    shift = 128 - sum(grid) // len(grid)
    return bytes(min(255, max(0, v + shift)) for v in grid)


def _lag_diff(grids: Sequence[bytes], lag: int) -> float:
    if lag < 1 or len(grids) <= lag:
        return 0.0
    step = max(1, (len(grids) - lag) // 120)
    ds = [grid_diff(grids[i], grids[i + lag]) for i in range(0, len(grids) - lag, step)]
    return statistics.median(ds)


def classify_camera(motion: float, drift: float) -> str:
    if drift < 6:
        return CAMERA_STATIC
    if drift < 16 and drift < motion * 2.2 + 4:
        return CAMERA_GENTLE
    return CAMERA_MOVING


def analyze_clip(ffmpeg: Path, path: Path, duration: float, cancel: Event | None = None,
                 *, with_sha: bool = True) -> ClipAnalysis:
    path = Path(path)
    sf, frames, decode_errors = sample_frames(ffmpeg, path, duration)
    if cancel is not None and cancel.is_set():
        raise AnalysisError("분석이 중지되었습니다.")
    lumas = [_luma(f) for f in frames]
    grids = [_grid(l) for l in lumas]
    n = len(frames)
    px = SW * SH
    means = [sum(l) / px for l in lumas]
    stds = [math.sqrt(sum((v - m) ** 2 for v in l) / px) for l, m in zip(lumas, means)]
    dark = sum(sum(1 for v in l if v < 18) / px for l in lumas) / n
    bright = sum(sum(1 for v in l if v > 240) / px for l in lumas) / n
    rgb = [0.0, 0.0, 0.0]
    for f in frames:
        for c in range(3):
            rgb[c] += sum(f[c::3]) / px
    mean_rgb = tuple(v / n for v in rgb)
    marks = [min(n - 1, int(n * p)) for p in (0.1, 0.3, 0.5, 0.7, 0.9)]
    hashes = tuple(dhash(lumas[i]) for i in marks)
    shapes = [_centered(gr) for gr in grids]
    motion = _lag_diff(shapes, max(1, round(0.5 * sf)))
    drift = _lag_diff(shapes, max(1, round(4 * sf))) if duration >= 6 else motion
    head = max(1, round(sf))
    an = ClipAnalysis(
        path=path, duration=duration, sample_fps=sf, grids=grids, dhash=hashes, mean_rgb=mean_rgb,  # type: ignore[arg-type]
        brightness=sum(means) / n, contrast=sum(stds) / n, motion=motion, drift=drift,
        camera=classify_camera(motion, drift),
        black_seconds=sum(1 for m, s in zip(means, stds) if m < 14 and s < 6) / sf,
        head_luma=sum(means[:head]) / head, tail_luma=sum(means[-head:]) / head,
    )
    try:
        sharp = [laplacian_var(g) for g in sharp_frames(ffmpeg, path, duration)]
        an.sharpness = statistics.median(sharp) if sharp else None
    except AnalysisError:
        an.sharpness = None
    if with_sha:
        an.sha256 = file_sha256(path, cancel)
    an.quality_warnings = quality_warnings(an, dark, bright)
    if decode_errors:
        an.quality_warnings.insert(0, "디코딩 오류 발견 — 파일 일부가 손상되었을 수 있습니다")
    return an


def quality_warnings(an: ClipAnalysis, dark_ratio: float = 0.0, bright_ratio: float = 0.0) -> list[str]:
    w: list[str] = []
    if an.brightness < 45:
        w.append("화면이 많이 어둡습니다")
    elif an.brightness > 205:
        w.append("화면이 너무 밝습니다 (노출 과다)")
    if dark_ratio > 0.35 and an.brightness >= 45:
        w.append("검게 뭉친 영역이 많습니다")
    if bright_ratio > 0.25 and an.brightness <= 205:
        w.append("하얗게 날아간 영역이 많습니다")
    if an.black_seconds >= 0.5:
        w.append(f"검은 화면 약 {an.black_seconds:.1f}초")
    if an.sharpness is not None and an.sharpness < 25 and an.contrast > 8:
        w.append("흐릿해 보입니다 (초점/화질 확인)")
    if an.motion > 28:
        w.append("움직임이 매우 큽니다 (배경으로 어지러울 수 있음)")
    if an.contrast < 6 and an.black_seconds < an.duration * 0.5:
        w.append("화면 변화/명암이 거의 없습니다")
    return w


# ---------------- 반복 지점 ----------------

def recommend_loop(an: ClipAnalysis, dissolve: float = 1.0) -> LoopAdvice:
    """끝 구간 → 시작 구간 디졸브가 가장 자연스러운 (시작, 끝) 지점을 찾는다.

    점수 = 디졸브로 겹쳐지는 두 구간(끝 D초, 시작 D초)의 프레임 평균 차이(0~255, 16×9 격자).
    완벽한 무한 루프는 보장하지 않으며, 애매하면 '확인 필요' 또는 '반복 부적합'으로 표시한다.
    """
    g = an.grids
    n = len(g)
    sf = an.sample_fps
    dur = an.duration
    w = max(1, round(dissolve * sf))
    min_keep = max(2 * dissolve + 1.0, dur * 0.55)

    def score(si: int, ei: int) -> float:
        # 끝 구간 [ei-w, ei) 와 시작 구간 [si, si+w)
        return sum(grid_diff(g[ei - w + j], g[si + j]) for j in range(w)) / w

    def penalized(sc: float, si: int, ei: int) -> float:
        return sc + 3.0 * (1 - (ei - si) / n)  # 원본을 많이 자를수록 약간 감점 (비슷하면 전체 사용)

    full = score(0, n) if n >= 2 * w else 99.0
    best = (full, 0, n)
    best_pen = penalized(*best)
    s_max = max(0, int(n * 0.3))
    e_min = min(n, max(2 * w, int(n * 0.7)))
    step = max(1, (s_max + (n - e_min)) // 80)
    searching = full > GOOD_SCORE  # 전체 구간이 이미 '반복 적합'이면 원본을 자르지 않는다
    for si in range(0, s_max + 1, step) if searching else ():
        for ei in range(n, e_min - 1, -step):
            if ei - w < si + w or (ei - si) / sf < min_keep:
                continue
            sc = score(si, ei)
            pen = penalized(sc, si, ei)
            if pen < best_pen - 0.5:
                best, best_pen = (sc, si, ei), pen
    sc, si, ei = best
    start = round(si / sf, 2)
    end = dur if ei >= n else round(ei / sf, 2)
    reasons: list[str] = []
    moving = an.camera == CAMERA_MOVING
    if moving:
        reasons.append("카메라가 이동하는 영상이라 반복 경계에서 장면이 겹쳐 보일 수 있습니다.")
    if sc <= GOOD_SCORE and not moving:
        verdict = VERDICT_GOOD
        reasons.append("시작과 끝 화면이 비슷해 디졸브 반복이 자연스럽습니다.")
    elif sc <= CHECK_SCORE and not (moving and sc > 9):
        verdict = VERDICT_CHECK
        reasons.append("시작과 끝이 조금 다릅니다. 미리보기로 이음새를 확인하세요.")
    else:
        verdict = VERDICT_BAD
        reasons.append("시작과 끝 화면 차이가 큽니다. 다른 영상을 섞어 장면 전환으로 반복하는 것을 권장합니다.")
    if start > 0 or end < dur:
        reasons.append(f"추천 구간 {start:.1f}초 ~ {end:.1f}초 (전체 그대로: 차이 {full:.1f} → 추천: {sc:.1f})")
    return LoopAdvice(start, end, round(sc, 2), round(full, 2), verdict, tuple(reasons), dur)


# ---------------- 중복 / 연결 ----------------

def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


@dataclass(frozen=True)
class DuplicateFinding:
    a: Path
    b: Path
    kind: str      # "동일 파일" | "매우 유사" | "유사 장면"
    distance: float


def find_duplicates(analyses: Sequence[ClipAnalysis]) -> list[DuplicateFinding]:
    found: list[DuplicateFinding] = []
    for i in range(len(analyses)):
        for j in range(i + 1, len(analyses)):
            a, b = analyses[i], analyses[j]
            if a.sha256 and a.sha256 == b.sha256:
                found.append(DuplicateFinding(a.path, b.path, "동일 파일", 0.0))
                continue
            if not a.dhash or not b.dhash:
                continue
            dist = sum(_hamming(x, y) for x, y in zip(a.dhash, b.dhash)) / len(a.dhash)
            color = sum(abs(x - y) for x, y in zip(a.mean_rgb, b.mean_rgb)) / 3
            ratio = min(a.duration, b.duration) / max(a.duration, b.duration, 0.001)
            if dist <= 6 and color < 12 and ratio > 0.7:
                found.append(DuplicateFinding(a.path, b.path, "매우 유사", round(dist, 1)))
            elif dist <= 12 and color < 20:
                found.append(DuplicateFinding(a.path, b.path, "유사 장면", round(dist, 1)))
    return found


def transition_warnings(ordered: Sequence[ClipAnalysis]) -> list[str]:
    """이어지는 장면 사이 밝기 차이가 크거나, 비슷한 장면이 연달아 오면 알려준다 (마지막→처음 포함)."""
    out: list[str] = []
    n = len(ordered)
    if n < 2:
        return out
    for k in range(n):
        a, b = ordered[k], ordered[(k + 1) % n]
        if abs(a.tail_luma - b.head_luma) > 70:
            out.append(f"{a.path.name} → {b.path.name}: 밝기 차이가 커서 전환이 눈에 띌 수 있습니다.")
        if a.dhash and b.dhash and a.path != b.path:
            dist = sum(_hamming(x, y) for x, y in zip(a.dhash, b.dhash)) / len(a.dhash)
            if dist <= 8:
                out.append(f"{a.path.name} → {b.path.name}: 비슷한 장면이 연달아 나옵니다. 순서를 바꿔 보세요.")
    return out
