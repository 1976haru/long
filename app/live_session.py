"""LIVE 세션 관리 (Phase 3A): 계속 방송 / 보관 안전 모드(11시간 50분) + 다음 세션 hook.

- 11시간 50분(42600초)은 YouTube 공식 숫자가 아니라, 12시간을 넘는 LIVE가 보관되지 않을 수 있는 위험에
  여유를 둔 이 프로그램의 운영값이다.
- Phase 3A의 보관 안전 모드는 "11:50에 안전 종료 → 다음 세션 대기"까지만 한다.
  새 YouTube Broadcast 자동 생성은 Phase 3B (YouTubeApiSessionProvider)에서 붙인다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

ARCHIVE_SAFE_SECONDS = 11 * 3600 + 50 * 60  # 42600
SESSION_CONTINUOUS = "continuous"
SESSION_ARCHIVE_SAFE = "archive_safe"
SESSION_MODES = (SESSION_CONTINUOUS, SESSION_ARCHIVE_SAFE)

NEXT_SESSION_MESSAGE = "다음 YouTube LIVE를 준비한 뒤\n[다음 세션 시작]을 눌러 주세요."


def session_limit_seconds(mode: str) -> float | None:
    if mode not in SESSION_MODES:
        raise ValueError(f"unknown session mode: {mode}")
    return ARCHIVE_SAFE_SECONDS if mode == SESSION_ARCHIVE_SAFE else None


def archive_notice(remaining: float | None) -> str:
    """상태 텍스트용 (팝업 없음)."""
    if remaining is None:
        return ""
    if remaining <= 0:
        return "보관 안전 종료 시간입니다"
    if remaining <= 60:
        return "약 1분 후 종료"
    if remaining <= 300:
        return "약 5분 후 종료"
    if remaining <= 600:
        return "약 10분 후 보관 안전 종료"
    return ""


def playlist_position(out_time: float | None, durations: Sequence[float]) -> tuple[int, int] | None:
    """송출 위치(초) → (현재 영상 index 0-based, playlist 회차 1-based). concat 타임스탬프가 연속이므로 계산 가능."""
    if out_time is None or not durations:
        return None
    total = sum(durations)
    if total <= 0:
        return None
    t = max(0.0, float(out_time))
    rnd = int(t // total) + 1
    within = t - (rnd - 1) * total
    acc = 0.0
    for i, d in enumerate(durations):
        acc += d
        if within < acc:
            return i, rnd
    return len(durations) - 1, rnd


def monthly_transfer_bytes(mbps: float, *, hours_per_day: float = 24, days: float = 30, streams: int = 1) -> float:
    """예상 송출 트래픽 (외부 API 없이 계산만). 예: 6.3 Mbps × 24h × 30일 ≈ 2.04 TB."""
    return mbps * 1_000_000 / 8 * hours_per_day * 3600 * days * streams


def format_tb(num_bytes: float) -> str:
    return f"{num_bytes / 1e12:.2f} TB"


class SessionRolloverProvider(Protocol):
    """다음 세션 전환 방식. Phase 3B에서 YouTubeApiSessionProvider로 교체한다 (UI/backend 수정 없이)."""

    def prepare_next_broadcast(self) -> str: ...

    def complete_current_broadcast(self) -> None: ...

    def get_next_ingest(self, current_ingest: str) -> str: ...


@dataclass
class ManualSessionProvider:
    """Phase 3A 기본: 사용자가 YouTube Studio에서 다음 LIVE를 준비하고 [다음 세션 시작]을 누른다."""

    def prepare_next_broadcast(self) -> str:
        return NEXT_SESSION_MESSAGE

    def complete_current_broadcast(self) -> None:
        return None

    def get_next_ingest(self, current_ingest: str) -> str:
        return current_ingest
