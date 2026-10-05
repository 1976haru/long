"""YouTube 자동 Broadcast 교체 (Phase 3B) — 11시간 50분마다 새 Broadcast로 교체, FFmpeg 송출은 계속.

순서 (같은 reusable liveStream 하나에 FFmpeg가 계속 송출):
  11:40  다음 Broadcast insert → 같은 stream에 bind → 'ready' 확인 → 다음 세션 준비됨
  11:50  ① 다음 Broadcast/바인딩 재확인 ② stream status active 확인
         ③ 현재 Broadcast → complete (확인) ④ 다음 Broadcast → live (liveStarting → live 확인)
         ⑤ 새 세션 시계 시작
안전 규칙:
  - 다음 Broadcast가 준비(바인딩 완료)되지 않았으면 현재 Broadcast를 complete하지 않는다
    (기본 정책 "방송 지속 우선": 12시간 보관 위험 경고를 표시하고 계속 재시도).
  - stream이 active가 아니면 complete하지 않는다.
  - API 오류로 FFmpeg를 멈추지 않는다 (FFmpeg/worker는 이 모듈이 제어하지 않음).
  - enableAutoStart/AutoStop = false, monitor stream off → 전환은 이 모듈이 직접 호출.
API 호출은 필요할 때만 (상시 health 5분, 준비/교체 중에만 3초 간격) — quota 보호.
"""
from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable

from .live_session import ARCHIVE_SAFE_SECONDS
from .youtube_api import PRIVACY_VALUES, TITLE_MAX, YouTubeApiClient, YouTubeApiError, validate_title

log = logging.getLogger(__name__)

STREAM_MODE_MANUAL = "MANUAL_STREAM_KEY"
STREAM_MODE_API = "YOUTUBE_API_MANAGED"
SESSION_YOUTUBE_AUTO = "youtube_auto_rollover"
PREPARE_LEAD_SECONDS = 600  # 11:40에 다음 방송 준비
RETRY_DELAYS = (5, 10, 30, 60)
HEALTH_POLL_SECONDS = 300
TRANSITION_POLL_SECONDS = 3
TRANSITION_TIMEOUT_SECONDS = 120
TITLE_RULE_SAME = "same"
TITLE_RULE_NUMBERED = "numbered"
ARCHIVE_RISK_WARNING = "⚠ 다음 방송 준비 실패 · 현재 방송 유지 중 (12시간을 넘으면 보관되지 않을 수 있습니다)"


def session_seconds_for_run() -> int:
    """운영값은 항상 42600 (11:50). 개발/테스트에서만 PLVM_DEV_MODE=1 + PLVM_DEV_SESSION_SECONDS로 짧게 (최소 60초)."""
    if os.environ.get("PLVM_DEV_MODE") == "1" and os.environ.get("PLVM_DEV_SESSION_SECONDS", "").isdigit():
        return max(60, int(os.environ["PLVM_DEV_SESSION_SECONDS"]))
    return ARCHIVE_SAFE_SECONDS


class RolloverState(enum.Enum):
    API_DISCONNECTED = "API_DISCONNECTED"
    API_READY = "API_READY"
    PREPARING_NEXT = "PREPARING_NEXT"
    NEXT_READY = "NEXT_READY"
    ROLLING_OVER = "ROLLING_OVER"
    VERIFYING_NEXT = "VERIFYING_NEXT"
    LIVE = "LIVE"
    ROLLOVER_FAILED = "ROLLOVER_FAILED"


STATE_LABELS = {
    RolloverState.API_DISCONNECTED: "YouTube 연결 안 됨",
    RolloverState.API_READY: "YouTube API 정상",
    RolloverState.PREPARING_NEXT: "다음 세션 준비 중",
    RolloverState.NEXT_READY: "✓ 다음 세션 준비됨",
    RolloverState.ROLLING_OVER: "방송 교체 중",
    RolloverState.VERIFYING_NEXT: "새 방송 확인 중",
    RolloverState.LIVE: "● LIVE",
    RolloverState.ROLLOVER_FAILED: "⚠ 방송 교체 문제",
}


@dataclass
class BroadcastTemplate:
    title: str
    description: str = ""
    privacy: str = "unlisted"
    made_for_kids: bool = False
    title_rule: str = TITLE_RULE_SAME

    def validate(self) -> None:
        validate_title(self.title)
        if self.privacy not in PRIVACY_VALUES:
            raise YouTubeApiError("공개 상태가 올바르지 않습니다.", kind="config", reason="invalidPrivacy")
        if self.title_rule not in (TITLE_RULE_SAME, TITLE_RULE_NUMBERED):
            raise YouTubeApiError("제목 규칙이 올바르지 않습니다.", kind="config", reason="invalidTitleRule")

    def title_for(self, session_number: int) -> str:
        t = self.title.strip()
        if self.title_rule == TITLE_RULE_SAME or session_number <= 1:
            return t
        suffix = f" | LIVE #{session_number:02d}"
        base = t.rsplit(" | ", 1)[0] if " | " in t else t
        return base[: TITLE_MAX - len(suffix)].rstrip() + suffix


@dataclass(frozen=True)
class RolloverSnapshot:
    state: RolloverState
    label: str
    session_number: int
    current_broadcast_id: str
    next_ready: bool
    seconds_until_rollover: float | None
    warning: str
    last_error: str


class YouTubeRolloverManager:
    def __init__(self, api: YouTubeApiClient, *, stream_id: str, template: BroadcastTemplate,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 session_seconds: float | None = None, prepare_lead: float = PREPARE_LEAD_SECONDS,
                 retry_delays=RETRY_DELAYS, health_poll: float = HEALTH_POLL_SECONDS,
                 transition_poll: float = TRANSITION_POLL_SECONDS,
                 transition_timeout: float = TRANSITION_TIMEOUT_SECONDS,
                 on_event: Callable[[str, str], None] | None = None):
        template.validate()
        self.api = api
        self.stream_id = stream_id
        self.template = template
        self.clock = clock
        self.sleep = sleep
        self.session_seconds = float(session_seconds if session_seconds is not None else session_seconds_for_run())
        self.prepare_lead = float(prepare_lead)
        self.retry_delays = tuple(retry_delays)
        self.health_poll = health_poll
        self.transition_poll = transition_poll
        self.transition_timeout = transition_timeout
        self.on_event = on_event
        self.state = RolloverState.API_READY
        self.current_id = ""
        self.next_id = ""
        self._pending_next_id = ""  # insert는 됐지만 bind 전 (재시도 때 새로 만들지 않음)
        self.session_number = 0
        self.session_started_at: float | None = None
        self.next_retry_at: float | None = None
        self.prepare_attempt = 0
        self.rollover_attempt = 0
        self.blocked = False  # 설정 오류: 사용자 조치 전까지 자동 재시도 중단
        self.warning = ""
        self.last_error = ""
        self.last_health_at = 0.0
        self._awaiting_next_live = False  # 현재 complete 후 다음 live 전환 재시도 중

    def __repr__(self) -> str:
        return f"YouTubeRolloverManager(state={self.state.value}, session={self.session_number})"

    def _event(self, kind: str, message: str = "") -> None:
        log.info("rollover %s %s", kind, message)
        if self.on_event:
            try:
                self.on_event(kind, message)
            except Exception:
                log.exception("rollover event callback failed")

    def _set(self, state: RolloverState, message: str = "") -> None:
        self.state = state
        self._event("state", f"{state.value} {message}".strip())

    # ---------- helpers ----------
    def _wait_stream_active(self) -> bool:
        end = self.clock() + self.transition_timeout
        while True:
            if self.api.get_stream(self.stream_id).stream_status == "active":
                return True
            if self.clock() >= end:
                return False
            self.sleep(self.transition_poll)

    def _wait_status(self, broadcast_id: str, wanted: tuple[str, ...]) -> str:
        end = self.clock() + self.transition_timeout
        while True:
            st = self.api.get_broadcast(broadcast_id).life_cycle_status
            if st in wanted:
                return st
            if self.clock() >= end:
                return st
            self.sleep(self.transition_poll)

    def _transition(self, broadcast_id: str, status: str) -> None:
        try:
            self.api.transition_broadcast(broadcast_id, status)
        except YouTubeApiError as e:
            if e.reason != "redundantTransition":  # 이미 그 상태/처리 중이면 성공으로 본다
                raise

    def _new_broadcast(self, number: int) -> str:
        """insert → bind. insert 후 bind가 실패하면 다음 재시도에서 같은 Broadcast를 bind만 한다."""
        if not self._pending_next_id:
            b = self.api.insert_broadcast(title=self.template.title_for(number), description=self.template.description,
                                          privacy=self.template.privacy, made_for_kids=self.template.made_for_kids,
                                          scheduled_start=self.clock() + 60)
            self._pending_next_id = b.id
        b = self.api.bind_broadcast(self._pending_next_id, self.stream_id)
        if b.bound_stream_id and b.bound_stream_id != self.stream_id:
            raise YouTubeApiError("다음 방송이 다른 스트림에 연결되었습니다.", kind="config", reason="wrongStream")
        bid, self._pending_next_id = self._pending_next_id, ""
        return bid

    # ---------- 첫 방송 ----------
    def go_live_first(self) -> str:
        """FFmpeg가 reusable stream으로 송출을 시작한 뒤 호출: 방송 생성 → bind → stream active → live."""
        bid = self._new_broadcast(1)
        if not self._wait_stream_active():
            raise YouTubeApiError("YouTube가 송출 신호를 아직 받지 못했습니다 (stream inactive). 송출 상태를 확인하세요.",
                                  kind="transition", reason="errorStreamInactive")
        self._transition(bid, "live")
        st = self._wait_status(bid, ("live",))
        if st != "live":
            raise YouTubeApiError(f"YouTube 방송이 live가 되지 않았습니다 (상태: {st}).", kind="transition", reason="notLive")
        self.attach(bid, self.clock(), session_number=1)
        return bid

    def attach(self, current_broadcast_id: str, started_at: float, *, session_number: int = 1) -> None:
        self.current_id = current_broadcast_id
        self.session_started_at = started_at
        self.session_number = session_number
        self.next_id = ""
        self.warning = ""
        self.last_health_at = self.clock()
        self._set(RolloverState.LIVE, f"session={session_number}")

    # ---------- 주기 호출 ----------
    def seconds_until_rollover(self) -> float | None:
        if self.session_started_at is None:
            return None
        return max(0.0, self.session_seconds - (self.clock() - self.session_started_at))

    def tick(self) -> RolloverState:
        """10초 정도마다 호출. API는 필요할 때만 호출한다."""
        if self.session_started_at is None or self.state is RolloverState.API_DISCONNECTED:
            return self.state
        now = self.clock()
        if self._awaiting_next_live:  # 현재는 complete 됨, 다음 live 전환 재시도
            if self.next_retry_at is None or now >= self.next_retry_at:
                self._finish_rollover()
            return self.state
        elapsed = now - self.session_started_at
        due_prepare = elapsed >= self.session_seconds - self.prepare_lead
        if not self.next_id and due_prepare and not self.blocked and (self.next_retry_at is None or now >= self.next_retry_at):
            self._prepare_next()
        if elapsed >= self.session_seconds:
            if self.next_id and (self.next_retry_at is None or now >= self.next_retry_at):
                self._rollover()
            elif not self.next_id:
                self.warning = ARCHIVE_RISK_WARNING  # 방송 지속 우선: 현재 방송을 complete하지 않는다
        elif now - self.last_health_at >= self.health_poll and self.state in (RolloverState.LIVE, RolloverState.NEXT_READY):
            self._health()
        return self.state

    def _retry_later(self, attempt: int) -> None:
        self.next_retry_at = self.clock() + self.retry_delays[min(attempt, len(self.retry_delays) - 1)]

    def _prepare_next(self) -> None:
        self._set(RolloverState.PREPARING_NEXT)
        try:
            self.next_id = self._new_broadcast(self.session_number + 1)
        except YouTubeApiError as e:
            self.last_error = str(e)
            if e.retryable or e.kind == "transition":
                self._retry_later(self.prepare_attempt)
                self.prepare_attempt += 1
            else:
                self.blocked = True  # 권한/설정 오류: 무한 재시도하지 않음
            self.warning = ARCHIVE_RISK_WARNING if (self.seconds_until_rollover() or 0) <= 0 else "⚠ 다음 방송 준비 실패 · 재시도 중"
            self._set(RolloverState.ROLLOVER_FAILED, f"prepare: {e.reason}")
            return
        self.prepare_attempt = 0
        self.next_retry_at = None
        self.warning = ""
        self._set(RolloverState.NEXT_READY, self.next_id)

    def _rollover(self) -> None:
        self._set(RolloverState.ROLLING_OVER)
        try:
            nb = self.api.get_broadcast(self.next_id)
            if nb.bound_stream_id != self.stream_id or nb.life_cycle_status not in ("ready", "created"):
                if nb.life_cycle_status in ("complete", "revoked"):
                    self.next_id = ""  # 다음 방송이 쓸 수 없게 됨 → 다시 준비
                    raise YouTubeApiError("준비된 다음 방송을 쓸 수 없습니다.", kind="transient", reason="nextUnusable")
                self.api.bind_broadcast(self.next_id, self.stream_id)
            if not self._wait_stream_active():
                raise YouTubeApiError("송출 신호가 끊겨 있어 방송을 교체하지 않았습니다 (현재 방송 유지).",
                                      kind="transition", reason="errorStreamInactive")
            self._transition(self.current_id, "complete")
            self._wait_status(self.current_id, ("complete",))
        except YouTubeApiError as e:
            # 현재 방송은 그대로 (complete 전 실패) → 재시도
            self.last_error = str(e)
            self.warning = ARCHIVE_RISK_WARNING
            self._retry_later(self.rollover_attempt)
            self.rollover_attempt += 1
            self._set(RolloverState.ROLLOVER_FAILED, f"before-complete: {e.reason}")
            if e.kind == "config":
                self.blocked = True
            return
        self._awaiting_next_live = True
        self._finish_rollover()

    def _finish_rollover(self) -> None:
        """현재는 complete됨, 다음 방송을 live로. 실패해도 다음 방송 정보가 있으므로 계속 재시도한다."""
        self._set(RolloverState.VERIFYING_NEXT, self.next_id)
        try:
            if not self._wait_stream_active():
                raise YouTubeApiError("송출 신호를 기다리는 중입니다.", kind="transition", reason="errorStreamInactive")
            self._transition(self.next_id, "live")
            st = self._wait_status(self.next_id, ("live",))
            if st != "live":
                raise YouTubeApiError(f"새 방송이 아직 live가 아닙니다 ({st}).", kind="transition", reason="notLive")
        except YouTubeApiError as e:
            self.last_error = str(e)
            self.warning = "⚠ 새 방송 시작 재시도 중 (송출은 계속됩니다)"
            self._retry_later(self.rollover_attempt)
            self.rollover_attempt += 1
            self._set(RolloverState.ROLLOVER_FAILED, f"next-live: {e.reason}")
            return
        self._awaiting_next_live = False
        self.rollover_attempt = 0
        self.next_retry_at = None
        self.last_error = ""
        new_id, self.next_id = self.next_id, ""
        self.attach(new_id, self.clock(), session_number=self.session_number + 1)
        self._event("rolled_over", new_id)

    def _health(self) -> None:
        self.last_health_at = self.clock()
        try:
            st = self.api.get_broadcast(self.current_id).life_cycle_status
        except YouTubeApiError as e:
            self.last_error = str(e)
            return
        if st not in ("live", "liveStarting"):
            self.warning = f"⚠ YouTube 방송 상태가 live가 아닙니다 ({st})"
        elif self.warning.startswith("⚠ YouTube 방송 상태"):
            self.warning = ""

    def retry_now(self) -> None:
        """사용자가 연결/권한을 고친 뒤 [다시 시도]."""
        self.blocked = False
        self.next_retry_at = None

    def snapshot(self) -> RolloverSnapshot:
        return RolloverSnapshot(self.state, STATE_LABELS[self.state], self.session_number, self.current_id,
                                bool(self.next_id), self.seconds_until_rollover(), self.warning, self.last_error)


class YouTubeApiSessionProvider:
    """Phase 3A SessionRolloverProvider 인터페이스 구현 (ManualSessionProvider 대체)."""

    def __init__(self, manager: YouTubeRolloverManager, ingest_url: str):
        self.manager = manager
        self.ingest_url = ingest_url

    def prepare_next_broadcast(self) -> str:
        if not self.manager.next_id:
            self.manager._prepare_next()
        return STATE_LABELS[self.manager.state]

    def complete_current_broadcast(self) -> None:
        return None  # 교체 시 manager가 '다음 방송 준비 확인 후' complete한다

    def get_next_ingest(self, current_ingest: str) -> str:
        return self.ingest_url or current_ingest
