"""여러 MP4 LIVE Playlist (순차 무한 반복, DIRECT COPY 전용) — Tk와 무관한 순수 모델.

- 1~20개, 순차 재생만 (shuffle 없음), 중복 파일 금지
- 모든 파일이 LIVE READY + 서로 같은 규격이어야 한다 (자동 재인코딩하지 않음)
- 재생은 FFmpeg concat demuxer + -stream_loop -1 + stream copy.
  각 항목 duration = 파일 길이 + AAC 1프레임(1024/샘플레이트). 실측 결과 경계에서 AAC 프레임 겹침으로 생기는
  "Non-monotonic DTS"가 사라지고, 60회 반복(178경계)에서도 A/V 차이가 ±29ms 안에서 누적되지 않았다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

MAX_PLAYLIST_ITEMS = 20
AAC_FRAME_SAMPLES = 1024
AV_TOLERANCE_SECONDS = 0.5
COMPAT_HINT = "Playlist DIRECT COPY는 모든 영상이 동일해야 합니다. [LIVE READY 파일 만들기]로 맞춰 주세요."


class PlaylistError(ValueError):
    pass


def _key(p: Path) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


@dataclass
class PlaylistItem:
    path: Path
    report: object | None = None  # LiveReadyReport (분석 완료 후)

    @property
    def name(self) -> str:
        return Path(self.path).name


@dataclass
class LivePlaylist:
    items: list[PlaylistItem] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.items)

    @property
    def paths(self) -> list[Path]:
        return [i.path for i in self.items]

    def add(self, path) -> PlaylistItem:
        p = Path(path)
        if len(self.items) >= MAX_PLAYLIST_ITEMS:
            raise PlaylistError(f"Playlist는 최대 {MAX_PLAYLIST_ITEMS}개까지 추가할 수 있습니다.")
        if any(_key(i.path) == _key(p) for i in self.items):
            raise PlaylistError(f"이미 추가된 영상입니다: {p.name}")
        item = PlaylistItem(p)
        self.items.append(item)
        return item

    def remove(self, index: int) -> None:
        if 0 <= index < len(self.items):
            self.items.pop(index)

    def move(self, index: int, delta: int) -> int:
        j = index + delta
        if 0 <= index < len(self.items) and 0 <= j < len(self.items):
            self.items[index], self.items[j] = self.items[j], self.items[index]
            return j
        return index

    def clear(self) -> None:
        self.items.clear()

    def set_report(self, path, report) -> None:
        for i in self.items:
            if _key(i.path) == _key(Path(path)):
                i.report = report

    @property
    def total_duration(self) -> float:
        return sum(getattr(i.report, "duration", 0.0) or 0.0 for i in self.items)

    @property
    def analyzed(self) -> bool:
        return all(i.report is not None for i in self.items)


@dataclass
class PlaylistValidationResult:
    ok: bool
    messages: list[str]
    item_status: list[str]  # 표의 '상태' 열

    @property
    def first_error(self) -> str:
        return self.messages[0] if self.messages else ""


def validate_playlist(reports: Sequence) -> PlaylistValidationResult:
    """모든 항목 LIVE READY + 서로 DIRECT COPY 이어붙이기 호환인지 검사."""
    msgs: list[str] = []
    status = ["확인 중" if r is None else "" for r in reports]
    if not reports:
        return PlaylistValidationResult(False, ["Playlist에 영상을 추가하세요."], [])
    if len(reports) > MAX_PLAYLIST_ITEMS:
        msgs.append(f"Playlist는 최대 {MAX_PLAYLIST_ITEMS}개까지입니다.")
    if any(r is None for r in reports):
        return PlaylistValidationResult(False, ["영상 분석 중입니다. 잠시 후 다시 확인하세요."], status)
    base = reports[0]
    for n, r in enumerate(reports, 1):
        problems = []
        blocking = [i.message for i in r.issues if i.blocking]
        if blocking:
            problems.append(f"{n}번 영상이 LIVE READY가 아닙니다: {blocking[0]}")
        for i in r.issues:  # Playlist에서는 경고도 차단 (경계마다 반복되기 때문)
            if not i.blocking and i.code == "vfr":
                problems.append(f"{n}번 영상이 가변 프레임(VFR)입니다.")
            if not i.blocking and i.code == "av_length":
                problems.append(f"{n}번 영상의 영상/소리 길이 차이가 큽니다 (허용 {AV_TOLERANCE_SECONDS}초).")
        if n > 1 and not blocking:
            if (r.width, r.height) != (base.width, base.height):
                problems.append(f"{n}번 영상의 해상도가 {r.width}×{r.height}입니다 (1번: {base.width}×{base.height}).")
            if abs(r.fps - base.fps) > 0.01:
                problems.append(f"{n}번 영상의 FPS가 {r.fps:.2f}fps입니다 (1번: {base.fps:.2f}fps).")
            if r.video_codec != base.video_codec:
                problems.append(f"{n}번 영상의 영상 코덱이 다릅니다 ({r.video_codec}).")
            if (r.audio_codec, r.sample_rate, r.channels) != (base.audio_codec, base.sample_rate, base.channels):
                problems.append(f"{n}번 영상의 오디오가 다릅니다 ({r.audio_codec} {r.sample_rate}Hz {r.channels}ch).")
        status[n - 1] = "✓ LIVE READY" if not problems else "✗ " + problems[0].split(": ", 1)[-1][:40]
        msgs += problems
    if msgs:
        msgs.append(COMPAT_HINT)
    return PlaylistValidationResult(not msgs, msgs, status)


def entry_durations(reports: Sequence) -> list[float]:
    """concat 항목 길이 = 파일 길이 + AAC 1프레임 (경계 DTS 겹침 방지, 실측 근거는 모듈 docstring)."""
    out = []
    for r in reports:
        rate = getattr(r, "sample_rate", 0) or 44100
        out.append(float(r.duration) + AAC_FRAME_SAMPLES / rate)
    return out


def escape_ffconcat_path(path) -> str:
    """ffconcat `file '...'` 안전 인용: / 경로, 작은따옴표는 '\\'' 로."""
    s = str(path).replace("\\", "/")
    if "\n" in s or "\r" in s:
        raise PlaylistError("파일 경로에 줄바꿈이 있습니다.")
    return "'" + s.replace("'", "'\\''") + "'"


def build_ffconcat(entries: Sequence[tuple]) -> str:
    """entries: (path, duration) — 순서대로. 경로는 호출자가 검증한 실제 파일 경로만 넣는다."""
    lines = ["ffconcat version 1.0"]
    for path, duration in entries:
        lines.append(f"file {escape_ffconcat_path(path)}")
        lines.append(f"duration {float(duration):.6f}")
    return "\n".join(lines) + "\n"


def write_ffconcat(target: Path, entries: Sequence[tuple]) -> Path:
    """.part → 이름 교체 (원자적)."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    part.write_text(build_ffconcat(entries), encoding="utf-8", newline="\n")
    os.replace(part, target)
    return target
