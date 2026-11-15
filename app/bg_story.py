"""④ 주인공 이미지 합성 (통합 08) — 움직이는 배경 영상 위에 STORY 주인공(투명 PNG)을 겹친다.

'사진을 독립 장면으로 넣기'(bg_fit, 사진 = 한 장면)와 다른 기능이다. 여기서는 장면(영상·사진) 위에 인물 이미지를
2D로 겹친다 (FFmpeg overlay). 인물 이미지는 움직이지 않는다 — 표정·걷기·시선 변화는 만들지 않고,
크기·위치·투명도·페이드·아주 느린 확대/좌우 이동만 적용한다. 인물이 장면 뒤로 들어가거나 실제로 움직이는
AI 영상 합성은 이 기능이 아니다 (별도 고급 기능).

원칙
- 원본 비율 유지 (가로·세로를 따로 늘리지 않음), 알파 채널 보존 (premultiply → lanczos 축소 → unpremultiply로
  투명 가장자리의 검은 테두리·계단 현상을 줄이고, 가장자리 알파만 아주 살짝 흐림).
- 등장 시간은 '장면 안의 시간(clip-local)'으로만 정한다. 한 바퀴를 한 번 인코딩한 뒤 -c copy로 반복하므로
  반복할 때마다 같은 프레임이 나오고, 반복 경계·조각 경계에서 인물이 튀거나 처음부터 다시 시작하지 않는다.
- 기본 등장 구간은 장면 사이 디졸브 바깥(디졸브가 끝난 뒤 ~ 다음 디졸브 전)이라 디졸브 중에 인물이 갑자기
  나타나거나 잘리지 않는다. 앞뒤는 페이드로 천천히.
- Python은 영상 프레임을 디코딩하지 않는다. 인물 이미지의 투명 영역 확인도 FFmpeg가 만든 작은 알파 지도만 본다.
- 원본 인물 이미지는 읽기만 한다. 라이브러리는 사본을 설정 폴더(%APPDATA%\\PlaylistLongVideoMaker\\story_library)에 둔다.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Iterable, Sequence

from .core import BuildError, creationflags_no_window

# ---------------- 선택지 ----------------

ALIGN_LEFT, ALIGN_CENTER, ALIGN_RIGHT = "left", "center", "right"
ALIGNS = (ALIGN_LEFT, ALIGN_CENTER, ALIGN_RIGHT)
ALIGN_LABELS = {ALIGN_LEFT: "왼쪽", ALIGN_CENTER: "가운데", ALIGN_RIGHT: "오른쪽"}

SMOTION_NONE, SMOTION_ZOOM, SMOTION_DRIFT = "none", "zoom_in", "drift"
SMOTIONS = (SMOTION_NONE, SMOTION_ZOOM, SMOTION_DRIFT)
SMOTION_LABELS = {SMOTION_NONE: "움직임 없음", SMOTION_ZOOM: "아주 느린 확대", SMOTION_DRIFT: "아주 느린 좌우 이동"}
ZOOM_AMOUNT = 0.04        # 등장 구간 전체에서 4% 확대 (30초 → 초당 약 0.13%)
DRIFT_AMOUNT = 0.015      # 등장 구간 전체에서 화면 폭의 1.5% 이동 (FHD 약 29화소, 소수 화소 보간)

FEATHER_SIGMA = {0: 0.0, 1: 0.7, 2: 1.4, 3: 2.4}   # 가장자리 알파만 흐림 (출력 화소 기준)

APPLY_SELECTED, APPLY_ALL, APPLY_INTERVAL = "selected", "all", "interval"
APPLY_LABELS = {APPLY_SELECTED: "선택한 장면에만", APPLY_ALL: "모든 장면", APPLY_INTERVAL: "일정 간격 (N장면마다)"}
DEFAULT_APPLY = APPLY_ALL
DEFAULT_EVERY = 2

TAGS = ("Christmas", "Fireworks", "Night", "Cafe")

MIN_SHOW = 2.0            # 등장 구간이 이보다 짧으면 그 장면에는 넣지 않는다 (경고)
EDGE_GAP = 0.5            # 디졸브와 등장 구간 사이 여유 (초)

ALPHA_MAP_W = 128         # 투명 영역 확인용 알파 지도 폭 (FFmpeg가 축소)
ALPHA_ON = 24             # 이 값보다 큰 알파 = 보이는 화소
NEED_TRANSPARENT = 0.03   # 투명 화소가 이보다 적으면 '배경 제거 필요'

STORY_EXTS = (".png", ".webp")
OPAQUE_EXTS = (".jpg", ".jpeg", ".bmp", ".gif", ".heic", ".tif", ".tiff")

BG_REMOVAL_NOTE = ("JPG처럼 배경이 있는 이미지는 그대로 겹치면 사각형 배경까지 함께 보입니다. 배경 제거가 필요합니다 — "
                   "CapCut [배경 제거], 휴대폰 사진 앱의 '피사체 분리', 이미지 편집 프로그램 등으로 인물만 남긴 "
                   "투명 PNG를 만들어 다시 선택하세요. (이 프로그램에는 검증된 자동 배경 제거 모델이 포함되어 있지 않습니다.)")
STATIC_NOTE = ("인물 이미지는 그대로 겹쳐집니다 — 표정·걷기·시선은 바뀌지 않으며, 크기·위치·투명도·페이드와 "
               "아주 느린 확대/좌우 이동만 적용됩니다 (2D 합성).")


class StoryImageError(ValueError):
    """화면에 그대로 보여 줄 수 있는 인물 이미지 문제 (배경 제거 필요 등)."""


# ---------------- 스타일 · 프리셋 ----------------

@dataclass(frozen=True)
class StoryStyle:
    """인물 크기·위치·시간·색 — 프리셋으로 저장·불러오기 하는 값 (이미지와 무관)."""
    size: float = 0.62          # 인물(보이는 부분) 높이 / 화면 높이
    align: str = ALIGN_RIGHT
    margin: float = 0.06        # 좌·우 정렬 시 화면 가장자리와의 거리 (화면 폭 비율)
    lift: float = 0.0           # 화면 하단 기준 높이 (+ 위로, - 아래로 내려 하단에서 잘림)
    opacity: float = 1.0
    fade: float = 1.5           # 페이드인·아웃 (초)
    duration: float = 30.0      # 한 장면에서 등장하는 시간 (초) — 장면이 짧으면 장면 길이에 맞춤
    delay: float = 1.0          # 장면 시작 디졸브가 끝난 뒤 등장까지 (초)
    motion: str = SMOTION_NONE
    brightness: float = 1.0
    warmth: float = 0.0         # + 따뜻하게(붉게) / - 차갑게(푸르게)
    saturation: float = 1.0
    feather: int = 1            # 가장자리 부드럽게 0~3
    protect_top: float = 0.0    # 화면 위쪽 이 비율은 비워 둠 (불꽃 공간) — 인물이 들어가면 경고

    def clamped(self) -> "StoryStyle":
        def f(v, lo, hi, d):
            try:
                x = float(v)
            except (TypeError, ValueError):
                return d
            return d if not math.isfinite(x) else max(lo, min(hi, x))
        base = StoryStyle()
        try:
            feather = int(self.feather)
        except (TypeError, ValueError):
            feather = base.feather
        return StoryStyle(
            size=f(self.size, 0.10, 1.20, base.size),
            align=self.align if self.align in ALIGNS else base.align,
            margin=f(self.margin, 0.0, 0.40, base.margin),
            lift=f(self.lift, -0.60, 0.60, base.lift),
            opacity=f(self.opacity, 0.10, 1.0, base.opacity),
            fade=f(self.fade, 0.0, 5.0, base.fade),
            duration=f(self.duration, 3.0, 600.0, base.duration),
            delay=f(self.delay, 0.0, 120.0, base.delay),
            motion=self.motion if self.motion in SMOTIONS else base.motion,
            brightness=f(self.brightness, 0.3, 1.6, base.brightness),
            warmth=f(self.warmth, -0.3, 0.3, base.warmth),
            saturation=f(self.saturation, 0.0, 1.6, base.saturation),
            feather=max(0, min(3, feather)),
            protect_top=f(self.protect_top, 0.0, 0.9, base.protect_top),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d) -> "StoryStyle":
        """알 수 없는 키는 무시, 없는 키는 기본값, 잘못된 값은 범위 안으로 (예전·이후 버전 설정과 호환)."""
        if not isinstance(d, dict):
            return StoryStyle()
        names = {f.name for f in fields(StoryStyle)}
        return StoryStyle(**{k: v for k, v in d.items() if k in names}).clamped()


@dataclass(frozen=True)
class StoryPreset:
    key: str
    name: str
    scene: str                 # 태그 (Christmas / Fireworks / Night / Cafe)
    desc: str
    style: StoryStyle
    builtin: bool = False


BUILTIN_PRESETS: tuple[StoryPreset, ...] = (
    StoryPreset("christmas_couple", "Christmas Couple", "Christmas",
                "크리스마스 거리 — 인물을 오른쪽(또는 왼쪽)에 두어 거리 풍경과 불빛이 보이게. 부드러운 페이드.",
                StoryStyle(size=0.62, align=ALIGN_RIGHT, margin=0.06, lift=0.0, fade=1.5, duration=30.0, delay=1.0,
                           motion=SMOTION_NONE, brightness=0.95, warmth=0.06, saturation=0.95, feather=1), True),
    StoryPreset("fireworks_couple", "Fireworks Couple", "Fireworks",
                "불꽃축제 — 화면 하단 가운데에 인물 뒷모습(상반신). 위쪽 하늘(불꽃 공간)은 비워 둡니다.",
                StoryStyle(size=0.46, align=ALIGN_CENTER, margin=0.0, lift=-0.12, fade=2.0, duration=30.0, delay=1.5,
                           motion=SMOTION_NONE, brightness=0.72, warmth=0.03, saturation=0.85, feather=1,
                           protect_top=0.55), True),
    StoryPreset("tokyo_night", "Tokyo Night Story", "Night",
                "도쿄 야경 — 왼쪽 옆 인물, 아주 느린 확대와 긴 페이드. 야경에 맞게 조금 어둡고 차갑게.",
                StoryStyle(size=0.66, align=ALIGN_LEFT, margin=0.05, lift=0.0, fade=2.0, duration=35.0, delay=1.5,
                           motion=SMOTION_ZOOM, brightness=0.85, warmth=-0.04, saturation=0.9, feather=1), True),
    StoryPreset("cafe_story", "Cafe Story", "Cafe",
                "카페 — 왼쪽 아래 상반신, 따뜻한 색감, 움직임 없음.",
                StoryStyle(size=0.58, align=ALIGN_LEFT, margin=0.07, lift=-0.05, fade=1.5, duration=30.0, delay=1.0,
                           motion=SMOTION_NONE, brightness=0.97, warmth=0.05, saturation=0.95, feather=1), True),
)
DEFAULT_PRESET = BUILTIN_PRESETS[0].key


# ---------------- 인물 이미지 ----------------

@dataclass(frozen=True)
class StoryImage:
    path: Path
    width: int
    height: int
    crop: tuple[int, int, int, int]   # 보이는(불투명) 영역 x, y, w, h — 투명 여백은 크기 계산에서 뺀다
    name: str = ""
    transparent: float = 0.0          # 투명 화소 비율 (참고)


@dataclass(frozen=True)
class StoryLayer:
    """한 장면에 겹칠 인물 1명(이미지 1장) + 스타일."""
    image: StoryImage
    style: StoryStyle
    label: str = ""                   # 화면 표시용 (프리셋 이름)


@dataclass(frozen=True)
class StoryWindow:
    """장면 안 시간(초, 사용 구간 시작 = 0) 기준 등장 구간."""
    start: float
    end: float
    fade: float


def _run(cmd: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                              creationflags=creationflags_no_window())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BuildError(f"FFmpeg를 실행할 수 없습니다: {e}") from e


def image_input_args(path: Path) -> list[str]:
    """인물 이미지 입력 (1장만 읽고 필터의 loop로 반복 — 매 프레임 다시 디코딩하지 않음). 이름의 %는 패턴이 아님."""
    return ["-f", "image2", "-pattern_type", "none", "-i", str(path)]


def inspect_image(ffmpeg: Path, ffprobe: Path, path: Path, name: str = "") -> StoryImage:
    """투명 PNG(RGBA)인지 확인하고 보이는 영역을 찾는다. 배경이 있으면 StoryImageError(배경 제거 안내).

    FFmpeg가 만든 128화소 폭 알파 지도만 Python으로 본다 (이미지 디코딩은 FFmpeg 안에서만)."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in OPAQUE_EXTS:
        raise StoryImageError(f"'{path.name}'은(는) {ext.lstrip('.').upper()} 이미지라 투명 배경이 없습니다.\n\n"
                              + BG_REMOVAL_NOTE)
    if ext not in STORY_EXTS:
        raise StoryImageError(f"'{path.name}': 주인공 이미지는 투명 PNG(또는 투명 WebP)만 쓸 수 있습니다.")
    if not path.is_file():
        raise StoryImageError(f"파일이 없습니다: {path}")
    p = _run([str(ffprobe), "-v", "error", "-select_streams", "v:0", "-show_entries",
              "stream=codec_name,width,height,pix_fmt", "-of", "json", str(path)])
    try:
        st = (json.loads(p.stdout.decode("utf-8", "replace") or "{}").get("streams") or [{}])[0]
        w, h = int(st.get("width") or 0), int(st.get("height") or 0)
    except (ValueError, TypeError, IndexError):
        w = h = 0
    if p.returncode != 0 or w < 16 or h < 16:
        raise StoryImageError(f"이미지를 읽을 수 없습니다: {path.name}")
    mw = min(ALPHA_MAP_W, w)
    mh = max(1, int(round(h * mw / w)))
    q = _run([str(ffmpeg), "-hide_banner", "-v", "error", "-nostdin", *image_input_args(path), "-vf",
              f"format=rgba,alphaextract,scale={mw}:{mh}:flags=area,format=gray", "-frames:v", "1",
              "-f", "rawvideo", "-"])
    amap = q.stdout[:mw * mh] if q.returncode == 0 else b""
    if len(amap) < mw * mh:
        raise StoryImageError(f"'{path.name}'의 투명 정보를 읽을 수 없습니다.\n\n" + BG_REMOVAL_NOTE)
    on = [v > ALPHA_ON for v in amap]
    transparent = 1.0 - sum(on) / len(on)
    if transparent < NEED_TRANSPARENT:
        raise StoryImageError(f"'{path.name}'에는 투명한 배경이 거의 없습니다 (투명 영역 {transparent:.0%}).\n\n"
                              + BG_REMOVAL_NOTE)
    if sum(on) < max(4, len(on) * 0.005):
        raise StoryImageError(f"'{path.name}'은(는) 거의 전부 투명해서 보이는 인물이 없습니다.")
    xs = [i % mw for i, v in enumerate(on) if v]
    ys = [i // mw for i, v in enumerate(on) if v]
    sx, sy = w / mw, h / mh                       # 지도 1칸 = 원본 sx×sy 화소 — 바깥쪽으로 1칸 여유
    x0 = max(0, int(math.floor((min(xs) - 1) * sx)))
    y0 = max(0, int(math.floor((min(ys) - 1) * sy)))
    x1 = min(w, int(math.ceil((max(xs) + 2) * sx)))
    y1 = min(h, int(math.ceil((max(ys) + 2) * sy)))
    return StoryImage(path.resolve(), w, h, (x0, y0, max(2, x1 - x0), max(2, y1 - y0)), name or path.stem,
                      round(transparent, 4))


def thumb_png(ffmpeg: Path, image: StoryImage, box: tuple[int, int] = (150, 190)) -> bytes:
    """라이브러리 미리보기: 보이는 영역을 회색 바탕 위에 (투명한 곳 = 회색)."""
    bw, bh = box
    x, y, cw, ch = image.crop
    graph = (f"[0:v]format=rgba,crop={cw}:{ch}:{x}:{y},scale={bw}:{bh}:force_original_aspect_ratio=decrease:"
             f"flags=lanczos[c];color=c=0x8a8a8a:s={bw}x{bh}:d=1,format=rgba[b];"
             f"[b][c]overlay=x=(W-w)/2:y=(H-h)/2:format=auto,format=rgb24[v]")
    p = _run([str(ffmpeg), "-hide_banner", "-v", "error", "-nostdin", *image_input_args(image.path),
              "-filter_complex", graph, "-map", "[v]", "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "pipe:1"])
    if p.returncode != 0 or not p.stdout:
        raise BuildError("인물 이미지 미리보기를 만들 수 없습니다.\n" + p.stderr.decode("utf-8", "replace")[-300:])
    return p.stdout


# ---------------- 배치 · 시간 ----------------

@dataclass(frozen=True)
class Placement:
    x: int        # 인물 왼쪽 위 (출력 화소, 화면 밖이면 음수/초과 가능)
    y: int
    w: int
    h: int

    @property
    def visible_top(self) -> int:
        return max(0, self.y)


def placement(layer: StoryLayer, W: int, H: int) -> Placement:
    """출력 W×H에서의 인물 크기·위치 (원본 비율 유지). 화면 비율로 정하므로 미리보기 해상도에서도 같은 구도."""
    s = layer.style
    _, _, cw, ch = layer.image.crop
    h = max(2, int(round(H * s.size)))
    w = max(2, int(round(h * cw / ch)))
    if w > W * 0.95:                       # 아주 넓은 이미지는 화면 폭 안으로 (비율 유지)
        w = max(2, int(W * 0.95))
        h = max(2, int(round(w * ch / cw)))
    m = int(round(W * s.margin))
    if s.align == ALIGN_LEFT:
        x = m
    elif s.align == ALIGN_RIGHT:
        x = W - w - m
    else:
        x = (W - w) // 2
    y = H - h - int(round(H * s.lift))
    y = min(y, H - max(2, h // 5))         # 아무리 내려도 인물 위쪽 1/5은 보이게
    return Placement(x, y, w, h)


def story_window(style: StoryStyle, scene_len: float, dissolve: float) -> StoryWindow | None:
    """장면(사용 구간 길이 scene_len, 프레임 맞춤 값)에서 인물이 보이는 구간. 너무 짧으면 None.

    시작: 앞 디졸브가 끝난 뒤 + delay. 끝: 다음 디졸브 시작 EDGE_GAP초 전까지, 최대 duration초.
    → 디졸브 조각에는 인물이 없으므로 장면 전환 중 인물이 갑자기 나타나거나 잘리지 않는다."""
    s = style
    start = dissolve + max(s.delay, EDGE_GAP)
    latest = scene_len - dissolve - EDGE_GAP
    if latest - start < MIN_SHOW:          # 짧은 장면: 지연을 줄여 앞쪽부터
        start = dissolve + EDGE_GAP
    end = min(start + s.duration, latest)
    show = end - start
    if show < MIN_SHOW:
        return None
    fade = min(s.fade, show / 3)
    return StoryWindow(round(start, 6), round(end, 6), round(fade, 6))


def window_overlaps(w: StoryWindow | None, local0: float, length: float) -> bool:
    return w is not None and local0 < w.end and local0 + length > w.start


def color_matrix(brightness: float, warmth: float, saturation: float) -> list[list[float]]:
    lr, lg, lb = 0.2126, 0.7152, 0.0722
    s = saturation
    sat = [[lr + (1 - lr) * s, lg * (1 - s), lb * (1 - s)],
           [lr * (1 - s), lg + (1 - lg) * s, lb * (1 - s)],
           [lr * (1 - s), lg * (1 - s), lb + (1 - lb) * s]]
    tint = (1 + warmth, 1.0, 1 - warmth)
    return [[brightness * tint[i] * sat[i][j] for j in range(3)] for i in range(3)]


def _mixer(style: StoryStyle) -> str:
    m = color_matrix(style.brightness, style.warmth, style.saturation)
    ident = all(abs(m[i][j] - (1.0 if i == j else 0.0)) < 1e-4 for i in range(3) for j in range(3))
    if ident and style.opacity >= 0.999:
        return ""
    names = (("rr", "rg", "rb"), ("gr", "gg", "gb"), ("br", "bg", "bb"))
    parts = [f"{names[i][j]}={m[i][j]:.5f}" for i in range(3) for j in range(3)]
    return "colorchannelmixer=" + ":".join(parts) + f":aa={style.opacity:.4f}"


def overlay_graph(layer: StoryLayer, W: int, H: int, fps: float, fps_text: str, local0: float, length: float,
                  window: StoryWindow, src: str, tag: str) -> tuple[str, str]:
    """(인물 스트림 그래프 → [{tag}c], overlay 필터). local0: 이 조각 첫 프레임의 장면 안 시간.

    시간은 장면 안 시간으로 맞춘다 (setpts = N/FPS + local0) → 조각이 나뉘거나 반복돼도 페이드·확대가 이어진다."""
    s = layer.style
    pl = placement(layer, W, H)
    x, y, cw, ch = layer.image.crop
    chain = [f"{src}format=rgba,crop={cw}:{ch}:{x}:{y}",
             f"premultiply=inplace=1,scale={pl.w}:{pl.h}:flags=lanczos,unpremultiply=inplace=1,format=rgba"]
    pre = ",".join(chain)
    sigma = FEATHER_SIGMA.get(int(s.feather), 0.0)
    feather = (f",split=2[{tag}s0][{tag}s1];[{tag}s1]alphaextract,gblur=sigma={sigma:g}[{tag}m];"
               f"[{tag}s0][{tag}m]alphamerge" if sigma > 0 else "")
    mix = _mixer(s)
    timed = (f"loop=loop=-1:size=1:start=0,settb=AVTB,setpts=(N/({fps_text})+{local0:.6f})/TB")
    fd = max(window.fade, 1.0 / fps)       # 페이드 0이어도 프레임을 버리지 않고 알파만 (조각 길이 그대로)
    timed += (f",fade=t=in:st={window.start:.6f}:d={fd:.6f}:alpha=1"
              f",fade=t=out:st={window.end - fd:.6f}:d={fd:.6f}:alpha=1")
    graph = pre + feather + (f",{mix}" if mix else "") + f",{timed}"
    if s.motion in (SMOTION_ZOOM, SMOTION_DRIFT):
        vis_h = min(pl.h, H - pl.y)
        if vis_h < pl.h:                   # 화면 아래로 내려간 부분은 미리 잘라 둔다 (확대 기준점 = 화면 하단)
            graph += f",crop={pl.w}:{vis_h}:0:0"
        # 화면 전체가 아니라 인물 + 확대·이동 여유만큼의 작은 투명 캔버스에서만 perspective (FHD 전체 대비 수 배 빠름)
        grow = ZOOM_AMOUNT if s.motion == SMOTION_ZOOM else 0.0
        shift = DRIFT_AMOUNT * W if s.motion == SMOTION_DRIFT else 0.0
        mx = int(math.ceil(pl.w * grow / 2 + shift)) + 4
        my = int(math.ceil(vis_h * grow)) + 4
        cw_c, ch_c = pl.w + 2 * mx, vis_h + my
        graph += f",pad={cw_c}:{ch_c}:{mx}:{my}:color=black@0"
        graph += "," + _perspective(s, W, fps, local0, window, mx + pl.w / 2, float(ch_c))
        ov = f"overlay=x={pl.x - mx}:y={pl.y - my}:format=yuv444:shortest=1"
    else:
        ov = f"overlay=x={pl.x}:y={pl.y}:format=yuv444:shortest=1"
    graph += f",setpts=PTS-STARTPTS[{tag}c]"
    return graph, ov


def _perspective(s: StoryStyle, W: int, fps: float, local0: float, w: StoryWindow, ax: float, ay: float) -> str:
    """인물 기준점(발 아래 가운데, 캔버스 좌표)을 중심으로 아주 느린 확대 또는 좌우 이동 — perspective의 소수 화소
    보간이라 정수 화소 단위로 '덜컥'이지 않는다. 진행률은 장면 안 시간 기준 (조각 경계에서 이어짐).
    W: 출력 화면 폭 (이동 거리 계산용). 식 안의 W·H는 perspective 입력(작은 캔버스) 크기."""
    span = max(1e-3, w.end - w.start)
    p = f"min(1,max(0,(in/{fps:.6f}+{local0:.6f}-{w.start:.6f})/{span:.6f}))"
    if s.motion == SMOTION_ZOOM:
        z = f"(1+{ZOOM_AMOUNT:.5f}*{p})"
        dx = "0"
    else:
        z = "1"
        sign = -1 if s.align == ALIGN_RIGHT else 1          # 화면 가운데 쪽으로
        dx = f"({sign * DRIFT_AMOUNT * W:.4f}*{p})"
    # 출력 X ← 원본 ax + (X-ax)/z - dx
    def sx(X):
        return f"({ax:.3f}+({X}-{ax:.3f})/{z}-{dx})"

    def sy(Y):
        return f"({ay:.3f}+({Y}-{ay:.3f})/{z})"
    x0, x1, y0, y2 = sx(0), sx("W"), sy(0), sy("H")
    return (f"perspective=x0='{x0}':y0='{y0}':x1='{x1}':y1='{y0}':x2='{x0}':y2='{y2}':x3='{x1}':y3='{y2}'"
            f":interpolation=cubic:eval=frame")


# ---------------- 장면 지정 ----------------

def assign_story(clips: Sequence, layer: StoryLayer | None, mode: str = DEFAULT_APPLY,
                 selected: Iterable[int] = (), every: int = DEFAULT_EVERY) -> list:
    """새 Clip 목록 (원본 목록은 바꾸지 않음). 제외한 장면은 그대로 둔다.

    selected: 목록 위치(0부터) — '선택한 장면에만'. interval: 사용 중인 장면 기준 1·(1+N)·(1+2N)… 번째."""
    sel = set(selected)
    out = []
    k = 0
    every = max(1, int(every))
    for i, c in enumerate(clips):
        on = False
        if layer is not None and c.enabled:
            if mode == APPLY_ALL:
                on = True
            elif mode == APPLY_SELECTED:
                on = i in sel
            elif mode == APPLY_INTERVAL:
                on = k % every == 0
        if c.enabled:
            k += 1
        out.append(replace(c, story=layer if on else None) if layer is not None or c.story is not None else c)
    return out


def scene_len(c, fps: float) -> float:
    """build_bg_plan과 같은 프레임 맞춤 장면 길이."""
    return int(math.floor(c.used_length * fps + 1e-6)) / fps


def frame_dissolve(dissolve: float, fps: float) -> float:
    return max(1, int(round(dissolve * fps))) / fps


def clip_window(c, dissolve: float, fps: float) -> StoryWindow | None:
    if getattr(c, "story", None) is None:
        return None
    return story_window(c.story.style, scene_len(c, fps), frame_dissolve(dissolve, fps))


def story_notes(clips: Sequence, dissolve: float, fps: float, W: int = 1920, H: int = 1080) -> list[str]:
    """제작 계획 경고: 짧아서 빠지는 장면, 반복 경계 연속 등장, 불꽃 공간 침범, 너무 큰 인물."""
    active = [c for c in clips if c.enabled]
    on = [c for c in active if getattr(c, "story", None) is not None]
    if not on:
        return []
    notes = [f"👤 주인공 이미지: {len(on)}/{len(active)}개 장면에 겹칩니다. {STATIC_NOTE}"]
    skipped = [c.path.name for c in on if clip_window(c, dissolve, fps) is None]
    if skipped:
        notes.append(f"장면이 짧아 주인공을 넣지 못하는 장면: {', '.join(skipped[:4])}"
                     + (f" 외 {len(skipped) - 4}개" if len(skipped) > 4 else "")
                     + f" (디졸브 바깥 등장 시간이 {MIN_SHOW:g}초 미만).")
    if len(active) >= 2 and active[0].story is not None and active[-1].story is not None and len(on) < len(active):
        notes.append("마지막 장면과 첫 장면에 연달아 주인공이 나옵니다 (반복 경계). 페이드로 사라졌다가 다시 나타나므로 "
                     "튀지는 않지만, 간격을 일정하게 하려면 장면 수가 간격의 배수가 되도록 하세요.")
    for layer in {c.story for c in on}:
        pl = placement(layer, W, H)
        st = layer.style
        if st.protect_top > 0 and pl.visible_top < H * st.protect_top:
            notes.append(f"'{layer.label or layer.image.name}': 인물이 화면 위쪽 {st.protect_top:.0%} (불꽃 공간)까지 "
                         "올라옵니다. 크기를 줄이거나 하단 높이를 낮추세요.")
        if pl.w * min(pl.h, H - pl.visible_top) > W * H * 0.30:
            notes.append(f"'{layer.label or layer.image.name}': 인물이 화면의 30% 이상을 가립니다. 풍경이 잘 보이도록 "
                         "크기를 줄이거나 한쪽으로 옮기세요.")
    return notes


# ---------------- 프리셋 · 라이브러리 저장 (설정 폴더, 원자적 저장, 손상 시 보존) ----------------

PRESETS_FILE = "story_presets.json"
LIBRARY_DIR = "story_library"
LIBRARY_FILE = "library.json"


def _settings_dir() -> Path:
    from . import settings as S
    return S.settings_dir()


def _read_json(path: Path, default):
    """읽기 실패·손상: 기존 파일은 지우지 않고 '.broken-시각'으로 이름만 바꿔 보존한 뒤 기본값."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        try:
            path.replace(path.with_name(f"{path.name}.broken-{time.strftime('%Y%m%d-%H%M%S')}"))
        except OSError:
            pass
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_presets() -> list[StoryPreset]:
    """기본 프리셋(코드, 수정 불가) + 사용자 프리셋(JSON)."""
    data = _read_json(_settings_dir() / PRESETS_FILE, {})
    out = list(BUILTIN_PRESETS)
    items = data.get("presets") if isinstance(data, dict) else None
    for d in items if isinstance(items, list) else []:
        if not isinstance(d, dict) or not str(d.get("key", "")).startswith("user_"):
            continue
        out.append(StoryPreset(str(d["key"]), str(d.get("name") or "내 프리셋")[:60], str(d.get("scene") or "")[:20],
                               str(d.get("desc") or "")[:300], StoryStyle.from_dict(d.get("style"))))
    return out


def _save_user_presets(presets: Sequence[StoryPreset]) -> None:
    _write_json(_settings_dir() / PRESETS_FILE, {"version": 1, "presets": [
        {"key": p.key, "name": p.name, "scene": p.scene, "desc": p.desc, "style": p.style.to_dict()}
        for p in presets if not p.builtin]})


def save_preset(name: str, style: StoryStyle, scene: str = "", desc: str = "", key: str | None = None) -> StoryPreset:
    """사용자 프리셋 추가(key 없음) 또는 수정(user_ key). 기본 프리셋은 바꾸지 않는다 — 복제해서 수정."""
    presets = load_presets()
    if key and not key.startswith("user_"):
        raise ValueError("기본 프리셋은 바꿀 수 없습니다. [복제]한 뒤 수정하세요.")
    p = StoryPreset(key or f"user_{uuid.uuid4().hex[:10]}", (name or "내 프리셋").strip()[:60], scene[:20], desc[:300],
                    style.clamped())
    user = [x for x in presets if not x.builtin and x.key != p.key]
    idx = next((i for i, x in enumerate(presets) if x.key == p.key and not x.builtin), None)
    if idx is not None:
        user.insert(min(idx - len(BUILTIN_PRESETS), len(user)), p)
    else:
        user.append(p)
    _save_user_presets(user)
    return p


def duplicate_preset(key: str) -> StoryPreset:
    src = next((p for p in load_presets() if p.key == key), None)
    if src is None:
        raise ValueError("프리셋을 찾을 수 없습니다.")
    return save_preset(f"{src.name} (복사)", src.style, src.scene, src.desc)


def delete_preset(key: str) -> bool:
    presets = load_presets()
    if not key.startswith("user_") or all(p.key != key for p in presets):
        return False
    _save_user_presets([p for p in presets if not p.builtin and p.key != key])
    return True


def get_preset(key: str | None) -> StoryPreset:
    presets = load_presets()
    return next((p for p in presets if p.key == key), presets[0])


@dataclass
class LibraryEntry:
    id: str
    name: str
    file: str                     # 라이브러리 폴더 안 사본 파일명
    source: str = ""              # 처음 등록한 원본 경로 (참고용 — 원본은 건드리지 않음)
    tags: list[str] = field(default_factory=list)
    width: int = 0
    height: int = 0
    crop: tuple[int, int, int, int] = (0, 0, 0, 0)
    transparent: float = 0.0
    style: dict | None = None     # 자주 쓰는 위치·크기 (이 이미지의 기본값)
    preset: str = ""
    added: str = ""

    def image(self, folder: Path) -> StoryImage:
        return StoryImage((folder / self.file).resolve(), self.width, self.height, tuple(self.crop), self.name,
                          self.transparent)


class StoryLibrary:
    """내 주인공 이미지 목록 — 사본과 library.json은 설정 폴더에. 프로그램 업데이트와 무관하게 유지되며,
    목록에서 빼도 사본 파일은 지우지 않는다."""

    def __init__(self, folder: Path | None = None):
        self.folder = Path(folder) if folder else _settings_dir() / LIBRARY_DIR
        self.entries: list[LibraryEntry] = []
        self.load()

    @property
    def index(self) -> Path:
        return self.folder / LIBRARY_FILE

    def load(self) -> None:
        data = _read_json(self.index, {})
        items = data.get("images") if isinstance(data, dict) else None
        self.entries = []
        names = {f.name for f in fields(LibraryEntry)}
        for d in items if isinstance(items, list) else []:
            if not isinstance(d, dict) or not d.get("id") or not d.get("file"):
                continue
            try:
                e = LibraryEntry(**{k: v for k, v in d.items() if k in names})
                e.crop = tuple(int(v) for v in e.crop)[:4] if len(e.crop) == 4 else (0, 0, e.width, e.height)
                e.tags = [t for t in e.tags if isinstance(t, str)][:8]
                e.width, e.height = int(e.width), int(e.height)
            except (TypeError, ValueError):
                continue
            if Path(e.file).name != e.file:     # 라이브러리 폴더 밖 경로는 쓰지 않음
                continue
            self.entries.append(e)

    def save(self) -> None:
        _write_json(self.index, {"version": 1, "images": [
            {**asdict(e), "crop": list(e.crop)} for e in self.entries]})

    def get(self, entry_id: str) -> LibraryEntry | None:
        return next((e for e in self.entries if e.id == entry_id), None)

    def add(self, image: StoryImage, name: str = "", tags: Sequence[str] = ()) -> LibraryEntry:
        """원본은 읽기만 하고 사본을 라이브러리 폴더에 만든다 (같은 원본도 사본은 따로)."""
        self.folder.mkdir(parents=True, exist_ok=True)
        eid = uuid.uuid4().hex[:12]
        fname = f"{eid}{image.path.suffix.lower()}"
        dst = self.folder / fname
        tmp = self.folder / f"{fname}.part"
        shutil.copyfile(image.path, tmp)
        os.replace(tmp, dst)
        e = LibraryEntry(eid, (name or image.name or image.path.stem).strip()[:60], fname, str(image.path),
                         [t for t in tags if t in TAGS], image.width, image.height, tuple(image.crop),
                         image.transparent, None, "", time.strftime("%Y-%m-%d %H:%M"))
        self.entries.append(e)
        self.save()
        return e

    def update(self, entry_id: str, *, name: str | None = None, tags: Sequence[str] | None = None,
               style: StoryStyle | None = None, preset: str | None = None) -> LibraryEntry:
        e = self.get(entry_id)
        if e is None:
            raise ValueError("목록에 없는 이미지입니다.")
        if name is not None and name.strip():
            e.name = name.strip()[:60]
        if tags is not None:
            e.tags = [t for t in tags if t in TAGS]
        if style is not None:
            e.style = style.clamped().to_dict()
        if preset is not None:
            e.preset = preset
        self.save()
        return e

    def remove(self, entry_id: str) -> bool:
        """목록에서만 뺀다 (사본 파일은 남김 — 실수로 지워도 폴더에서 다시 등록 가능)."""
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.id != entry_id]
        if len(self.entries) != before:
            self.save()
            return True
        return False

    def image_of(self, entry: LibraryEntry) -> StoryImage:
        return entry.image(self.folder)

    def available(self, entry: LibraryEntry) -> bool:
        return (self.folder / entry.file).is_file()


def layer_for(entry_image: StoryImage, preset: StoryPreset, style: StoryStyle | None = None) -> StoryLayer:
    return StoryLayer(entry_image, (style or preset.style).clamped(), preset.name)


__all__ = [
    "ALIGNS", "ALIGN_LABELS", "SMOTIONS", "SMOTION_LABELS", "APPLY_LABELS", "TAGS", "StoryStyle", "StoryPreset",
    "BUILTIN_PRESETS", "StoryImage", "StoryLayer", "StoryWindow", "StoryImageError", "inspect_image", "thumb_png",
    "placement", "story_window", "overlay_graph", "assign_story", "clip_window", "story_notes", "load_presets",
    "save_preset", "duplicate_preset", "delete_preset", "get_preset", "StoryLibrary", "LibraryEntry", "layer_for",
    "BG_REMOVAL_NOTE", "STATIC_NOTE", "image_input_args",
]
