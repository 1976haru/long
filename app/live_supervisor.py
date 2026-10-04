"""LIVE watchdog: FFmpeg 비정상 종료 시 bounded backoff로 재접속한다.

- 예기치 않은 종료 → RECONNECT_WAIT 후 재시작 (5, 10, 30, 60, 이후 60초)
- 사용자 STOP → 재접속하지 않음
- sleep을 직접 쓰지 않는다. poll()을 주기적으로 호출하는 구조이며 clock은 주입 가능하다.
"""
from __future__ import annotations

import enum
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

from .live_core import LiveProcess, LiveStats, build_live_command
from .live_profile import LiveConfig
from .tooling import FFMPEG_GUARD, FfmpegExecutionGuard

log = logging.getLogger(__name__)

RETRY_DELAYS = (5, 10, 30, 60)
# 이 시간 이상 정상 송출된 뒤 끊기면 backoff를 처음(5초)부터 다시 시작한다.
STABLE_RESET_SECONDS = 60.0
GUARD_OWNER = "live"


def retry_delay(attempt: int) -> int:
    """attempt는 0부터. 마지막 값(60초)에서 계속 머문다."""
    return RETRY_DELAYS[min(max(attempt, 0), len(RETRY_DELAYS) - 1)]


class LiveState(enum.Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    RECONNECT_WAIT = "RECONNECT_WAIT"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


class LiveBusyError(RuntimeError):
    pass


class ProcessLike(Protocol):
    def start(self) -> None: ...

    def stop(self, timeout: float | None = None) -> int | None: ...

    def is_running(self) -> bool: ...

    def return_code(self) -> int | None: ...

    def uptime(self) -> float: ...


def make_process_factory(ffmpeg: Path, config: LiveConfig) -> Callable[[], LiveProcess]:
    def factory() -> LiveProcess:
        cmd = build_live_command(ffmpeg=ffmpeg, config=config)
        return LiveProcess(cmd, secrets=[config.stream_key])
    return factory


class LiveSupervisor:
    def __init__(
        self,
        process_factory: Callable[[], ProcessLike],
        *,
        guard: FfmpegExecutionGuard = FFMPEG_GUARD,
        clock: Callable[[], float] = time.monotonic,
        on_state: Callable[[LiveState, str], None] | None = None,
    ):
        self._factory = process_factory
        self._guard = guard
        self._clock = clock
        self._on_state = on_state
        self._lock = threading.RLock()
        self._proc: ProcessLike | None = None
        self._user_stop = False
        self._run_started = 0.0
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self.state = LiveState.STOPPED
        self.attempt = 0
        self.next_retry_at: float | None = None
        self.last_exit_code: int | None = None
        self.last_error = ""

    def _set(self, state: LiveState, message: str = "") -> None:
        self.state = state
        log.info("LIVE state=%s %s", state.value, message)
        if self._on_state:
            try:
                self._on_state(state, message)
            except Exception:
                log.exception("on_state callback failed")

    @property
    def active(self) -> bool:
        return self.state not in (LiveState.STOPPED, LiveState.FAILED)

    def start(self) -> None:
        with self._lock:
            if self.active:
                raise LiveBusyError("LIVE가 이미 실행 중입니다.")
            if not self._guard.try_acquire(GUARD_OWNER):
                raise LiveBusyError("다른 FFmpeg 작업(장시간 영상 제작)이 실행 중이라 LIVE를 시작할 수 없습니다.")
            self._user_stop = False
            self.attempt = 0
            self.next_retry_at = None
            self.last_exit_code = None
            self.last_error = ""
            self._launch()

    def _launch(self) -> None:
        self._set(LiveState.STARTING)
        try:
            proc = self._factory()
            proc.start()
        except Exception as e:
            # 설정/실행 파일 문제는 재시도해도 해결되지 않으므로 FAILED.
            self._proc = None
            self.last_error = str(e)
            self._guard.release(GUARD_OWNER)
            self._set(LiveState.FAILED, self.last_error)
            return
        self._proc = proc
        self._run_started = self._clock()
        self.next_retry_at = None
        self._set(LiveState.RUNNING)

    def poll(self) -> LiveState:
        """주기적으로 호출. 프로세스 종료 감지 및 재접속 예약/실행."""
        with self._lock:
            if self._user_stop:
                return self.state
            now = self._clock()
            if self.state is LiveState.RUNNING and self._proc is not None and not self._proc.is_running():
                self.last_exit_code = self._proc.return_code()
                try:
                    self._proc.stop()  # 종료된 프로세스 회수 (zombie 방지)
                except Exception:
                    log.exception("LIVE process cleanup failed")
                if now - self._run_started >= STABLE_RESET_SECONDS:
                    self.attempt = 0
                delay = retry_delay(self.attempt)
                self.attempt += 1
                self.next_retry_at = now + delay
                self._set(LiveState.RECONNECT_WAIT, f"rc={self.last_exit_code} retry_in={delay}s attempt={self.attempt}")
            elif self.state is LiveState.RECONNECT_WAIT and self.next_retry_at is not None and now >= self.next_retry_at:
                self._launch()
            return self.state

    def stop(self) -> None:
        """사용자 중지. 이후 자동 재접속하지 않는다."""
        with self._lock:
            self._user_stop = True
            self._wake.set()
            if not self.active:
                return
            self._set(LiveState.STOPPING)
            proc, self._proc = self._proc, None
            try:
                if proc is not None:
                    self.last_exit_code = proc.stop()
            finally:
                self.next_retry_at = None
                self._guard.release(GUARD_OWNER)
                self._set(LiveState.STOPPED)
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2)

    def seconds_until_retry(self) -> float | None:
        if self.state is not LiveState.RECONNECT_WAIT or self.next_retry_at is None:
            return None
        return max(0.0, self.next_retry_at - self._clock())

    def stats(self) -> LiveStats | None:
        proc = self._proc
        return proc.stats() if proc is not None and hasattr(proc, "stats") else None

    def run_in_background(self, interval: float = 0.5) -> threading.Thread:
        """start() 후 watchdog 스레드를 띄운다. GUI thread를 막지 않는다."""
        self._wake.clear()

        def loop():
            while not self._wake.wait(interval):
                if self.poll() in (LiveState.STOPPED, LiveState.FAILED):
                    break

        self._thread = threading.Thread(target=loop, name="live-watchdog", daemon=True)
        self._thread.start()
        return self._thread
