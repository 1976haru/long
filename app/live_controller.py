"""LIVE 창의 로직 계층 (Tk 위젯과 분리, 단위 테스트 대상).

- preflight: LIVE 시작 전/설정 검사 시 실행하는 검사 목록
- LiveController: supervisor 수명, 이벤트 큐, keep-awake, 상태 snapshot
backend 스레드는 event queue에만 쓰고, Tk main thread가 drain_events()/snapshot()으로 읽는다.
모든 사용자 노출 문자열은 Stream Key가 redact된 상태다.
"""
from __future__ import annotations

import atexit
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .core import BuildError, VideoInfo, format_duration, probe_video
from .live_core import LiveConfigError, build_output_url, build_stream_command, describe_live_command
from .live_profile import MODE_COPY, MODE_TRANSCODE, LiveConfig, LivePreset, redact, validate_live_config
from .live_playlist import validate_playlist
from .live_session import archive_notice, playlist_position
from .live_supervisor import TERMINAL_STATES, LiveBusyError, LiveState, LiveSupervisor, busy_message, make_process_factory
from .tooling import FFMPEG_GUARD, FfmpegExecutionGuard, KeepAwake

STATE_LABELS = {
    LiveState.STOPPED: "대기",
    LiveState.STARTING: "연결 중",
    LiveState.RUNNING: "● LIVE",
    LiveState.RECONNECT_WAIT: "재연결 대기",
    LiveState.STOPPING: "종료 중",
    LiveState.FAILED: "오류",
    LiveState.SESSION_LIMIT_REACHED: "보관 안전 종료 · 다음 세션 대기",
}
LOCKED_STATES = (LiveState.STARTING, LiveState.RUNNING, LiveState.RECONNECT_WAIT, LiveState.STOPPING)


def format_bitrate(value: str | None) -> str:
    """FFmpeg `8123.4kbits/s` → `8.1 Mbit/s`."""
    if not value:
        return "-"
    v = value.strip()
    if v.endswith("kbits/s"):
        try:
            kb = float(v[:-7])
        except ValueError:
            return v
        return f"{kb / 1000:.1f} Mbit/s" if kb >= 1000 else f"{kb:.0f} kbit/s"
    return v


def describe_input(info: VideoInfo) -> str:
    audio = f"{info.audio_codec.upper()} {info.sample_rate / 1000:g}kHz" if info.audio_codec else "오디오 없음"
    return (f"{info.width}×{info.height} / {info.fps:.2f}fps / {info.video_codec.upper()} / "
            f"{audio} / {format_duration(info.duration)}")


def probe_live_input(path: Path, ffprobe: Path, probe: Callable = probe_video) -> VideoInfo:
    """영상 stream이 있고 길이가 0보다 큰 파일만 통과. 오디오 여부는 preflight에서 판단."""
    path = Path(path)
    if not path.is_file():
        raise LiveConfigError(f"영상 파일을 찾을 수 없습니다: {path.name}")
    try:
        info = probe(path, ffprobe)
    except BuildError as e:
        raise LiveConfigError(f"영상 분석 실패 (영상 stream이 없거나 읽을 수 없는 파일): {path.name}") from e
    if info.duration <= 0:
        raise LiveConfigError(f"재생 시간이 0초인 영상입니다: {path.name}")
    if info.width <= 0 or info.height <= 0:
        raise LiveConfigError(f"영상 stream이 없습니다: {path.name}")
    return info


@dataclass(frozen=True)
class PreflightItem:
    ok: bool
    text: str


@dataclass
class PreflightResult:
    items: list[PreflightItem] = field(default_factory=list)
    config: LiveConfig | None = field(default=None, repr=False)
    info: VideoInfo | None = None

    @property
    def ok(self) -> bool:
        return all(i.ok for i in self.items) and self.config is not None

    def add(self, ok: bool, text: str) -> bool:
        self.items.append(PreflightItem(ok, text))
        return ok

    def errors(self) -> list[str]:
        return [i.text for i in self.items if not i.ok]

    def report(self) -> str:
        lines = [("✓ " if i.ok else "✗ ") + i.text for i in self.items]
        lines += ["", "LIVE 시작 준비 완료" if self.ok else "LIVE를 시작할 수 없습니다."]
        return "\n".join(lines)


def run_preflight(
    *,
    ffmpeg: Path | None,
    ffprobe: Path | None,
    input_path: str | Path | None,
    ingest_url: str,
    stream_key: str,
    preset: LivePreset,
    guard: FfmpegExecutionGuard = FFMPEG_GUARD,
    supervisor_state: LiveState = LiveState.STOPPED,
    probe: Callable = probe_video,
    mode: str = MODE_TRANSCODE,
    ready_report=None,
    location: str = "local",
    playlist_reports: list | None = None,
    manifest_path: Path | None = None,
) -> PreflightResult:
    """실제 송출 없이 검사. 결과 문자열에는 Stream Key가 절대 들어가지 않는다.

    mode=copy(DIRECT COPY)이면 ready_report(LiveReadyReport)가 LIVE READY여야 한다.
    location=cloud이면 PC FFmpeg 잠금(FFMPEG_GUARD)/로컬 supervisor 상태는 보지 않는다 (서버에서 실행).
    """
    r = PreflightResult()
    key = (stream_key or "").strip()

    tools_ok = bool(ffmpeg and ffprobe and Path(ffmpeg).exists() and Path(ffprobe).exists())
    r.add(tools_ok, "FFmpeg 준비" if tools_ok else "FFmpeg/ffprobe를 찾을 수 없습니다. 메인 창에서 FFmpeg 설정을 확인하세요.")

    info = None
    if not input_path:
        r.add(False, "LIVE 영상을 선택하세요.")
    elif tools_ok:
        try:
            info = probe_live_input(Path(input_path), Path(ffprobe), probe)
            r.info = info
            r.add(True, f"영상 확인: {Path(input_path).name}")
            r.add(True, f"{info.width}×{info.height} / {info.fps:.2f}fps / {info.video_codec.upper()} (입력 해상도 그대로 송출)")
            if info.audio_codec:
                r.add(True, f"오디오 {info.audio_codec.upper()} {info.sample_rate / 1000:g}kHz {info.channels}ch")
            else:
                r.add(False, "오디오가 없는 영상은 LIVE할 수 없습니다 (음악 LIVE용).")
        except LiveConfigError as e:
            r.add(False, str(e))

    url_ok = False
    try:
        build_output_url(ingest_url, key or "placeholder")
        url_ok = True
        secure = ingest_url.strip().lower().startswith("rtmps://")
        r.add(True, "YouTube RTMPS 송출 주소" if secure else "RTMP 송출 주소 (암호화 없음)")
    except LiveConfigError as e:
        r.add(False, str(e))

    if not key:
        r.add(False, "Stream Key를 입력하세요.")
    elif url_ok:
        try:
            build_output_url(ingest_url, key)
            r.add(True, "Stream Key 입력됨")
        except LiveConfigError as e:
            r.add(False, str(e))

    is_playlist = bool(playlist_reports) and len(playlist_reports) > 1
    if is_playlist:
        mode = MODE_COPY  # Playlist는 DIRECT COPY 전용
        v = validate_playlist(playlist_reports)
        if v.ok:
            r.add(True, f"Playlist {len(playlist_reports)}개 · 모두 LIVE READY · DIRECT COPY Playlist 가능")
        else:
            for m in v.messages:
                r.add(False, m)
        if manifest_path is None:
            r.add(False, "Playlist 목록 파일 위치가 없습니다.")
    elif mode == MODE_COPY:
        if ready_report is None:
            r.add(False, "LIVE READY 분석이 필요합니다.")
        elif ready_report.ready:
            r.add(True, "LIVE READY · DIRECT COPY (재인코딩 없음)")
        else:
            reason = next((i.message for i in ready_report.issues if i.blocking), "")
            r.add(False, f"LIVE READY 파일이 아닙니다: {reason} [LIVE READY 파일 만들기]를 사용하세요.")
    if location == "local":
        if guard.owner is not None and guard.owner != "live":
            r.add(False, busy_message(guard.owner))
        if supervisor_state not in TERMINAL_STATES:
            r.add(False, "LIVE가 이미 실행 중입니다.")

    if all(i.ok for i in r.items) and info is not None:
        config = LiveConfig(
            input_path=Path(manifest_path) if is_playlist else Path(input_path),
            ingest_url=ingest_url.strip(),
            stream_key=key,
            video_bitrate_kbps=preset.video_bitrate_kbps,
            audio_bitrate_kbps=preset.audio_bitrate_kbps,
            fps=preset.fps,
            keyframe_seconds=preset.keyframe_seconds,
            mode=mode,
            input_format="concat" if is_playlist else "",
        )
        try:
            validate_live_config(config, check_input=not is_playlist)
            cmd = build_stream_command(ffmpeg=Path(ffmpeg), config=config)
            text = describe_live_command(cmd, config)
            if key in text:
                r.add(False, "보안 검사 실패: 송출 정보에서 Stream Key를 가리지 못했습니다.")
            else:
                r.add(True, "송출 명령 생성 완료 (Stream Key 숨김)")
                r.config = config
        except LiveConfigError as e:
            r.add(False, str(e))
    # 어떤 경로로든 key가 문구에 섞이지 않게 마지막으로 한 번 더 가린다.
    r.items = [PreflightItem(i.ok, redact(i.text, [key])) for i in r.items]
    return r


@dataclass(frozen=True)
class LiveSnapshot:
    state: LiveState
    label: str
    session_seconds: float
    fps: float | None
    bitrate: str | None
    speed: float | None
    out_time_seconds: float | None
    reconnects: int
    retry_in: float | None
    last_exit_code: int | None
    last_error: str
    session_limit: float | None = None
    session_remaining: float | None = None
    notice: str = ""
    playlist_index: int | None = None  # 0-based
    playlist_count: int = 0
    playlist_round: int | None = None
    current_media: str = ""


class LiveController:
    def __init__(
        self,
        *,
        guard: FfmpegExecutionGuard = FFMPEG_GUARD,
        keep_awake: KeepAwake | None = None,
        clock: Callable[[], float] = time.monotonic,
        factory_builder: Callable = make_process_factory,
    ):
        self.guard = guard
        self.keep_awake = keep_awake or KeepAwake()
        self._clock = clock
        self._factory_builder = factory_builder
        self.events: queue.Queue = queue.Queue()
        self.supervisor: LiveSupervisor | None = None
        self._secret = ""
        self._stop_thread: threading.Thread | None = None
        self._playlist: list[tuple[str, float]] = []
        self._atexit_registered = False

    def __repr__(self) -> str:
        return f"LiveController(state={self.state.value})"

    @property
    def state(self) -> LiveState:
        return self.supervisor.state if self.supervisor else LiveState.STOPPED

    @property
    def active(self) -> bool:
        return self.state not in TERMINAL_STATES

    def _on_state(self, state: LiveState, message: str) -> None:
        # backend 스레드에서 호출될 수 있다: Tk 위젯을 만지지 않고 큐에만 넣는다.
        self.events.put(("state", state, STATE_LABELS[state], redact(message, [self._secret])))

    def start(self, *, ffmpeg: Path, config: LiveConfig, reconnect: bool = True,
              keep_awake: bool = True, background: bool = True, session_limit: float | None = None,
              playlist: list[tuple[str, float]] | None = None) -> LiveState:
        """Tk main thread에서 호출 (keep-awake가 호출 스레드에 묶이므로).

        session_limit: None=계속 방송, 숫자=보관 안전 모드(초). playlist: [(파일명, concat 항목 길이)] 상태 표시용.
        """
        if self.active:
            raise LiveBusyError("LIVE가 이미 실행 중입니다.")
        self._secret = config.stream_key
        self._playlist = list(playlist or [])
        sup = LiveSupervisor(
            self._factory_builder(ffmpeg, config),
            guard=self.guard, clock=self._clock, on_state=self._on_state, reconnect=reconnect,
            secrets=(config.stream_key,), session_limit=session_limit,
        )
        sup.start()  # guard 충돌 시 LiveBusyError
        self.supervisor = sup
        if not self._atexit_registered:
            atexit.register(self._atexit_stop)
            self._atexit_registered = True
        if sup.state is LiveState.RUNNING:
            if keep_awake:
                self.keep_awake.enable()
            if background:
                sup.run_in_background()
        return sup.state

    def _atexit_stop(self) -> None:
        # 마지막 안전장치: 인터프리터 종료 시 LIVE FFmpeg를 남기지 않는다.
        try:
            if self.active:
                self.stop_blocking(timeout=15)
        except Exception:
            pass

    def stop_async(self) -> threading.Thread | None:
        """GUI를 막지 않고 백그라운드에서 graceful stop (q → terminate → kill)."""
        sup = self.supervisor
        if sup is None or not self.active:
            return None
        if self._stop_thread and self._stop_thread.is_alive():
            return self._stop_thread
        self._stop_thread = threading.Thread(target=sup.stop, name="live-stop", daemon=True)
        self._stop_thread.start()
        return self._stop_thread

    def stop_blocking(self, timeout: float = 20.0) -> None:
        t = self.stop_async()
        if t is not None:
            t.join(timeout)
        elif self.supervisor is not None:
            self.supervisor.stop()  # 이미 종료됐어도 guard/thread 정리
        self.keep_awake.disable()

    @property
    def stopping(self) -> bool:
        return bool(self._stop_thread and self._stop_thread.is_alive())

    def drain_events(self) -> list[tuple]:
        """Tk main thread에서 주기적으로 호출."""
        out = []
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            out.append(ev)
            if ev[0] == "state" and ev[1] in TERMINAL_STATES:
                self.keep_awake.disable()
        return out

    def snapshot(self) -> LiveSnapshot:
        sup = self.supervisor
        state = self.state
        if sup is None:
            return LiveSnapshot(state, STATE_LABELS[state], 0.0, None, None, None, None, 0, None, None, "")
        stats = sup.stats() if state is LiveState.RUNNING else None
        session = 0.0
        if sup.session_started_at is not None and self.active:
            session = max(0.0, self._clock() - sup.session_started_at)
        remaining = sup.session_remaining()
        names = [n for n, _ in getattr(self, "_playlist", [])]
        pos = playlist_position(stats.out_time_seconds if stats else None, [d for _, d in getattr(self, "_playlist", [])])
        return LiveSnapshot(
            state=state,
            label=STATE_LABELS[state],
            session_seconds=session,
            fps=stats.fps if stats else None,
            bitrate=stats.bitrate if stats else None,
            speed=stats.speed if stats else None,
            out_time_seconds=stats.out_time_seconds if stats else None,
            reconnects=sup.reconnect_count,
            retry_in=sup.seconds_until_retry(),
            last_exit_code=sup.last_exit_code,
            last_error=redact(sup.last_error or "", [self._secret]),
            session_limit=sup.session_limit,
            session_remaining=remaining,
            notice=archive_notice(remaining),
            playlist_index=pos[0] if pos else None,
            playlist_count=len(names),
            playlist_round=pos[1] if pos else None,
            current_media=names[pos[0]] if pos and names else "",
        )
