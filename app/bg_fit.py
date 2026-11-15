"""④ 화면 비율 자동 변환 — 세로·정사각형·4:3·21:9 영상과 사진을 16:9 배경 화면에 맞춘다.

원칙
- 원본을 가로·세로로 찌그러뜨리지 않는다 (모든 방식이 원본 비율 그대로 확대·축소만 한다).
- 화면 밖 장면을 새로 만들지 않는다 (생성형 AI 없음). 흐림 배경은 같은 화면을 크게 확대해 흐리게 깐 것이다.
- Python은 영상 프레임을 디코딩하지 않는다. 회전·SAR·해상도는 ffprobe, 사진 EXIF 방향은 파일 앞부분 바이트만 읽는다.
- 16:9(±2%) 원본은 어떤 방식을 골라도 기존 ④와 똑같은 필터(확대 + 가운데 자르기)를 쓴다 (기존 결과 그대로).

화면 맞춤 방식
- 흐림 배경 (기본): 원본 전체를 가운데에 두고, 남는 곳은 같은 화면을 확대·흐림·살짝 어둡게 한 배경으로 채운다.
- 꽉 채우기: 16:9를 꽉 채우도록 확대 후 가운데를 자른다 (세로 영상은 많이 잘림 → 경고).
- 원본 전체: 원본 전체 + 단색 여백 (검정 기본).
"""
from __future__ import annotations

import json
import math
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Sequence

from .core import BuildError, VideoInfo, creationflags_no_window

FIT_BLUR = "blur"
FIT_FILL = "fill"
FIT_CONTAIN = "contain"
FIT_CHOICES = (FIT_BLUR, FIT_FILL, FIT_CONTAIN)
DEFAULT_FIT = FIT_BLUR
FIT_LABELS = {
    FIT_BLUR: "흐림 배경 (기본·잘림 없음)",
    FIT_FILL: "꽉 채우기 (가장자리 잘림)",
    FIT_CONTAIN: "원본 전체 (단색 여백)",
}
FIT_SHORT = {FIT_BLUR: "흐림 배경", FIT_FILL: "꽉 채우기", FIT_CONTAIN: "원본 전체"}

BLUR_LEVELS = (1, 2, 3)            # 약 · 중 · 강
DEFAULT_BLUR = 2
BLUR_LABELS = {1: "약하게", 2: "보통 (기본)", 3: "강하게"}
BLUR_SIGMA = {1: 3.0, 2: 6.0, 3: 10.0}   # 작은 배경 캔버스(BG_W×BG_H) 기준 가우시안 반경
BG_W, BG_H = 320, 180              # 흐림 배경은 작게 줄여 흐린 뒤 키운다 (빠르고 부드러움, 메모리 일정)
BG_EQ = "eq=brightness=-0.05:saturation=0.88"  # 배경만 살짝 어둡고 덜 진하게 → 원본이 자연스럽게 앞에 보임

PAD_COLORS = {"검정 (기본)": "0x000000", "짙은 회색": "0x202020", "흰색": "0xFFFFFF"}
DEFAULT_PAD = "0x000000"

ASPECT_TOLERANCE = 0.02            # 16:9와 이 비율 이내면 '16:9'로 보고 기존 필터 그대로
CROP_SEVERE = 0.60                 # 꽉 채우기에서 남는 화면이 이보다 적으면 '심각한 잘림'
CROP_NOTICE = 0.85
UPSCALE_NOTICE = 1.10              # 이보다 크게 확대하면 화질 저하 안내
UPSCALE_STRONG = 1.60

# ---- 사진 ----
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".m4v")
IMAGE_CODECS = {"mjpeg", "png", "webp", "jpegls", "apng"}
STILL_CHOICES = (10, 20, 30, 60)
DEFAULT_STILL = 30.0
MIN_STILL, MAX_STILL = 3.0, 600.0

MOTION_NONE = "none"
MOTION_ZOOM_IN = "zoom_in"
MOTION_ZOOM_OUT = "zoom_out"
MOTION_PAN = "pan"
MOTION_CHOICES = (MOTION_NONE, MOTION_ZOOM_IN, MOTION_ZOOM_OUT, MOTION_PAN)
DEFAULT_MOTION = MOTION_NONE
MOTION_LABELS = {MOTION_NONE: "정지 화면 유지 (기본)", MOTION_ZOOM_IN: "아주 느린 줌인", MOTION_ZOOM_OUT: "아주 느린 줌아웃",
                 MOTION_PAN: "아주 느린 좌→우 이동"}
MOTION_RATE = 0.002                # 초당 확대율 0.2% (30초 → 6%) — 거의 느껴지지 않을 만큼 느리게
MOTION_MAX = 0.08

STILL_TAG = "정지 이미지"           # 목록·경고에 쓰는 표시 (실제 촬영 영상이라고 표시하지 않음)


def is_image_path(path: Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTS


# ---------------- 원본 정보 (회전 · SAR · EXIF) ----------------

@dataclass(frozen=True)
class MediaMeta:
    """원본 1개의 화면 정보. width/height(VideoInfo)는 회전·SAR을 반영한 '실제로 보이는' 크기로 바꿔 둔다."""
    is_image: bool = False
    coded_w: int = 0
    coded_h: int = 0
    sar: float = 1.0               # 화소 가로/세로 비 (1 = 정사각 화소)
    rotation: int = 0              # 영상 회전 메타데이터 (0/90/180/270, 시계 방향)
    exif_orientation: int = 1      # 사진 EXIF 방향 (1~8)

    @property
    def pre_filter(self) -> str:
        """화면 맞춤 전에 넣는 필터. 영상 회전은 FFmpeg가 자동 적용(autorotate)하므로 여기서 하지 않는다.
        사진은 -noautorotate로 읽고 EXIF 방향을 직접 적용한다 (FFmpeg 버전마다 EXIF 처리 여부가 달라서)."""
        parts: list[str] = []
        if self.is_image:
            parts += EXIF_FILTERS.get(self.exif_orientation, [])
        if abs(self.sar - 1.0) > 0.01:  # 회전 뒤에도 맞게 FFmpeg의 sar 변수로 (transpose가 SAR도 뒤집음)
            parts.append("scale='trunc(iw*sar/2)*2':ih:flags=lanczos,setsar=1")
        return ",".join(parts)


EXIF_FILTERS = {
    2: ["hflip"], 3: ["hflip", "vflip"], 4: ["vflip"], 5: ["transpose=0"], 6: ["transpose=1"],
    7: ["transpose=3"], 8: ["transpose=2"],
}


def display_size(w: int, h: int, sar: float = 1.0, rotation: int = 0, exif: int = 1) -> tuple[int, int]:
    """보이는 크기 (SAR 반영 → 회전 90/270 또는 EXIF 5~8이면 가로·세로 교환)."""
    dw = int(round(w * (sar if sar > 0 else 1.0)))
    dh = int(h)
    if rotation % 180 == 90 or exif in (5, 6, 7, 8):
        dw, dh = dh, dw
    return max(1, dw), max(1, dh)


def _parse_ratio(s) -> float:
    try:
        a, b = str(s).split(":", 1) if ":" in str(s) else str(s).split("/", 1)
        a, b = float(a), float(b)
        return a / b if a > 0 and b > 0 else 1.0
    except (TypeError, ValueError):
        return 1.0


def _norm_rotation(v) -> int:
    try:
        r = int(round(float(v))) % 360
    except (TypeError, ValueError):
        return 0
    return min((0, 90, 180, 270), key=lambda x: min(abs(r - x), 360 - abs(r - x)))


def read_exif_orientation(path: Path) -> int:
    """JPEG(APP1 Exif) · PNG(eXIf) · WebP(EXIF 청크)의 방향 태그(0x0112). 없거나 읽을 수 없으면 1.

    파일 앞부분 최대 1 MB만 읽는다 (이미지 디코딩 아님)."""
    try:
        with open(path, "rb") as f:
            head = f.read(1024 * 1024)
    except OSError:
        return 1
    tiff = b""
    if head[:2] == b"\xff\xd8":                       # JPEG
        i = 2
        while i + 4 <= len(head) and head[i] == 0xFF:
            marker = head[i + 1]
            if marker in (0xD9, 0xDA):
                break
            seg_len = struct.unpack(">H", head[i + 2:i + 4])[0]
            if marker == 0xE1 and head[i + 4:i + 10] == b"Exif\x00\x00":
                tiff = head[i + 10:i + 2 + seg_len]
                break
            i += 2 + seg_len
    elif head[:8] == b"\x89PNG\r\n\x1a\n":            # PNG
        i = 8
        while i + 8 <= len(head):
            n = struct.unpack(">I", head[i:i + 4])[0]
            kind = head[i + 4:i + 8]
            if kind == b"eXIf":
                tiff = head[i + 8:i + 8 + n]
                break
            if kind == b"IDAT":
                break
            i += 12 + n
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":   # WebP
        i = 12
        while i + 8 <= len(head):
            kind = head[i:i + 4]
            n = struct.unpack("<I", head[i + 4:i + 8])[0]
            if kind == b"EXIF":
                tiff = head[i + 8:i + 8 + n]
                if tiff.startswith(b"Exif\x00\x00"):
                    tiff = tiff[6:]
                break
            i += 8 + n + (n & 1)
    return _tiff_orientation(tiff)


def _tiff_orientation(tiff: bytes) -> int:
    if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        return 1
    e = "<" if tiff[:2] == b"II" else ">"
    try:
        off = struct.unpack(e + "I", tiff[4:8])[0]
        count = struct.unpack(e + "H", tiff[off:off + 2])[0]
        for k in range(count):
            p = off + 2 + 12 * k
            tag, typ = struct.unpack(e + "HH", tiff[p:p + 4])
            if tag == 0x0112:
                v = struct.unpack(e + "H", tiff[p + 8:p + 10])[0] if typ == 3 else struct.unpack(e + "I", tiff[p + 8:p + 12])[0]
                return int(v) if 1 <= int(v) <= 8 else 1
    except (struct.error, IndexError):
        return 1
    return 1


def _ffprobe_stream(ffprobe: Path, path: Path) -> tuple[dict, dict]:
    cmd = [str(ffprobe), "-v", "error", "-select_streams", "v:0", "-show_entries",
           "stream=codec_name,width,height,sample_aspect_ratio,pix_fmt,nb_frames:stream_tags=rotate:"
           "stream_side_data=rotation:format=format_name,size", "-of", "json", str(path)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                           creationflags=creationflags_no_window())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BuildError(f"ffprobe를 실행할 수 없습니다: {e}") from e
    if p.returncode != 0:
        raise BuildError(f"파일을 읽을 수 없습니다: {Path(path).name}\n{p.stderr.strip()[-300:]}")
    try:
        data = json.loads(p.stdout or "{}")
    except ValueError as e:
        raise BuildError(f"파일 정보 분석에 실패했습니다: {Path(path).name}") from e
    streams = data.get("streams") or []
    if not streams:
        raise BuildError(f"영상·사진 화면이 없는 파일입니다: {Path(path).name}")
    return streams[0], data.get("format") or {}


def _rotation_of(st: dict) -> int:
    for sd in st.get("side_data_list") or []:
        if "rotation" in sd:
            # display matrix rotation은 반시계(-90 = 시계 90). 화면 크기 판단에는 90/270 여부만 쓴다
            return _norm_rotation(-float(sd["rotation"]))
    tags = st.get("tags") or {}
    if "rotate" in tags:
        return _norm_rotation(tags["rotate"])
    return 0


def probe_media(path: Path, ffprobe: Path, still_seconds: float = DEFAULT_STILL) -> tuple[VideoInfo, MediaMeta]:
    """영상·사진 1개 → (VideoInfo, MediaMeta). VideoInfo의 width/height는 '보이는' 크기(회전·SAR 반영).

    사진: duration = 표시 시간(still_seconds), fps = 0 (출력 FPS 결정에 참여하지 않음), 오디오 없음."""
    from .core import probe_video
    path = Path(path)
    if is_image_path(path):
        st, fmt = _ffprobe_stream(ffprobe, path)
        codec = str(st.get("codec_name") or "")
        if codec not in IMAGE_CODECS:
            raise BuildError(f"지원하지 않는 사진 형식입니다: {path.name} ({codec or '알 수 없음'})")
        w, h = int(st.get("width") or 0), int(st.get("height") or 0)
        if w < 16 or h < 16:
            raise BuildError(f"사진 크기를 읽을 수 없습니다: {path.name}")
        sar = _parse_ratio(st.get("sample_aspect_ratio") or "1:1")
        exif = read_exif_orientation(path)
        dw, dh = display_size(w, h, sar, 0, exif)
        meta = MediaMeta(True, w, h, sar, 0, exif)
        size = int(fmt.get("size") or 0) or path.stat().st_size
        info = VideoInfo(path.resolve(), float(clamp_still(still_seconds)), size, dw, dh, 0.0, codec, "",
                         str(st.get("pix_fmt") or ""), STILL_TAG, 0, 0)
        return info, meta
    info = probe_video(path, ffprobe)
    try:
        st, _ = _ffprobe_stream(ffprobe, path)
    except BuildError:
        return info, MediaMeta(False, info.width, info.height)
    sar = _parse_ratio(st.get("sample_aspect_ratio") or "1:1")
    rot = _rotation_of(st)
    w, h = int(st.get("width") or info.width), int(st.get("height") or info.height)
    dw, dh = display_size(w, h, sar, rot)
    meta = MediaMeta(False, w, h, sar, rot, 1)
    from dataclasses import replace
    return replace(info, width=dw, height=dh), meta


def clamp_still(seconds: float) -> float:
    try:
        v = float(seconds)
    except (TypeError, ValueError):
        return DEFAULT_STILL
    if not math.isfinite(v):
        return DEFAULT_STILL
    return max(MIN_STILL, min(MAX_STILL, v))


def parse_still(text: str) -> float:
    """사진 표시 시간 직접 입력 (초 또는 MM:SS). 잘못되면 ValueError."""
    s = (text or "").strip().replace("초", "").strip()
    try:
        if ":" in s:
            m, sec = s.split(":", 1)
            v = int(m) * 60 + float(sec)
        else:
            v = float(s)
    except ValueError:
        raise ValueError("사진 표시 시간은 초(예: 30) 또는 분:초(예: 1:30)로 입력하세요.") from None
    if not math.isfinite(v) or v < MIN_STILL or v > MAX_STILL:
        raise ValueError(f"사진 표시 시간은 {MIN_STILL:g}~{MAX_STILL:g}초 사이로 입력하세요.")
    return v


# ---------------- 비율 · 맞춤 계산 ----------------

def aspect_label(w: int, h: int) -> str:
    if w <= 0 or h <= 0:
        return "?"
    a = w / h
    named = ((16 / 9, "16:9"), (9 / 16, "9:16 세로"), (1.0, "1:1 정사각형"), (4 / 3, "4:3"), (3 / 4, "3:4 세로"),
             (21 / 9, "21:9 와이드"), (64 / 27, "21:9 와이드"), (2.0, "2:1"), (3 / 2, "3:2"), (2 / 3, "2:3 세로"),
             (4 / 5, "4:5 세로"), (2.35, "2.35:1 와이드"))
    best = min(named, key=lambda x: abs(math.log(a / x[0])))
    if abs(math.log(a / best[0])) < 0.03:
        return best[1]
    return f"{a:.2f}:1" + (" 세로" if a < 1 else "")


def is_169(w: int, h: int) -> bool:
    return w > 0 and h > 0 and abs((w / h) / (16 / 9) - 1) <= ASPECT_TOLERANCE


@dataclass(frozen=True)
class FitGeometry:
    scale: float          # 원본 → 출력 배율 (>1 = 확대)
    fg_w: int             # 화면에 보이는 원본 크기 (꽉 채우기면 출력 크기)
    fg_h: int
    kept: float           # 원본에서 화면에 남는 비율 (꽉 채우기만 < 1)
    lost_axis: str        # 잘리는 방향 "위아래" | "좌우" | ""
    plain: bool           # 16:9라 기존 필터(확대+가운데 자르기) 그대로


def fit_geometry(dw: int, dh: int, W: int, H: int, fit: str) -> FitGeometry:
    if dw <= 0 or dh <= 0:
        return FitGeometry(1.0, W, H, 1.0, "", True)
    plain = is_169(dw, dh)
    if fit == FIT_FILL or plain:
        s = max(W / dw, H / dh)
        kept = min(1.0, (W * H) / (dw * s * dh * s))
        axis = "" if kept > 0.995 else ("위아래" if dw / dh < W / H else "좌우")
        return FitGeometry(s, W, H, kept, axis, plain)
    s = min(W / dw, H / dh)
    return FitGeometry(s, int(round(dw * s)), int(round(dh * s)), 1.0, "", False)


def native_height(info: VideoInfo, fit: str) -> float:
    """출력 해상도(720/1080/2160) 결정용 원본의 세로 화소 등급.

    16:9 원본: 기존 그대로 min(h, w·9/16) (작은 원본만 있으면 출력도 낮춰 쓸데없이 키우지 않음).
    16:9가 아닌 원본: 원본 전체가 들어가는 크기 max(h, w·9/16) — 1080×1920 세로 영상은 1080p 출력.
    꽉 채우기는 잘린 부분이 확대되므로 그 배율을 fit_notes가 따로 안내한다 (fit은 호환을 위해 받기만 함)."""
    w, h = info.width, info.height
    if is_169(w, h):
        return min(h, w * 9 / 16)
    return max(h, w * 9 / 16)


def fit_notes(infos: Sequence[VideoInfo], W: int, H: int, fit: str) -> list[str]:
    """출력 규격 안내: 16:9가 아닌 원본의 처리 방식, 꽉 채우기 잘림 경고, 확대(화질 저하) 안내."""
    notes: list[str] = []
    odd = [i for i in infos if not is_169(i.width, i.height)]
    if odd:
        names = ", ".join(f"{i.path.name}({aspect_label(i.width, i.height)})" for i in odd[:4])
        more = f" 외 {len(odd) - 4}개" if len(odd) > 4 else ""
        if fit == FIT_FILL:
            notes.append(f"16:9가 아닌 원본 {len(odd)}개는 화면을 채우도록 확대 후 가장자리가 잘립니다: {names}{more}")
        elif fit == FIT_CONTAIN:
            notes.append(f"16:9가 아닌 원본 {len(odd)}개는 원본 전체를 보여 주고 남는 곳을 단색 여백으로 채웁니다: {names}{more}")
        else:
            notes.append(f"16:9가 아닌 원본 {len(odd)}개는 원본 전체를 가운데에 두고 남는 곳을 같은 화면의 흐린 배경으로 "
                         f"채웁니다 (새 장면을 만들지 않음): {names}{more}")
    if fit == FIT_FILL:
        for i in odd:
            g = fit_geometry(i.width, i.height, W, H, fit)
            lost = 1 - g.kept
            if g.kept < CROP_SEVERE:
                notes.append(f"⚠ 심각한 잘림: '{i.path.name}'은(는) 원본 화면의 {lost:.0%}가 잘립니다 ({g.lost_axis}) — "
                             "머리·발·주요 피사체가 잘릴 수 있습니다. [흐림 배경]을 권장합니다.")
            elif g.kept < CROP_NOTICE:
                notes.append(f"'{i.path.name}'은(는) {g.lost_axis} {lost:.0%}가 잘립니다.")
    for i in infos:
        g = fit_geometry(i.width, i.height, W, H, fit)
        kind = "사진" if i.fps <= 0 else "영상"
        if g.scale >= UPSCALE_STRONG:
            notes.append(f"확대 주의: {kind} '{i.path.name}' {g.scale:.1f}배 확대 — 원본 {i.width}×{i.height}이 작아 "
                         "흐릿하거나 뭉개져 보일 수 있습니다.")
        elif g.scale >= UPSCALE_NOTICE:
            notes.append(f"확대 안내: {kind} '{i.path.name}' {g.scale:.1f}배 확대 (선명도가 조금 낮아질 수 있음).")
    return notes


# ---------------- 필터 그래프 ----------------

def fit_filter(fit: str, W: int, H: int, dw: int, dh: int, *, blur: int = DEFAULT_BLUR, pad: str = DEFAULT_PAD,
               tag: str = "x") -> tuple[str, bool]:
    """(필터, 분기 여부). 분기(흐림 배경)는 split 라벨을 쓰므로 호출하는 쪽이 앞뒤를 ';'로 잇는다.

    16:9 원본이나 꽉 채우기는 기존 ④와 같은 '확대 + 가운데 자르기' 문자열 그대로."""
    plain = fit == FIT_FILL or dw <= 0 or dh <= 0 or is_169(dw, dh)
    if plain:
        return f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H}", False
    fg = f"scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos"
    if fit == FIT_CONTAIN:
        return f"{fg},pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={pad}", False
    sigma = BLUR_SIGMA.get(int(blur), BLUR_SIGMA[DEFAULT_BLUR])
    graph = (f"split=2[{tag}f][{tag}b];"
             f"[{tag}b]scale={BG_W}:{BG_H}:force_original_aspect_ratio=increase:flags=bilinear,crop={BG_W}:{BG_H},"
             f"gblur=sigma={sigma:g},{BG_EQ},scale={W}:{H}:flags=bicubic[{tag}bb];"
             f"[{tag}f]{fg}[{tag}ff];[{tag}bb][{tag}ff]overlay=x=(W-w)/2:y=(H-h)/2")
    return graph, True


def motion_amount(total: float) -> float:
    return min(MOTION_MAX, MOTION_RATE * max(0.0, total))


def motion_filter(motion: str, W: int, H: int, offset: float, total: float, fps: float = 30.0) -> str:
    """사진 움직임 (화면 맞춤 뒤 W×H 화면에 적용).

    정수 화소 단위로 자르고 키우면(scale+crop, zoompan) 아주 느린 움직임이 몇 프레임마다 1화소씩 '덜컥'거린다.
    perspective 필터는 원본 좌표를 소수점으로 받아 보간하므로 매 프레임 조금씩 부드럽게 움직인다.
    진행률은 프레임 번호(in)로 계산한다 — 앞의 fps 필터로 고정 FPS가 된 뒤라 정확하다.
    offset: 이 조각이 사진 표시 구간의 몇 초 지점부터인지 → 조각이 나뉘어도 움직임이 끊기지 않고 이어진다."""
    if motion not in (MOTION_ZOOM_IN, MOTION_ZOOM_OUT, MOTION_PAN) or total <= 0:
        return ""
    amt = motion_amount(total)
    if amt <= 0:
        return ""
    p = f"min(1,max(0,(in/{fps:.6f}+{offset:.6f})/{total:.6f}))"
    if motion == MOTION_ZOOM_IN:
        z = f"(1+{amt:.5f}*{p})"
    elif motion == MOTION_ZOOM_OUT:
        z = f"(1+{amt:.5f}*(1-{p}))"
    else:
        z = f"{1 + amt:.5f}"
    # 보이는 원본 영역: 가로 W/z, 세로 H/z. 줌은 가운데 기준, 좌→우 이동은 남는 폭(W - W/z)을 진행률만큼
    x0 = f"(W-W/{z})*{p}" if motion == MOTION_PAN else f"(W-W/{z})/2"
    y0 = f"(H-H/{z})/2"
    x1 = f"{x0}+W/{z}"
    y2 = f"{y0}+H/{z}"
    return (f"perspective=x0='{x0}':y0='{y0}':x1='{x1}':y1='{y0}':x2='{x0}':y2='{y2}':x3='{x1}':y3='{y2}'"
            f":interpolation=cubic:eval=frame")


# ---------------- 화면 맞춤 미리보기 (PNG 1장) ----------------

PREVIEW_W, PREVIEW_H = 480, 270


def crop_box(dw: int, dh: int, W: int, H: int) -> tuple[float, float, float, float]:
    """꽉 채우기에서 원본 중 화면에 남는 영역 (원본 비율 0~1: x, y, w, h)."""
    s = max(W / dw, H / dh)
    kw, kh = min(1.0, W / (dw * s)), min(1.0, H / (dh * s))
    return (1 - kw) / 2, (1 - kh) / 2, kw, kh


def _run_png(cmd: list[str], cancel: Event | None = None) -> bytes:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
                           creationflags=creationflags_no_window())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BuildError(f"미리보기를 만들 수 없습니다: {e}") from e
    if cancel is not None and cancel.is_set():
        from .core import BuildCancelled
        raise BuildCancelled("사용자가 작업을 중지했습니다.")
    if p.returncode != 0 or not p.stdout:
        err = p.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise BuildError("미리보기를 만들 수 없습니다.\n" + "\n".join(err))
    return p.stdout


def source_crop_png(ffmpeg: Path, input_args: list[str], pre: str, dw: int, dh: int, W: int, H: int,
                    box_h: int = PREVIEW_H, cancel: Event | None = None) -> bytes:
    """원본 전체(축소) 위에 '꽉 채우기에서 남는 영역'을 밝게, 잘리는 곳을 어둡게 표시한 PNG."""
    x, y, kw, kh = crop_box(dw, dh, W, H)
    ph = box_h
    pw = max(2, int(round(dw / dh * ph / 2)) * 2)
    bx, by, bw, bh = int(x * pw), int(y * ph), max(2, int(kw * pw)), max(2, int(kh * ph))
    chain = (f"{pre + ',' if pre else ''}scale={pw}:{ph}:flags=bicubic,setsar=1,split=2[s0][s1];"
             f"[s0]eq=brightness=-0.35:saturation=0.4[dim];[s1]crop={bw}:{bh}:{bx}:{by}[keep];"
             f"[dim][keep]overlay={bx}:{by},drawbox=x={bx}:y={by}:w={bw}:h={bh}:color=red@0.9:t=3,format=rgb24")
    cmd = [str(ffmpeg), "-hide_banner", "-v", "error", "-nostdin", *input_args,
           "-filter_complex", f"[0:v]{chain}[v]", "-map", "[v]", "-frames:v", "1", "-an",
           "-f", "image2pipe", "-c:v", "png", "pipe:1"]
    return _run_png(cmd, cancel)


__all__ = [
    "FIT_BLUR", "FIT_FILL", "FIT_CONTAIN", "FIT_CHOICES", "DEFAULT_FIT", "FIT_LABELS", "FIT_SHORT", "BLUR_LEVELS",
    "DEFAULT_BLUR", "BLUR_LABELS", "PAD_COLORS", "DEFAULT_PAD", "IMAGE_EXTS", "VIDEO_EXTS", "STILL_CHOICES",
    "DEFAULT_STILL", "MOTION_CHOICES", "MOTION_LABELS", "DEFAULT_MOTION", "MOTION_NONE", "MOTION_ZOOM_IN",
    "MOTION_ZOOM_OUT", "MOTION_PAN", "MediaMeta", "probe_media", "read_exif_orientation", "display_size",
    "aspect_label", "is_169", "fit_geometry", "native_height", "fit_notes", "fit_filter", "motion_filter",
    "crop_box", "source_crop_png", "parse_still", "clamp_still", "is_image_path", "STILL_TAG",
]
