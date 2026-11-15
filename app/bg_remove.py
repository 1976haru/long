"""④ 주인공 사진 배경 제거 (통합 08 UX HOTFIX) — 로컬 모델이 있을 때만 쓰는 선택 기능.

검토 결과 (2026-11-15):
- 후보: rembg(MIT) + U²-Net 모델(Apache-2.0, 약 176 MB) 또는 u2netp(약 4.7 MB, 경계 품질 낮음), onnxruntime 필요.
- 이 PC에는 rembg·onnxruntime·모델 파일이 없고, 기존 EXE에 대형 모델을 무단으로 넣지 않는다는 원칙,
  첫 실행 시 모델을 인터넷에서 받아 오는 rembg 기본 동작(사용자 승인 없는 다운로드)을 고려해 **기본 탑재하지 않는다**.
- 그래서 detect()는 검증된 제거기가 등록되어 있을 때만 그것을 돌려주고, 없으면 None — 화면은 [배경 제거하기]를
  작동하는 것처럼 보이지 않게 비활성 + 이유를 보여 준다. [사진 그대로 넣기]는 항상 동작한다.

처리 규칙 (제거기가 있을 때): 원본은 읽기만 → 결과는 지정 폴더에 '.part' → 검사 통과 후 '.png'로 이름 변경.
외부 서버로 사진을 보내지 않는다 (로컬 제거기만 등록 가능). 진행률·취소 지원. 경계 품질 검사 실패 시 결과를 지운다.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable

from .bg_story import KIND_CUTOUT, StoryImage, StoryImageError, inspect_image
from .core import BuildCancelled, BuildError

UNAVAILABLE_NOTE = ("이 PC에서는 아직 자동 배경 제거를 쓸 수 없습니다 (검증된 로컬 배경 제거 모델이 설치되어 있지 않음). "
                    "[사진 그대로 넣기]를 쓰거나, CapCut [배경 제거]·휴대폰 '피사체 분리'로 배경을 지운 PNG를 "
                    "[다른 사진 선택]으로 고르세요. 사진을 외부 서버로 보내지 않습니다.")
MIN_VISIBLE = 0.05        # 결과에서 인물이 화면의 5% 이상 남아야 함
MAX_VISIBLE = 0.95        # 95% 넘게 남으면 배경이 지워지지 않은 것


@dataclass(frozen=True)
class Remover:
    """로컬 배경 제거기. run(src, dst_png, cancel, progress) — dst_png에 RGBA PNG를 쓴다 (외부 전송 금지)."""
    name: str
    source: str
    license: str
    size_mb: float
    run: Callable[[Path, Path, Event, Callable[[float], None]], None]


_REGISTERED: Remover | None = None


def register(remover: Remover | None) -> None:
    """검증된 로컬 제거기를 등록 (설치·승인 절차를 거친 뒤에만). None = 해제."""
    global _REGISTERED
    _REGISTERED = remover


def detect() -> Remover | None:
    """쓸 수 있는 제거기. 이번 배포에는 기본 제거기가 없으므로 등록된 것이 없으면 None."""
    return _REGISTERED


def remove_background(remover: Remover, ffmpeg: Path, ffprobe: Path, src: Path, out_dir: Path, cancel: Event,
                      progress: Callable[[float], None] | None = None) -> StoryImage:
    """원본 사진 → 투명 PNG 사본 (out_dir). 원본은 읽기만 한다. 경계 품질 검사를 통과해야 결과를 남긴다."""
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"{src.stem}_배경제거_{uuid.uuid4().hex[:6]}.png"
    part = final.with_name(final.name + ".part.png")
    prog = progress or (lambda f: None)
    try:
        prog(0.0)
        remover.run(src, part, cancel, prog)
        if cancel.is_set():
            raise BuildCancelled("배경 제거를 취소했습니다.")
        if not part.exists() or part.stat().st_size == 0:
            raise BuildError("배경 제거 결과가 만들어지지 않았습니다.")
        img = inspect_image(ffmpeg, ffprobe, part)
        visible = 1.0 - img.transparent
        if img.kind != KIND_CUTOUT or not (MIN_VISIBLE <= visible <= MAX_VISIBLE):
            raise BuildError(f"배경이 제대로 지워지지 않았습니다 (남은 부분 {visible:.0%}). 원본은 그대로입니다. "
                             "[사진 그대로 넣기]를 쓰거나 다른 사진을 고르세요.")
        os.replace(part, final)
        prog(1.0)
        return inspect_image(ffmpeg, ffprobe, final, src.stem)
    except StoryImageError as e:
        raise BuildError(f"배경 제거 결과를 확인할 수 없습니다: {e}") from e
    finally:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass


__all__ = ["Remover", "register", "detect", "remove_background", "UNAVAILABLE_NOTE"]
