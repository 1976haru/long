"""Playlist 일괄 LIVE READY 변환 — [문제 영상 모두 LIVE READY로 만들기] / [선택 영상 LIVE READY로 만들기].

- 변환 자체는 live_ready.make_live_ready_file()을 그대로 쓴다 (중복 FFmpeg 구현 없음, .part.mp4 → 검증 → 이름 교체).
- 원본 파일은 수정/삭제하지 않는다. 결과는 원본 옆 *_LIVE_READY.mp4.
- 한 번에 하나씩 순차 변환 (FFmpeg 동시 실행 1개), 취소 지원. 사용자가 버튼을 눌렀을 때만 변환한다.
- 출력 규격: H.264 yuv420p · 원본 해상도(1080p 초과만 1080p) · 원본 FPS(정수 반올림) CFR · Keyframe 2초 ·
  AAC 44.1kHz 스테레오 → 변환본끼리는 항상 Playlist DIRECT COPY 호환.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Sequence

from .live_ready import LiveReadyCancelled, make_live_ready_file

SOURCE_KEPT = "원본 파일은 그대로 보관됩니다."
READY_AUDIO = ("aac", 44100, 2)


def output_fps(fps: float) -> int:
    """원본 FPS 유지 (정수 반올림, 지원 범위 밖이면 30)."""
    n = int(round(fps or 0))
    return n if 1 <= n <= 60 else 30


def item_ok(report) -> bool:
    """혼자서 Playlist에 쓸 수 있는가 (LIVE READY + VFR/영상·소리 길이 차이 없음 — Playlist에서는 경계마다 반복)."""
    if report is None or not report.ready:
        return False
    return not any(i.code in ("vfr", "av_length") for i in report.issues)


def _spec(r) -> tuple:
    return (round(float(r.fps), 2), r.video_codec, r.audio_codec, r.sample_rate, r.channels)


def _converted_spec(r) -> tuple:
    return (float(output_fps(r.fps)), "h264", *READY_AUDIO)


def plan_bulk_conversion(reports: Sequence) -> list[int]:
    """변환할 index. 개별 문제 영상 + 변환 후에도 규격(FPS/코덱/오디오)이 서로 다를 영상.
    해상도 차이는 변환으로 맞추지 않는다 (원본 해상도 유지) → 검사 결과에 그대로 표시된다."""
    if not reports or any(r is None for r in reports):
        return []
    picked = {i for i, r in enumerate(reports) if not item_ok(r)}
    if len(reports) == 1:
        return sorted(picked)
    for _ in range(3):
        specs = [_converted_spec(r) if i in picked else _spec(r) for i, r in enumerate(reports)]
        if len(set(specs)) <= 1:
            break
        target = _converted_spec(reports[min(picked)]) if picked else specs[0]  # 변환본 규격, 없으면 1번 기준
        picked |= {i for i, s in enumerate(specs) if s != target}
    return sorted(picked)


@dataclass
class BulkResult:
    index: int
    source: Path
    output: Path | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.output is not None


def run_bulk_live_ready(*, ffmpeg: Path, ffprobe: Path, items: Sequence[tuple[int, Path, object]], cancel: Event,
                        progress: Callable[[int, int, str, float, str], None] | None = None,
                        convert: Callable = make_live_ready_file) -> tuple[list[BulkResult], bool]:
    """items: (playlist index, 원본 경로, LiveReadyReport). 순차 변환 → (결과, 취소 여부).
    한 파일이 실패해도 나머지는 계속한다. 취소하면 진행 중 파일의 .part.mp4는 make_live_ready_file이 지운다."""
    n = len(items)
    cb = progress or (lambda *a: None)
    results: list[BulkResult] = []
    for k, (index, src, rep) in enumerate(items, 1):
        src = Path(src)
        if cancel.is_set():
            return results, True
        cb(k, n, src.name, 0.0, "변환 준비 중")
        try:
            out = convert(ffmpeg=ffmpeg, ffprobe=ffprobe, src=src, height=int(rep.height or 0),
                          duration=float(rep.duration or 0.0), cancel=cancel, fps=output_fps(rep.fps),
                          progress_cb=lambda f, t, k=k, name=src.name: cb(k, n, name, f, t))
            results.append(BulkResult(index, src, Path(out)))
            cb(k, n, src.name, 1.0, "완료")
        except LiveReadyCancelled:
            return results, True
        except Exception as e:  # 다음 파일은 계속
            results.append(BulkResult(index, src, None, str(e)[:400]))
    return results, False


def summary_lines(results: Sequence[BulkResult], cancelled: bool) -> list[str]:
    lines = [f"✓ {r.source.name} → {r.output.name}" if r.ok else f"✗ {r.source.name}: {r.error.splitlines()[0] if r.error else '실패'}"
             for r in results]
    if cancelled:
        lines.append("■ 사용자가 변환을 중지했습니다.")
    lines.append(SOURCE_KEPT)
    return lines
