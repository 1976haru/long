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
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event
from typing import Callable, Sequence

from .core import (
    BuildCancelled, BuildError, BuildPlan, VideoInfo, creationflags_no_window, enough_disk_space, escape_concat_path,
    format_duration, free_bytes, probe_video, run_concat_copy, write_concat_file,
)

TARGET_MINUTES = (3, 10, 20, 30, 60)
DISSOLVE_CHOICES = (0.5, 1.0, 1.5, 2.0)
DEFAULT_DISSOLVE = 1.0
DISK_FLOOR = 512 * 1024**2      # 작업 중 이 이하로 남으면(또는 남은 작업량+이 값보다 적으면) 안전하게 중단
DISK_POLL_SECONDS = 2.0
PROJECTION_HEADROOM = 1.15      # 실측 비트레이트로 다시 계산할 때 15% 여유
MEASURE_HEADROOM = 1.40         # 공간 검사용: 실측(장면 본문 가운데 2초씩)에 두는 여유 — 실환경 최대 1.17배보다 넉넉히
MEASURE_SECONDS = 2.0           # 시험 인코딩 구간 길이
MEASURE_SAMPLES = 12            # 장면마다 1곳 (장면이 더 많으면 고르게 12곳), 장면 길이로 가중 평균
# 화면 표시용 예상 범위 (실측 × 비율). 실환경 검증 (서로 다른 FHD 6개 영상, 3·10분, 고화질·균형·저용량·H.265,
# 순서 2가지): 실제 최종 크기 = 실측 × 1.05~1.17 (디졸브 구간이 본문보다 조금 무거움, 순서와 무관하게 일정).
MEASURE_SHOW_LOW = 0.95
MEASURE_SHOW_HIGH = 1.30
OWNER_FILE = "owner.txt"        # 임시 폴더 안: 이 작업이 만드는 .part.mp4 경로 (강제 종료 후 정리용)


CODEC_H264 = "h264"
CODEC_HEVC = "hevc"
HEVC_BASE_CRF = 20          # FHD/4K H.265 기본 CRF. 실측(눈 내리는 숲): x265 CRF20 medium 676 MB·VMAF 92.4 (10분)
HEVC_SIZE_RATIO = 0.75      # 예상 용량용 H.265 / H.264(CRF18) 크기 비율 — 실측 0.72 (세밀한 결이 많으면 이득이 작음)
HEVC_CAP_RATIO = 0.8        # 프로필 비트레이트 상한을 H.265에 적용할 때 비율 (같은 화질에서 이득이 작아 과하게 줄이지 않음)


@dataclass(frozen=True)
class EncodeProfile:
    """화질·용량 선택지. crf_offset은 해상도·코덱별 기본 CRF(FHD H.264 18 / 4K H.264 19 / H.265 22)에 더한다."""
    key: str
    label: str
    crf_offset: int
    preset: str
    size_factor: float  # 기본(고화질) 대비 예상 용량 비율 (CRF는 내용에 따라 달라지므로 보수적인 값)
    note: str
    maxrate_kbps: int = 0       # FHD 30fps H.264 기준 VBV 상한 (0 = 상한 없음). 해상도·FPS·코덱에 맞게 비례 조정
    hevc_preset: str = "medium"
    short: str = ""             # 화면의 간단 선택 버튼 이름


PROFILE_BEST = "best"
# 실측 근거 (C07, 실제 눈 내리는 가로수길 FHD 29.97fps 60초 구간, 같은 구간·같은 제작 명령, 10분 환산):
#   고화질 CRF18 fast 901 MB · VMAF 93.4 / 균형 CRF19 slow + 상한 8M 551 MB · VMAF 91.5 /
#   저용량 CRF24 medium 424 MB · VMAF 90.7. 세 경우 모두 CAMBI(그라데이션 띠) 0.
#   (내용에 따라 달라짐: 물결·나뭇잎·눈송이처럼 세밀하게 움직이면 크고, 잔잔한 화면은 훨씬 작다)
ENCODE_PROFILES: tuple[EncodeProfile, ...] = (
    EncodeProfile(PROFILE_BEST, "고화질 (기본)", 0, "fast", 1.0,
                  "원본에 가장 가까운 화질 (H.264 CRF 18). 용량이 가장 큽니다 — 실측 FHD 10분 약 0.9 GB "
                  "(눈 내리는 숲 기준, 물결·나뭇잎이 많으면 더 큼).",
                  short="고화질 (기본)"),
    EncodeProfile("balanced", "균형 (권장 · 용량 약 40% 절약)", 1, "slow", 0.7,
                  "CRF 19 + 비트레이트 상한 8 Mbps(FHD). 실측: 고화질 대비 용량 약 61% (866 MB급 → 약 550 MB), "
                  "보통 화면에서는 차이가 거의 없고 크게 확대하면 나뭇가지·눈송이 같은 결이 조금 부드러워집니다. "
                  "상한은 평균 크기를 제한할 뿐 정확한 최종 크기를 보장하지 않습니다. 제작 시간은 약 2~3배.",
                  maxrate_kbps=8000, short="균형 (권장)"),
    EncodeProfile("small", "저용량 (용량 약 50% 절약)", 6, "medium", 0.55,
                  "CRF 24 + 상한 8 Mbps(FHD, 균형과 같은 상한이라 균형보다 커지지 않음). 실측: 고화질 대비 용량 약 44% "
                  "(866 MB급 → 약 390 MB). "
                  "화질 저하가 보일 수 있습니다: 물결·나뭇잎·잔디·나무껍질 결이 뭉개지고, 어두운 야경·하늘 그라데이션에 "
                  "계단(띠)이 생길 수 있습니다. CRF·상한 모두 정확한 크기를 보장하지 않으니 [⑥ 실제 용량 측정]으로 "
                  "확인하세요. 업로드용 최종본에는 권장하지 않습니다.",
                  maxrate_kbps=8000, short="저용량"),
)
# 참고 (C07 실측): 저용량 CRF24 상한 없음 424 MB·VMAF 90.7 / 상한 8M 392 MB·VMAF 90.2 / 상한 6M 329 MB·VMAF 89.3.
# 상한 없이 두면 세밀한 화면(실환경 검증의 합성 패턴 10분)에서 저용량 499 MB > 균형 435 MB로 역전되어, 균형과 같은
# 8M 상한을 둔다 (6M은 화질 손상이 커서 쓰지 않음).


def get_profile(key: str | None) -> EncodeProfile:
    return next((p for p in ENCODE_PROFILES if p.key == key), ENCODE_PROFILES[0])


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
    size_factor: float = 1.0
    profile: str = PROFILE_BEST
    maxrate_kbps: int = 0   # >0 이면 CRF + VBV 상한 (-maxrate/-bufsize). 평균 크기를 '제한'할 뿐 정확한 크기를 보장하지 않는다
    bufsize_kbps: int = 0

    @property
    def encoder(self) -> str:
        return "libx265" if self.codec == "hevc" else "libx264"

    @property
    def live_compatible(self) -> bool:
        """② LIVE DIRECT COPY 조건(H.264 + yuv420p)을 만족하는 출력인가. H.265는 저장·업로드용."""
        return self.codec == "h264"

    @property
    def bytes_per_second(self) -> float:
        """제작 전 예상 용량용 평균 비트레이트 — CRF는 크기가 고정되지 않으므로 '넉넉한' 값이다.

        기준(기본 CRF, H.264): 실측에서 FHD 세밀한 화면이 약 21~23 Mbps까지 나와 FHD 24 Mbps로 잡는다.
        제작 중에는 실제로 인코딩된 조각의 크기로 다시 계산하고, 한 바퀴 완성 후에는 실제 파일 크기로 재검사한다.
        """
        mbps_h264 = {2160: 70.0, 1440: 36.0, 1080: 24.0, 720: 12.0, 540: 8.0}
        base = next((v for h, v in sorted(mbps_h264.items()) if self.height <= h), 70.0)
        base *= max(1.0, self.fps / 30.0) ** 0.6
        if self.codec == "hevc":
            base *= HEVC_SIZE_RATIO
        bps = base * self.size_factor * 1_000_000 / 8
        if self.maxrate_kbps > 0:  # VBV 상한: 평균이 상한을 크게 넘지 않으므로 상한 +8%(컨테이너·버퍼 여유)를 넘게 잡지 않는다
            bps = min(bps, self.maxrate_kbps * 1000 * 1.08 / 8)
        return bps


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
    work_size: int  # 한 바퀴(cycle) 분량 — 임시 조각 합계 ≈ cycle.mp4 ≈ 이 값
    clip_count: int
    warnings: tuple[str, ...] = field(default_factory=tuple)
    measured_bps: float = 0.0  # >0: 실제 시험 인코딩(measure_bitrate)으로 잰 평균 바이트/초

    @property
    def required_bytes(self) -> int:
        if self.measured_bps > 0:  # 실측값 + 25% 여유 (짧은 시험 구간이 전체를 대표하지 못할 수 있음)
            rate = self.measured_bps * MEASURE_HEADROOM
            return peak_bytes(rate * self.cycle_duration, rate * self.target)
        return peak_bytes(self.work_size, self.expected_size)

    @property
    def size_range(self) -> tuple[int, int]:
        """화면 표시용 예상 최종 크기 범위 (바이트). 실측 전에는 보수적인 상한만 의미가 있다."""
        if self.measured_bps > 0:
            mid = self.measured_bps * self.target
            return int(mid * MEASURE_SHOW_LOW), int(mid * MEASURE_SHOW_HIGH)
        return 0, self.expected_size


def peak_bytes(cycle: int | float, final: int | float) -> int:
    """작업 중 동시에 차지하는 최대 공간.

    1) 조각 인코딩: 조각 합계(≈cycle)  2) 조각 → cycle.mp4 연결: 조각 + cycle = 2×cycle
    3) 조각 삭제 후 cycle.mp4 → 최종 .part.mp4 반복 연결: cycle + final  (교체는 같은 폴더 이름 변경이라 추가 공간 없음)
    """
    return int(cycle + max(cycle, final))


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


def choose_output_spec(infos: Sequence[VideoInfo], quality: str = QUALITY_FHD, *, hevc_available: bool = True,
                       profile: str = PROFILE_BEST, codec: str = CODEC_H264) -> OutputSpec:
    """FHD H.264 고화질이 기본 (② LIVE 호환). 원본보다 큰 해상도로 무의미하게 키우지 않는다(16:9 기준).

    codec="hevc"는 사용자가 고를 때만 쓰는 저장·업로드용 선택지다 (LIVE READY 조건을 바꾸지 않음).
    """
    use_hevc = codec == CODEC_HEVC and hevc_available
    base = _base_spec(infos, quality, hevc=use_hevc)
    if codec == CODEC_HEVC and not hevc_available:
        base = replace(base, notes=base.notes + ("이 FFmpeg에는 H.265(libx265) 인코더가 없어 H.264로 만듭니다.",))
    prof = get_profile(profile)
    crf = base.crf + prof.crf_offset
    spec = replace(base, crf=crf, preset=prof.hevc_preset if use_hevc else prof.preset, size_factor=prof.size_factor,
                   profile=prof.key, maxrate_kbps=_scaled_rate(prof.maxrate_kbps, base, use_hevc),
                   bufsize_kbps=_scaled_rate(prof.maxrate_kbps * 2, base, use_hevc))
    cap = f", 상한 {spec.maxrate_kbps / 1000:g} Mbps" if spec.maxrate_kbps else ""
    return replace(spec, label=f"{base.label} · {prof.label} (CRF {crf}, {spec.preset}{cap})")


def _scaled_rate(kbps_fhd: int, base: OutputSpec, hevc: bool) -> int:
    """프로필 상한(FHD 30fps H.264 기준)을 해상도·FPS·코덱에 맞게 비례 조정."""
    if kbps_fhd <= 0:
        return 0
    k = (base.width * base.height) / (1920 * 1080)
    k *= max(1.0, base.fps / 30.0) ** 0.6
    if hevc:
        k *= HEVC_CAP_RATIO
    return int(round(kbps_fhd * k / 100.0)) * 100


def _base_spec(infos: Sequence[VideoInfo], quality: str, *, hevc: bool) -> OutputSpec:
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
    name = f"4K {width}×{height}" if height >= 2160 else f"{width}×{height}"
    if hevc:
        notes.append("H.265는 저장·업로드용입니다. ② LIVE DIRECT COPY에는 H.264 출력을 쓰세요.")
        return OutputSpec(width, height, fps, CODEC_HEVC, HEVC_BASE_CRF, "medium", f"{name} H.265 (저장·업로드용)",
                          tuple(notes))
    if height >= 2160:
        return OutputSpec(width, height, fps, CODEC_H264, 19, "fast", f"{name} H.264", tuple(notes))
    return OutputSpec(width, height, fps, CODEC_H264, 18, "fast", f"{name} H.264 고화질", tuple(notes))


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
    work_size = int(bps * cycle)
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
    if spec.maxrate_kbps > 0:
        cmd += ["-maxrate", f"{spec.maxrate_kbps}k", "-bufsize", f"{spec.bufsize_kbps or spec.maxrate_kbps * 2}k"]
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


def _measurement_plan(plan: BgPlan, count: int, seconds: float) -> list[tuple[Piece, float]]:
    """(시험 구간, 가중치=그 구간이 대표하는 본문 길이). 장면마다 1곳씩 — 장면이 count개보다 많으면 고르게 count곳.

    예전에는 6개 장면 중 4곳만 재고 단순 평균해, 세밀한 장면이 빠지면 실제보다 22% 작게 나왔다 (실환경 GUI 검증).
    """
    bodies = [p for p in plan.pieces if p.kind == "body" and p.length >= 0.5]
    if not bodies:
        return []
    k = min(count, len(bodies))
    groups = [bodies[int(i * len(bodies) / k):int((i + 1) * len(bodies) / k)] for i in range(k)]
    out = []
    for g in groups:
        p = max(g, key=lambda x: x.length)
        ln = min(seconds, p.length)
        start = p.a.start + max(0.0, (p.length - ln) / 2)
        out.append((Piece("body", Segment(p.a.path, start, ln), None, ln, f"용량 측정 · {p.label}"),
                    sum(x.length for x in g)))
    return out


def measurement_pieces(plan: BgPlan, count: int = MEASURE_SAMPLES, seconds: float = MEASURE_SECONDS) -> list[Piece]:
    """예상 용량 실측용 짧은 구간: 각 장면(본문) 가운데에서 seconds초씩 (최대 count곳)."""
    return [p for p, _ in _measurement_plan(plan, count, seconds)]


def measure_bitrate(ffmpeg: Path, plan: BgPlan, cancel_event: Event, work_dir: Path,
                    progress_cb: Callable[[float, str], None] | None = None) -> float:
    """실제 제작과 같은 설정으로 짧은 구간 몇 곳을 시험 인코딩해 평균 바이트/초를 잰다 (원본은 읽기만).

    시험 파일은 work_dir 안 이 함수가 만든 임시 폴더에만 쓰고 끝나면 지운다.
    각 구간이 키프레임으로 시작하므로 실제보다 약간 크게 나오는 편(=안전한 쪽)이다.
    """
    picks = _measurement_plan(plan, MEASURE_SAMPLES, MEASURE_SECONDS)
    if not picks:
        raise BuildError("용량을 잴 장면이 없습니다.")
    tmp = Path(work_dir) / f"{TEMP_PREFIX}measure_{uuid.uuid4().hex[:8]}"
    tmp.mkdir(parents=True, exist_ok=False)
    try:
        weighted = 0.0  # Σ (구간 바이트/초 × 그 구간이 대표하는 본문 길이)
        weight = 0.0
        for k, (p, w) in enumerate(picks):
            if cancel_event.is_set():
                raise BuildCancelled("사용자가 작업을 중지했습니다.")
            if progress_cb:
                progress_cb(k / len(picks), f"예상 용량 실측 중 ({k + 1}/{len(picks)})")
            out = tmp / f"m{k}.mp4"
            run_ffmpeg_progress(piece_command(ffmpeg, p, plan.spec, out), cancel_event)
            size = _file_size(out)
            if size > 0 and p.length > 0:
                weighted += size / p.length * w
                weight += w
        if weighted <= 0 or weight <= 0:
            raise BuildError("시험 인코딩 결과가 비어 있습니다.")
        if progress_cb:
            progress_cb(1.0, "예상 용량 실측 완료")
        return weighted / weight
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def with_measured_rate(plan: BgPlan, bytes_per_second: float) -> BgPlan:
    """실측 비트레이트를 반영한 계획 (예상 크기 = 실측 × 길이, 공간 검사 = 실측 × MEASURE_HEADROOM)."""
    return replace(plan, measured_bps=float(bytes_per_second), expected_size=int(bytes_per_second * plan.target),
                   work_size=int(bytes_per_second * plan.cycle_duration))


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
    """plan대로 디졸브 편집 → 목표 길이 반복. 원본은 읽기만 하고, 임시파일은 출력 폴더(또는 temp_root)에만 만든다.

    저장 공간 보호 (CRF는 크기가 고정되지 않으므로 3단계로 확인):
    1) 시작 전: 보수적 비트레이트로 계산한 최대 동시 점유량(peak_bytes) + 여유 1 GB 이상 필요 (기존 검사).
    2) 조각마다: 실제로 인코딩된 크기로 비트레이트를 다시 계산해, 남은 작업량이 남은 공간보다 크면 미리 중단.
    3) 한 바퀴 완성 후: cycle.mp4 실제 크기 × 반복 = 최종 크기로 다시 검사.
    그리고 작업 내내 2초마다 디스크 잔량을 확인한다. 중단 시 이 작업이 만든 임시 폴더와 .part.mp4만 지운다.
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    sources = {p.a.path.resolve() for p in plan.pieces} | {p.b.path.resolve() for p in plan.pieces if p.b}
    if output.resolve() in sources:
        raise BuildError("원본 영상과 같은 파일명으로 저장할 수 없습니다.")
    if output.exists():
        raise BuildError(f"출력 파일이 이미 존재합니다: {output.name}")
    ok, free, need = enough_disk_space(output.parent, plan.required_bytes)
    if not ok:
        raise BuildError(f"저장 공간 부족: 남음 {free / 1024**3:.1f} GB / 필요 약 {need / 1024**3:.1f} GB "
                         f"(최종 약 {plan.expected_size / 1024**3:.1f} GB + 작업용 임시 약 "
                         f"{(plan.required_bytes - plan.expected_size) / 1024**3:.1f} GB)")

    started = time.monotonic()
    temp_dir = Path(temp_root or output.parent) / f"{TEMP_PREFIX}{uuid.uuid4().hex[:10]}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    part = output.with_name(output.name + ".part.mp4")  # core.run_concat_copy가 만드는 임시 최종 파일
    try:  # 강제 종료(전원·작업 관리자)로 finally가 못 돌면 다음 제작 때 cleanup_stale_temp가 이 이름만 지운다
        (temp_dir / OWNER_FILE).write_text(part.name, encoding="utf-8")
    except OSError:
        pass
    encode_total = sum(p.length for p in plan.pieces)
    encode_weight = 0.8
    stop = Event()  # 사용자 중지 또는 디스크 위험 → 실행 중인 FFmpeg 종료
    watch = _DiskWatch(output.parent, temp_dir, cancel_event, stop)
    watch.remaining = lambda: max(0, plan.required_bytes - _dir_bytes(temp_dir))
    watch.start()

    def report(frac: float, text: str) -> None:
        if progress_cb:
            progress_cb(min(0.999, max(0.0, frac)), text)

    def check_stop() -> None:
        if stop.is_set() or cancel_event.is_set():
            watch.raise_reason()

    try:
        files: list[Path] = []
        done = 0.0
        written = 0
        for idx, piece in enumerate(plan.pieces):
            check_stop()
            out = temp_dir / f"p{idx:04d}.mp4"
            base = done

            def on_sec(sec: float, base=base, piece=piece, idx=idx) -> None:
                frac = (base + min(sec, piece.length)) / encode_total * encode_weight
                report(frac, f"디졸브 편집 중 · {piece.label} ({idx + 1}/{len(plan.pieces)})")

            on_sec(0.0)
            try:
                run_ffmpeg_progress(piece_command(ffmpeg, piece, plan.spec, out), stop, on_sec)
            except BuildCancelled:
                watch.raise_reason()
            if not out.exists() or out.stat().st_size == 0:
                raise BuildError(f"편집 조각이 만들어지지 않았습니다: {piece.label}")
            files.append(out)
            done += piece.length
            written += out.stat().st_size
            # 2) 실측 비트레이트로 남은 작업량 재계산 (충분히 인코딩된 뒤부터: 3초 이상 또는 본문 조각 1개 이상)
            if done >= min(3.0, encode_total):
                rate = written / done * PROJECTION_HEADROOM
                cyc = rate * plan.cycle_duration
                peak = peak_bytes(cyc, rate * plan.target)
                watch.remaining = lambda peak=peak: max(0, peak - _dir_bytes(temp_dir))
                _check_projection(output.parent, temp_dir, peak - _dir_bytes(temp_dir), rate * plan.target,
                                  "실제 인코딩 크기로 다시 계산해 보니")

        report(encode_weight, "한 바퀴 영상 합치는 중 (무손실)")
        cycle = temp_dir / "cycle.mp4"
        try:
            _concat_copy_simple(ffmpeg, files, cycle, stop)
        except BuildCancelled:
            watch.raise_reason()
        for f in files:
            try:
                f.unlink()
            except OSError:
                pass
        report(encode_weight + 0.02, "한 바퀴 영상 검사 중 (디코드 확인)")
        try:
            decode_check(ffmpeg, cycle, stop)
        except BuildCancelled:
            watch.raise_reason()
        cinfo = probe_video(cycle, ffprobe)
        if (cinfo.width, cinfo.height) != (plan.spec.width, plan.spec.height):
            raise BuildError("편집본 해상도가 예상과 다릅니다.")
        if abs(cinfo.duration - plan.cycle_duration) > max(0.5, 3 / plan.spec.fps * len(plan.pieces)):
            raise BuildError(f"편집본 길이 검증 실패: 예상 {plan.cycle_duration:.2f}초, 실제 {cinfo.duration:.2f}초")

        repeats = max(1, math.ceil(plan.target / cinfo.duration - 1e-9))
        final_size = int(cinfo.size * min(plan.target, cinfo.duration * repeats) / cinfo.duration)
        # 3) 한 바퀴 실제 크기로 최종 MP4 공간 재검사 (cycle.mp4는 이미 디스크에 있으므로 최종 크기만 필요)
        ok, free, need = enough_disk_space(output.parent, final_size)
        if not ok:
            raise BuildError(f"저장 공간 부족 (한 바퀴 실제 크기로 다시 계산): 최종 영상 약 {final_size / 1024**3:.2f} GB, "
                             f"남음 {free / 1024**3:.1f} GB / 필요 약 {need / 1024**3:.1f} GB. "
                             "원본과 기존 파일은 그대로이며, 이 작업의 임시 파일만 지웠습니다. "
                             "공간을 확보하거나 길이를 줄이거나 [화질·용량]에서 용량이 작은 설정을 고르세요.")
        watch.remaining = lambda: max(0, int(final_size * 1.02) - _file_size(part))
        long_plan = BuildPlan(sequence=(cycle,) * repeats, expected_duration=plan.target,
                              expected_size=final_size, cycles=repeats, trim_to=plan.target, mode="time")
        base = encode_weight + 0.04

        def on_long(frac: float, _text: str) -> None:
            report(base + frac * (1 - base), f"목표 길이로 무손실 반복 연결 중 · {repeats}바퀴")

        try:
            r = run_concat_copy(ffmpeg=ffmpeg, ffprobe=ffprobe, infos=[cinfo], plan=long_plan, output=output,
                                cancel_event=stop, progress_cb=on_long)
        except BuildCancelled:
            watch.raise_reason()
        except BuildError as e:
            if "No space left" in str(e) or "not enough space" in str(e):
                raise BuildError("디스크 공간이 부족해 제작을 멈췄습니다. 원본과 기존 파일은 그대로이며, "
                                 "이 작업의 임시 파일만 지웠습니다.\n" + str(e)) from e
            raise
        final = probe_video(r.output, ffprobe)
        if progress_cb:
            progress_cb(1.0, "완료")
        return BgResult(r.output, final.duration, final.size, final.width, final.height, final.fps,
                        cinfo.duration, repeats, time.monotonic() - started)
    finally:
        watch.close()
        shutil.rmtree(temp_dir, ignore_errors=True)
        try:  # run_concat_copy도 지우지만, 예외 경로에서 남지 않게 한 번 더 (이 작업이 만든 이름만)
            if part.exists():
                part.unlink()
        except OSError:
            pass


def _file_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _dir_bytes(folder: Path) -> int:
    total = 0
    try:
        for p in Path(folder).iterdir():
            total += _file_size(p)
    except OSError:
        pass
    return total


def _same_drive(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return True


def _check_projection(out_dir: Path, temp_dir: Path, remaining: float, final: float, why: str) -> None:
    free = free_bytes(out_dir)
    need = max(0.0, remaining)
    if not _same_drive(out_dir, temp_dir):  # 임시 폴더가 다른 드라이브면 출력 드라이브에는 최종 파일만
        need = final
    if free < need + DISK_FLOOR:
        raise BuildError(f"저장 공간 부족 예상: {why} 남은 작업에 약 {need / 1024**3:.2f} GB가 더 필요하지만 "
                         f"남은 공간은 {free / 1024**3:.2f} GB입니다. 디스크가 가득 차기 전에 미리 멈췄습니다. "
                         "원본과 기존 파일은 그대로이며, 이 작업의 임시 파일만 지웠습니다. "
                         "공간을 확보하거나 길이를 줄이거나 [화질·용량]에서 용량이 작은 설정을 고르세요.")


class _DiskWatch:
    """작업 중 디스크 잔량 감시 스레드. 위험하면 stop을 켜서 실행 중인 FFmpeg를 끝내게 한다.

    사용자 중지(cancel_event)도 여기서 stop으로 전달한다 (FFmpeg 실행기는 stop만 본다).
    """

    def __init__(self, out_dir: Path, temp_dir: Path, cancel_event: Event, stop: Event):
        self.out_dir, self.temp_dir = Path(out_dir), Path(temp_dir)
        self.cancel_event, self.stop = cancel_event, stop
        self.remaining: Callable[[], int] = lambda: 0
        self.reason: str | None = None
        self._done = Event()
        self._thread = threading.Thread(target=self._run, name="bg-disk-watch", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._done.set()
        self._thread.join(timeout=5)

    def check_now(self) -> None:
        try:
            free = free_bytes(self.out_dir)
            remaining = self.remaining() if _same_drive(self.out_dir, self.temp_dir) else 0
        except Exception:
            return
        if free < DISK_FLOOR or free < remaining + DISK_FLOOR // 2:
            self.reason = (f"작업 중 디스크 남은 공간이 위험할 만큼 줄어 안전하게 멈췄습니다 (남음 {free / 1024**3:.2f} GB, "
                           f"남은 작업 약 {remaining / 1024**3:.2f} GB). 원본과 기존 파일은 그대로이며, "
                           "이 작업의 임시 파일만 지웠습니다. 다른 프로그램이 디스크를 쓰고 있지 않은지 확인하세요.")
            self.stop.set()

    def _run(self) -> None:
        last = 0.0
        while not self._done.wait(0.2):
            if self.cancel_event.is_set():
                self.stop.set()
            now = time.monotonic()
            if now - last >= DISK_POLL_SECONDS and self.reason is None:
                last = now
                self.check_now()

    def raise_reason(self) -> None:
        if self.reason:
            raise BuildError(self.reason)
        raise BuildCancelled("사용자가 작업을 중지했습니다.")


# ---------------- 반복 경계 미리보기 ----------------

PREVIEW_CONTEXT = 2.5  # 경계 앞뒤로 보여 줄 초
PREVIEW_SPEC = OutputSpec(640, 360, 30.0, "h264", 26, "veryfast", "미리보기 640×360")


def seam_segments(clip_a: Clip, clip_b: Clip, dissolve: float, context: float = PREVIEW_CONTEXT) -> tuple[Segment, Segment]:
    """clip_a 사용 구간의 마지막 (context+디졸브)초, clip_b 사용 구간의 처음 (디졸브+context)초.

    같은 클립을 두 번 주면 '이 영상만 반복할 때의 이음새(끝 → 처음)'가 된다.
    """
    la = min(clip_a.used_length, context + dissolve)
    lb = min(clip_b.used_length, context + dissolve)
    if la < dissolve + 0.2 or lb < dissolve + 0.2:
        raise ValueError("사용 구간이 너무 짧아 미리보기를 만들 수 없습니다.")
    return Segment(clip_a.path, clip_a.used_end - la, la), Segment(clip_b.path, clip_b.used_start, lb)


def seam_preview_command(ffmpeg: Path, a: Segment, b: Segment, dissolve: float, out: Path,
                         spec: OutputSpec = PREVIEW_SPEC) -> list[str]:
    """제작과 같은 정규화·디졸브(xfade)로 경계 부분만 저해상도로 빠르게 만든다 (소리 없음, 원본은 읽기만)."""
    cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-nostdin"]
    for seg in (a, b):
        cmd += ["-ss", f"{seg.start:.6f}", "-t", f"{seg.length + 0.5:.6f}", "-i", str(seg.path)]
    total = a.length + b.length - dissolve
    graph = (f"[0:v]{_norm_chain(spec, a.length)}[a];[1:v]{_norm_chain(spec, b.length)}[b];"
             f"[a][b]xfade=transition=fade:duration={dissolve:.6f}:offset={a.length - dissolve:.6f},"
             f"trim=duration={total:.6f},setpts=PTS-STARTPTS[v]")
    cmd += ["-filter_complex", graph, "-map", "[v]", "-an", "-sn", "-dn", "-map_metadata", "-1",
            "-c:v", spec.encoder, "-preset", spec.preset, "-crf", str(spec.crf), "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(out)]
    return cmd


def make_seam_preview(ffmpeg: Path, clip_a: Clip, clip_b: Clip, dissolve: float, out: Path,
                      cancel_event: Event) -> Path:
    a, b = seam_segments(clip_a, clip_b, dissolve)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_ffmpeg_progress(seam_preview_command(ffmpeg, a, b, dissolve, out), cancel_event)
    except BaseException:
        try:
            out.unlink()
        except OSError:
            pass
        raise
    if not out.exists() or out.stat().st_size == 0:
        raise BuildError("미리보기를 만들지 못했습니다.")
    return out


def cleanup_stale_temp(folder: Path, max_age_hours: float = 12.0) -> int:
    """이전에 비정상 종료로 남은 이 기능의 임시 폴더만 정리한다 (이름 접두사로 구분, 원본 영상은 건드리지 않음)."""
    removed = 0
    try:
        now = time.time()
        for p in Path(folder).glob(f"{TEMP_PREFIX}*"):
            if p.is_dir() and now - p.stat().st_mtime > max_age_hours * 3600:
                _remove_owned_part(Path(folder), p, now, max_age_hours)
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
        for p in Path(folder).glob(".PLVM_TEMP_*"):
            if p.is_dir() and now - p.stat().st_mtime > max_age_hours * 3600 and _owned_concat_dir(p):
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
    except OSError:
        pass
    return removed


def _owned_concat_dir(d: Path) -> bool:
    """① 무손실 연결(core.run_concat_copy)이 ④ 반복 연결 때 만든 임시 폴더인가.

    강제 종료되면 ①의 finally가 못 돌아 concat.txt만 남는다. concat.txt 하나만 있고 모든 줄이 같은 폴더의
    ④ 임시 폴더(.PLVM_BG_TEMP_*) 안 파일을 가리킬 때만 '이 기능 소유'로 본다 — ① 영상 늘리기의 임시 폴더는 건드리지 않음.
    """
    try:
        items = list(d.iterdir())
        if len(items) != 1 or items[0].name != "concat.txt" or items[0].stat().st_size > 1024 * 1024:
            return False
        lines = [ln.strip() for ln in items[0].read_text(encoding="utf-8").splitlines() if ln.strip()]
    except (OSError, UnicodeDecodeError):
        return False
    prefix = f"file '{escape_concat_path(d.parent)}/{TEMP_PREFIX}"
    return bool(lines) and all(ln.startswith(prefix) and "/.." not in ln for ln in lines)


def _remove_owned_part(folder: Path, temp_dir: Path, now: float, max_age_hours: float) -> None:
    """임시 폴더의 owner.txt에 적힌 '.part.mp4' 1개만 지운다 (같은 폴더, 이름 형식·오래됨 확인). 완성 MP4는 절대 아님."""
    try:
        name = (temp_dir / OWNER_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return
    if not name or Path(name).name != name or not name.endswith(".mp4.part.mp4"):
        return
    part = folder / name
    try:
        if part.is_file() and now - part.stat().st_mtime > max_age_hours * 3600:
            part.unlink()
    except OSError:
        pass


def clip_from_info(info: VideoInfo) -> Clip:
    return Clip(info=info)


def with_range(clip: Clip, start: float, end: float | None) -> Clip:
    return replace(clip, start=max(0.0, start), end=end)


__all__ = [
    "Clip", "OutputSpec", "Segment", "Piece", "BgPlan", "BgResult", "TARGET_MINUTES", "DISSOLVE_CHOICES",
    "DEFAULT_DISSOLVE", "QUALITY_FHD", "QUALITY_4K", "GUARD_OWNER", "choose_output_spec", "build_bg_plan",
    "default_bg_output_name", "piece_command", "run_background_build", "run_ffmpeg_progress", "hevc_available",
    "cleanup_stale_temp", "decode_check", "probe_video", "fps_expr", "EncodeProfile", "ENCODE_PROFILES",
    "PROFILE_BEST", "get_profile", "peak_bytes", "seam_segments", "seam_preview_command", "make_seam_preview",
    "CODEC_H264", "CODEC_HEVC", "HEVC_BASE_CRF", "measure_bitrate", "measurement_pieces", "with_measured_rate",
    "MEASURE_HEADROOM", "OWNER_FILE",
]
