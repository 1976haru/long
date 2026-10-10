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

from .live_ready import READY_SUFFIX, LiveReadyCancelled, make_live_ready_file

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


def _plan(reports: Sequence) -> tuple[list[int], tuple | None]:
    """(변환할 index, 맞출 규격). 규격 = 변환본 규격(변환 대상이 있으면) 또는 1번 영상 규격."""
    if not reports or any(r is None for r in reports):
        return [], None
    picked = {i for i, r in enumerate(reports) if not item_ok(r)}
    target = _converted_spec(reports[min(picked)]) if picked else _spec(reports[0])
    if len(reports) == 1:
        return sorted(picked), target
    for _ in range(3):
        specs = [_converted_spec(r) if i in picked else _spec(r) for i, r in enumerate(reports)]
        if len(set(specs)) <= 1:
            break
        target = _converted_spec(reports[min(picked)]) if picked else specs[0]  # 변환본 규격, 없으면 1번 기준
        picked |= {i for i, s in enumerate(specs) if s != target}
    return sorted(picked), target


def plan_bulk_conversion(reports: Sequence) -> list[int]:
    """변환할 index. 개별 문제 영상 + 변환 후에도 규격(FPS/코덱/오디오)이 서로 다를 영상.
    해상도 차이는 변환으로 맞추지 않는다 (원본 해상도 유지) → 검사 결과에 그대로 표시된다.
    이미 LIVE READY이고 Playlist 규격과 맞는 영상은 절대 포함하지 않는다 (파일 이름이 아니라 분석 결과 기준)."""
    return _plan(reports)[0]


def plan_target_spec(reports: Sequence) -> tuple | None:
    return _plan(reports)[1]


# ---------------- 표의 상태 / 변환 이유 (초보자용 문구) ----------------

KIND_ANALYZING, KIND_OK, KIND_CONVERT, KIND_MATCH = "analyzing", "ok", "convert", "match"
OK_TEXT = "✓ LIVE READY · 변환 안 함"
ANALYZING_TEXT = "○ 분석 중"


@dataclass(frozen=True)
class ItemPlan:
    index: int
    kind: str  # analyzing | ok | convert | match
    status: str  # 표의 '상태' 열
    reason: str = ""  # 왜 변환하는지 (쉬운 말)

    @property
    def needs_conversion(self) -> bool:
        return self.kind in (KIND_CONVERT, KIND_MATCH)


def _short_issue(report) -> str:
    for i in report.issues:
        if i.blocking or i.code in ("vfr", "av_length"):
            if i.code == "vfr":
                return "가변 프레임(VFR)"
            if i.code == "av_length":
                return "영상/소리 길이 차이"
            return i.message.split(" (", 1)[0].rstrip(".")
    return "LIVE READY 아님"


def _match_reason(report, target: tuple) -> tuple[str, str]:
    """(짧은 이름, 이유 문장) — LIVE READY지만 다른 Playlist 영상과 규격이 달라 맞춰야 하는 경우."""
    fps, vcodec, acodec, rate, ch = _spec(report)
    tfps, tv, ta, trate, tch = target
    names, parts = [], []
    if fps != tfps:
        names.append("FPS")
        parts.append(f"다른 영상은 {tfps:g}fps인데 이 영상은 {fps:g}fps라")
    if vcodec != tv:
        names.append("영상 코덱")
        parts.append(f"영상 코덱이 {vcodec}(다른 영상: {tv})라")
    if (acodec, rate, ch) != (ta, trate, tch):
        names.append("오디오")
        parts.append(f"오디오가 {acodec} {rate}Hz {ch}ch(다른 영상: {ta} {trate}Hz {tch}ch)라")
    if not parts:
        return "규격", "Playlist DIRECT COPY를 위해 규격을 맞춰야 합니다."
    return " · ".join(names), ", ".join(parts) + " Playlist DIRECT COPY를 위해 규격을 맞춰야 합니다."


def plan_items(reports: Sequence) -> list[ItemPlan]:
    """Playlist 표의 상태 + 변환 대상 미리 보기. 변환 대상 = plan_bulk_conversion 과 같다."""
    if any(r is None for r in reports):  # 분석이 끝나야 규격 비교 가능
        return [ItemPlan(i, KIND_ANALYZING, ANALYZING_TEXT) if r is None else
                (ItemPlan(i, KIND_CONVERT, f"⚠ {_short_issue(r)} · 변환 필요", _short_issue(r)) if not item_ok(r) else
                 ItemPlan(i, KIND_OK, "✓ LIVE READY (다른 영상 분석 중)"))
                for i, r in enumerate(reports)]
    picked, target = _plan(reports)
    out = []
    for i, r in enumerate(reports):
        if i not in picked:
            out.append(ItemPlan(i, KIND_OK, OK_TEXT))
        elif not item_ok(r):
            out.append(ItemPlan(i, KIND_CONVERT, f"⚠ {_short_issue(r)} · 변환 필요",
                                f"{_short_issue(r)} — LIVE로 반복 송출하려면 변환이 필요합니다."))
        else:
            what, why = _match_reason(r, target)
            out.append(ItemPlan(i, KIND_MATCH, f"⚠ LIVE READY · {what} 불일치 · 규격 맞춤 필요", why))
    return out


# ---------------- 기존 변환본 재사용 (덮어쓰지 않음) ----------------

def default_output_path(src: Path) -> Path:
    """live_ready_output_path와 같은 이름 규칙이지만 _2/_3 번호를 붙이지 않은 기본 이름."""
    src = Path(src)
    stem = src.stem if not src.stem.endswith(READY_SUFFIX) else src.stem + "_2"
    return src.with_name(f"{stem}{READY_SUFFIX}.mp4")


REUSE_DURATION_TOLERANCE = 1.0


def find_reusable_output(src: Path, report, target: tuple | None, analyze: Callable) -> Path | None:
    """원본 옆에 이미 있는 기본 이름 변환본이 유효하면 그 경로 (불확실하면 None → 새 파일로 변환).
    유효 = 원본보다 나중에 만들어짐 + 분석 결과 LIVE READY + Playlist 규격 일치 + 길이 차이 1초 이하."""
    src = Path(src)
    cand = default_output_path(src)
    try:
        if not cand.is_file() or cand.resolve() == src.resolve():
            return None
        if cand.stat().st_mtime < src.stat().st_mtime:  # 원본이 나중에 바뀜 → 옛 변환본
            return None
        rep = analyze(cand)
    except Exception:
        return None
    if not item_ok(rep):
        return None
    if target is not None and _spec(rep) != target:
        return None
    if abs(float(rep.duration or 0) - float(getattr(report, "duration", 0) or 0)) > REUSE_DURATION_TOLERANCE:
        return None
    return cand


@dataclass
class BulkResult:
    index: int
    source: Path
    output: Path | None = None
    error: str = ""
    reused: bool = False  # 이미 있던 유효한 변환본을 그대로 씀 (FFmpeg 실행 안 함)

    @property
    def ok(self) -> bool:
        return self.output is not None


def run_bulk_live_ready(*, ffmpeg: Path, ffprobe: Path, items: Sequence[tuple[int, Path, object]], cancel: Event,
                        progress: Callable[[int, int, str, float, str], None] | None = None,
                        convert: Callable = make_live_ready_file,
                        reuse: Callable[[Path, object], Path | None] | None = None,
                        on_item: Callable[[BulkResult], None] | None = None) -> tuple[list[BulkResult], bool]:
    """items: (playlist index, 원본 경로, LiveReadyReport). 순차 변환 → (결과, 취소 여부).
    한 파일이 끝나면 다음 파일을 자동으로 시작한다 (FFmpeg 동시 실행 1개). 한 파일이 실패해도 나머지는 계속한다.
    취소하면 진행 중 파일의 .part.mp4는 make_live_ready_file이 지우고 다음 파일은 시작하지 않는다.
    reuse: 이미 있는 유효한 변환본 경로를 돌려주면 그 파일은 변환하지 않는다."""
    n = len(items)
    cb = progress or (lambda *a: None)
    done = on_item or (lambda r: None)
    results: list[BulkResult] = []
    for k, (index, src, rep) in enumerate(items, 1):
        src = Path(src)
        if cancel.is_set():
            return results, True
        cb(k, n, src.name, 0.0, "변환 준비 중")
        existing = reuse(src, rep) if reuse is not None else None
        if existing is not None:
            results.append(BulkResult(index, src, Path(existing), reused=True))
            cb(k, n, src.name, 1.0, "기존 변환본 재사용")
            done(results[-1])
            continue
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
        done(results[-1])
    return results, False


def summary_lines(results: Sequence[BulkResult], cancelled: bool) -> list[str]:
    lines = [(f"↺ {r.source.name} → {r.output.name} (기존 변환본 재사용)" if r.reused else
              f"✓ {r.source.name} → {r.output.name}") if r.ok else
             f"✗ {r.source.name}: {r.error.splitlines()[0] if r.error else '실패'}"
             for r in results]
    if cancelled:
        lines.append("■ 사용자가 변환을 중지했습니다.")
    lines.append(SOURCE_KEPT)
    return lines
