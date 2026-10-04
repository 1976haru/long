"""LIVE 송출 실행 경로 (단일 MP4 무한 반복 → RTMP/RTMPS).

장시간 MP4 제작 경로(app/core.py의 run_concat_copy, -c copy)와 분리되어 있다.
LIVE는 YouTube ingest 안정성을 위해 H.264/AAC 실시간 인코딩 프로필을 사용한다.
Python은 영상 프레임을 읽지 않고, FFmpeg -progress 출력만 파싱한다.
"""
from __future__ import annotations

import collections
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urlsplit

from .core import creationflags_no_window
from .live_profile import MASK, LiveConfig, LiveConfigError, redact, validate_live_config

log = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("rtmp", "rtmps")
_KEY_FORBIDDEN = set("/?#\\")


def build_output_url(ingest_url: str | None, stream_key: str | None) -> str:
    """ingest URL과 Stream Key를 결합하는 유일한 함수. 예외 메시지에 key를 넣지 않는다."""
    url = (ingest_url or "").strip()
    key = (stream_key or "").strip()
    if not url:
        raise LiveConfigError("송출 주소(ingest URL)가 비어 있습니다.")
    if not key:
        raise LiveConfigError("Stream Key가 비어 있습니다.")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise LiveConfigError("송출 주소는 rtmp:// 또는 rtmps:// 로 시작해야 합니다.")
    if not parts.hostname:
        raise LiveConfigError("송출 주소에 서버 주소가 없습니다.")
    if parts.query or parts.fragment:
        raise LiveConfigError("송출 주소에 ? 또는 # 을 포함할 수 없습니다.")
    if any(c.isspace() or not c.isprintable() or c in _KEY_FORBIDDEN for c in key):
        raise LiveConfigError("Stream Key 형식이 올바르지 않습니다 (공백/슬래시 등 사용 불가).")
    return f"{url.rstrip('/')}/{key}"


def mask_output_url(ingest_url: str) -> str:
    return f"{(ingest_url or '').strip().rstrip('/')}/{MASK}"


def build_live_command(
    *,
    ffmpeg: Path,
    config: LiveConfig,
    output_target: str | None = None,
    output_format: str = "flv",
) -> list[str]:
    """LIVE FFmpeg argument list. shell=True 없이 그대로 Popen에 전달한다.

    output_target을 지정하면 ingest URL 대신 그 대상(로컬 파일 등)으로 출력한다 (smoke/dry-run 용).
    """
    validate_live_config(config, check_input=False)
    target = output_target if output_target is not None else build_output_url(config.ingest_url, config.stream_key)
    gop = config.fps * config.keyframe_seconds
    vb = config.video_bitrate_kbps
    return [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-loglevel", "warning",
        # 실시간 속도로 입력을 읽는다 (파일을 최대 속도로 밀어내지 않도록).
        "-re",
        # 단일 MP4 무한 반복.
        "-stream_loop", "-1",
        "-i", str(config.input_path),
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-r", str(config.fps),
        "-b:v", f"{vb}k",
        "-maxrate", f"{vb}k",
        "-bufsize", f"{vb * 2}k",
        "-g", str(gop),
        "-keyint_min", str(gop),
        "-sc_threshold", "0",
        "-c:a", "aac",
        "-b:a", f"{config.audio_bitrate_kbps}k",
        "-ar", str(config.audio_sample_rate),
        "-ac", "2",
        "-progress", "pipe:1",
        "-nostats",
        "-f", output_format,
        target,
    ]


def redact_command(cmd: Sequence[str], secrets: Iterable[str | None]) -> list[str]:
    secrets = list(secrets)
    return [redact(str(a), secrets) for a in cmd]


def describe_live_command(cmd: Sequence[str], config: LiveConfig) -> str:
    """Dry-run 로그용 설명. Stream Key는 항상 가려진다."""
    safe = redact_command(cmd, [config.stream_key])
    lines = [
        "LIVE command prepared",
        f"input={config.input_path.name}",
        f"target={mask_output_url(config.ingest_url)}",
        f"stream_key={config.masked_key or '(없음)'}",
        f"video={config.video_bitrate_kbps}k fps={config.fps} keyframe={config.keyframe_seconds}s",
        f"audio=aac {config.audio_bitrate_kbps}k",
        "command=" + subprocess.list2cmdline(safe),
    ]
    return "\n".join(lines)


def prepare_live(*, ffmpeg: Path, config: LiveConfig, check_input: bool = True) -> tuple[list[str], str]:
    """Dry-run: 설정 검증 + 명령 생성 + 마스킹된 설명. 프로세스는 실행하지 않는다."""
    validate_live_config(config, check_input=check_input)
    cmd = build_live_command(ffmpeg=ffmpeg, config=config)
    return cmd, describe_live_command(cmd, config)


@dataclass(frozen=True)
class LiveStats:
    uptime_seconds: float = 0.0
    fps: float | None = None
    bitrate: str | None = None
    speed: float | None = None
    out_time_seconds: float | None = None
    frame: int | None = None


def _to_float(v: str | None) -> float | None:
    if v is None:
        return None
    v = v.strip().rstrip("x")
    if not v or v.upper() == "N/A":
        return None
    try:
        return float(v)
    except ValueError:
        return None


class ProgressParser:
    """FFmpeg `-progress pipe:1` key=value 블록을 LiveStats로 변환한다."""

    def __init__(self):
        self._block: dict[str, str] = {}
        self.latest: LiveStats | None = None

    def feed(self, line: str) -> LiveStats | None:
        line = line.strip()
        if "=" not in line:
            return None
        k, v = line.split("=", 1)
        self._block[k.strip()] = v.strip()
        if k.strip() != "progress":
            return None
        b, self._block = self._block, {}
        out_us = _to_float(b.get("out_time_us")) or _to_float(b.get("out_time_ms"))
        bitrate = b.get("bitrate")
        frame = _to_float(b.get("frame"))
        self.latest = LiveStats(
            fps=_to_float(b.get("fps")),
            bitrate=None if not bitrate or bitrate == "N/A" else bitrate,
            speed=_to_float(b.get("speed")),
            out_time_seconds=None if out_us is None else out_us / 1_000_000,
            frame=None if frame is None else int(frame),
        )
        return self.latest


class LiveProcess:
    """LIVE FFmpeg 프로세스 1개. stdout/stderr는 백그라운드 스레드가 읽어 GUI를 막지 않는다."""

    STOP_TIMEOUT = 5.0

    def __init__(self, cmd: Sequence[str], *, secrets: Iterable[str | None] = (), popen=subprocess.Popen):
        self._cmd = list(cmd)
        self._secrets = [s for s in secrets if s]
        self._popen = popen
        self._proc: subprocess.Popen | None = None
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._parser = ProgressParser()
        self._stderr: collections.deque[str] = collections.deque(maxlen=30)
        self._started_at: float | None = None
        self._ended_at: float | None = None

    def __repr__(self) -> str:
        return f"LiveProcess(running={self.is_running()}, rc={self.return_code()})"

    def start(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise RuntimeError("LIVE 프로세스가 이미 실행 중입니다.")
            self._parser = ProgressParser()
            self._stderr.clear()
            try:
                self._proc = self._popen(
                    self._cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=creationflags_no_window(),
                )
            except OSError as e:
                raise RuntimeError(redact(f"FFmpeg를 실행할 수 없습니다: {e}", self._secrets)) from None
            self._started_at = time.monotonic()
            self._ended_at = None
            self._threads = [
                threading.Thread(target=self._read_stdout, args=(self._proc.stdout,), daemon=True),
                threading.Thread(target=self._read_stderr, args=(self._proc.stderr,), daemon=True),
            ]
            for t in self._threads:
                t.start()
        log.info("LIVE ffmpeg started (pid=%s)", getattr(self._proc, "pid", "?"))

    def _read_stdout(self, stream) -> None:
        if stream is None:
            return
        try:
            for line in stream:
                self._parser.feed(line)
        except (OSError, ValueError):
            pass

    def _read_stderr(self, stream) -> None:
        if stream is None:
            return
        try:
            for line in stream:
                line = line.strip()
                if line:
                    self._stderr.append(redact(line, self._secrets))
        except (OSError, ValueError):
            pass

    def stop(self, timeout: float | None = None) -> int | None:
        timeout = self.STOP_TIMEOUT if timeout is None else timeout
        with self._lock:
            proc = self._proc
            if proc is None:
                return None
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                except OSError:
                    pass
            try:
                # 항상 wait()로 회수해 zombie가 남지 않게 한다.
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning("LIVE ffmpeg did not exit after kill")
            for t in self._threads:
                t.join(timeout=2)
            for s in (proc.stdout, proc.stderr):
                try:
                    if s:
                        s.close()
                except OSError:
                    pass
            self._mark_ended()
            return proc.returncode

    def _mark_ended(self) -> None:
        if self._started_at is not None and self._ended_at is None:
            self._ended_at = time.monotonic()

    def is_running(self) -> bool:
        if self._proc is None:
            return False
        if self._proc.poll() is None:
            return True
        self._mark_ended()
        return False

    def return_code(self) -> int | None:
        return None if self._proc is None else self._proc.poll()

    def uptime(self) -> float:
        if self._started_at is None:
            return 0.0
        end = self._ended_at if self._ended_at is not None else time.monotonic()
        return max(0.0, end - self._started_at)

    def stats(self) -> LiveStats:
        s = self._parser.latest or LiveStats()
        return LiveStats(self.uptime(), s.fps, s.bitrate, s.speed, s.out_time_seconds, s.frame)

    def recent_errors(self) -> list[str]:
        return list(self._stderr)
