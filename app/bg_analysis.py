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
    stats: list = field(default_factory=list, repr=False)  # 격자별 GridStat (반복 지점 구조 비교용, 없으면 계산)

    def get_stats(self) -> list:
        if len(self.stats) != len(self.grids):
            self.stats = [grid_stats(g) for g in self.grids]
        return self.stats

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
    luma_score: float = 0.0    # 밝기·색 차이 (격자 그대로)
    struct_score: float = 0.0  # 구도 차이 (밝기·명암 폭을 맞춘 뒤)
    motion_end: float = 0.0    # 끝 부분 / 시작 부분 움직임 속도 (0.25초 간격 구조 변화)
    motion_start: float = 0.0
    uncertain: bool = False    # 자동 판정이 애매해 '확인 필요'로 낮춘 경우


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


FLAT_STD = 3.0  # 이보다 명암 차이가 작은 격자는 '구도 없음'(검은 화면 등)


@dataclass(frozen=True)
class GridStat:
    mean: float
    std: float
    z: tuple[float, ...]  # (v - mean) / std, 거의 단색이면 (v - mean) 그대로


def grid_stats(grid: bytes) -> GridStat:
    n = len(grid)
    m = sum(grid) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in grid) / n)
    k = 1 / sd if sd >= FLAT_STD else 1.0
    return GridStat(m, sd, tuple((v - m) * k for v in grid))


def matched_diff(a: bytes, b: bytes, sa: GridStat | None = None, sb: GridStat | None = None) -> float:
    """구조(구도) 차이: 두 격자의 평균 밝기와 명암 폭을 서로 맞춘 뒤 비교한다 (0~255 단위).

    페이드 인/아웃·노출 변화는 밝기와 명암 폭만 바뀌므로 거의 0이 되고, 카메라 이동·장면 변화는 그대로 남는다.
    두 격자의 명암 폭이 같으면 기존 평균 맞춤(_centered) 비교와 같은 값이다.
    """
    sa = sa or grid_stats(a)
    sb = sb or grid_stats(b)
    if sa.std < FLAT_STD or sb.std < FLAT_STD:  # 한쪽이 거의 단색이면 비율을 맞출 수 없음 → 평균만 맞춘다
        return sum(abs((x - sa.mean) - (y - sb.mean)) for x, y in zip(a, b)) / len(a)
    s = (sa.std + sb.std) / 2
    return s * sum(abs(x - y) for x, y in zip(sa.z, sb.z)) / len(sa.z)


def _is_blank(st: GridStat) -> bool:
    return st.mean < 14 and st.std < 6


def _lag_diff(grids: Sequence[bytes], lag: int, stats: Sequence[GridStat] | None = None) -> float:
    """lag 간격 프레임 사이 구조 차이의 중앙값. 검은(빈) 화면 쌍은 제외한다 (페이드 구간이 이동으로 오판되지 않게)."""
    if lag < 1 or len(grids) <= lag:
        return 0.0
    stats = stats or [grid_stats(g) for g in grids]
    step = max(1, (len(grids) - lag) // 120)
    ds = [matched_diff(grids[i], grids[i + lag], stats[i], stats[i + lag])
          for i in range(0, len(grids) - lag, step) if not (_is_blank(stats[i]) or _is_blank(stats[i + lag]))]
    return statistics.median(ds) if ds else 0.0


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
    stats = [grid_stats(gr) for gr in grids]
    motion = _lag_diff(grids, max(1, round(0.5 * sf)), stats)
    drift = _lag_diff(grids, max(1, round(4 * sf)), stats) if duration >= 6 else motion
    head = max(1, round(sf))
    an = ClipAnalysis(
        path=path, duration=duration, sample_fps=sf, grids=grids, dhash=hashes, mean_rgb=mean_rgb,  # type: ignore[arg-type]
        brightness=sum(means) / n, contrast=sum(stds) / n, motion=motion, drift=drift,
        camera=classify_camera(motion, drift),
        black_seconds=sum(1 for m, s in zip(means, stds) if m < 14 and s < 6) / sf,
        head_luma=sum(means[:head]) / head, tail_luma=sum(means[-head:]) / head, stats=stats,
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
    score = _scorer(an, w)

    def penalized(sc: float, si: int, ei: int) -> float:
        return sc + 3.0 * (1 - (ei - si) / n)  # 원본을 많이 자를수록 약간 감점 (비슷하면 전체 사용)

    full = score(0, n)[0] if n >= 2 * w else 99.0
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
            # 점수 = max(밝기, 구도) ≥ 밝기 → 밝기만으로 이미 못 이기면 구도 계산을 건너뛴다 (화면 갱신 속도)
            sc = score(si, ei, bound=best_pen - 0.5 - (penalized(0.0, si, ei)))[0]
            pen = penalized(sc, si, ei)
            if pen < best_pen - 0.5:
                best, best_pen = (sc, si, ei), pen
    _, si, ei = best
    return _advice(an, si, ei, w, full, score, recommended=True)


def evaluate_range(an: ClipAnalysis, start: float, end: float | None, dissolve: float = 1.0) -> LoopAdvice:
    """사용자가 정한 구간(start~end)을 그대로 반복할 때의 평가 (추천 탐색 없이)."""
    sf = an.sample_fps
    n = len(an.grids)
    w = max(1, round(dissolve * sf))
    si = max(0, min(n - 1, int(round(start * sf))))
    ei = n if end is None or end >= an.duration - 0.01 else max(si + 1, min(n, int(round(end * sf))))
    score = _scorer(an, w)
    full = score(0, n)[0] if n >= 2 * w else 99.0
    return _advice(an, si, ei, w, full, score, recommended=False)


def _scorer(an: ClipAnalysis, w: int):
    """(si, ei) → (경계 점수, 밝기 차이, 구도 차이). 끝 구간 [ei-w, ei) 와 시작 구간 [si, si+w)를 겹쳐 비교.

    경계 점수 = max(밝기 차이, 구도 차이): 밝기만 같고 구도가 다르거나(다른 장면), 구도는 같고 밝기가 다른(노출 변화)
    경우 모두 디졸브에서 눈에 띄므로 둘 중 큰 쪽을 쓴다.
    """
    g = an.grids
    st = an.get_stats()
    n = len(g)

    def score(si: int, ei: int, bound: float | None = None) -> tuple[float, float, float]:
        if ei - w < 0 or si + w > n or w <= 0:
            return 99.0, 99.0, 99.0
        lum = sum(grid_diff(g[ei - w + j], g[si + j]) for j in range(w)) / w
        if bound is not None and lum >= bound:
            return lum, lum, lum  # 어차피 채택되지 않음
        struct = sum(matched_diff(g[ei - w + j], g[si + j], st[ei - w + j], st[si + j]) for j in range(w)) / w
        return max(lum, struct), lum, struct
    return score


def _motion(an: ClipAnalysis, i0: int, i1: int) -> float:
    """[i0, i1) 구간에서 이웃한 샘플 사이 구조 변화의 평균 (움직임 속도)."""
    g, st = an.grids, an.get_stats()
    i0, i1 = max(0, i0), min(len(g), i1)
    ds = [matched_diff(g[k], g[k + 1], st[k], st[k + 1]) for k in range(i0, i1 - 1)]
    return sum(ds) / len(ds) if ds else 0.0


def _advice(an: ClipAnalysis, si: int, ei: int, w: int, full: float, score, *, recommended: bool) -> LoopAdvice:
    sf, dur, n = an.sample_fps, an.duration, len(an.grids)
    sc, lum, struct = score(si, ei) if ei - si >= 2 * w else (99.0, 99.0, 99.0)
    start = round(si / sf, 2)
    end = dur if ei >= n else round(ei / sf, 2)
    span = w + max(2, round(sf))  # 경계 앞뒤 (디졸브 + 약 1초)
    m_end, m_start = _motion(an, ei - span, ei), _motion(an, si, si + span)
    reasons: list[str] = []
    moving = an.camera == CAMERA_MOVING
    uncertain = False
    if moving:
        reasons.append("카메라가 이동하는 영상이라 반복 경계에서 장면이 겹쳐 보일 수 있습니다.")
    if sc <= GOOD_SCORE and not moving:
        verdict = VERDICT_GOOD
        reasons.append("시작과 끝의 밝기·구도가 비슷해 디졸브 반복이 자연스럽습니다.")
    elif sc <= CHECK_SCORE and not (moving and sc > 9):
        verdict = VERDICT_CHECK
        reasons.append("시작과 끝이 조금 다릅니다. [반복 경계 미리보기]로 이음새를 확인하세요.")
    else:
        verdict = VERDICT_BAD
        reasons.append("시작과 끝 화면 차이가 큽니다. 다른 영상을 섞어 장면 전환으로 반복하는 것을 권장합니다.")
    if verdict != VERDICT_GOOD and struct <= GOOD_SCORE < lum:
        reasons.append("구도는 비슷하지만 밝기·색이 다릅니다 (노출 변화·해 질 녘 등).")
    elif verdict != VERDICT_GOOD and lum <= GOOD_SCORE < struct:
        reasons.append("밝기는 비슷하지만 구도가 다릅니다 (다른 장면·카메라 위치 변화).")
    # 움직임 속도가 끝과 시작에서 크게 다르면 (예: 잔잔한 물결 → 거센 파도) 디졸브가 어색할 수 있다
    gap = abs(m_end - m_start)
    if gap > max(2.5, 0.5 * max(m_end, m_start)):
        reasons.append(f"끝과 시작의 움직임 속도가 다릅니다 (끝 {m_end:.1f} / 시작 {m_start:.1f}).")
        if verdict == VERDICT_GOOD:
            verdict, uncertain = VERDICT_CHECK, True
    if n < 4 * w + 4 and verdict == VERDICT_GOOD:  # 영상이 짧아 비교할 샘플이 적음
        verdict, uncertain = VERDICT_CHECK, True
        reasons.append("영상이 짧아 자동 판정의 근거가 적습니다.")
    if uncertain:
        reasons.append("자동 판정이 확실하지 않아 '확인 필요'로 표시합니다. 미리보기 후 사용 구간을 조정하세요.")
    if recommended and (start > 0 or end < dur):
        reasons.append(f"추천 구간 {start:.1f}초 ~ {end:.1f}초 (전체 그대로: 차이 {full:.1f} → 추천: {sc:.1f})")
    return LoopAdvice(start, end, round(sc, 2), round(full, 2), verdict, tuple(reasons), dur,
                      luma_score=round(lum, 2), struct_score=round(struct, 2),
                      motion_end=round(m_end, 2), motion_start=round(m_start, 2), uncertain=uncertain)


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
