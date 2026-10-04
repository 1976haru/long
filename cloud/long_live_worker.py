#!/usr/bin/env python3
"""Long Live Cloud worker — Linux 서버에서 LIVE READY MP4 1개를 YouTube로 24시간 DIRECT COPY 송출한다.

- Python 표준 라이브러리만 사용. Tkinter/Windows API 없음. 단일 파일로 배포된다.
- Python은 영상 frame을 읽지 않는다. FFmpeg stream copy만 실행한다 (재인코딩 없음).
- FFmpeg/YouTube 연결 문제 → 이 worker의 watchdog (5/10/30/60초 재접속)
- worker 자체 crash → systemd Restart=on-failure
- 서버 reboot → systemd enable 상태면 자동 복구
- Stream Key는 /etc/long-live/stream.key (0600)에서만 읽고 로그/status에 절대 쓰지 않는다.

설정: /etc/long-live/live.json  {"media": "FILE_LIVE_READY.mp4", "ingest_url": "rtmps://...", "mode": "copy"}
상태: /opt/long-live/state/status.json (원자적 교체, 최근 정보만)
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

WORKER_VERSION = "1"
DEFAULT_CONFIG = "/etc/long-live/live.json"
DEFAULT_KEY = "/etc/long-live/stream.key"
DEFAULT_MEDIA = "/opt/long-live/media"
DEFAULT_STATE = "/opt/long-live/state"
DEFAULT_LOGS = "/opt/long-live/logs"
RETRY_DELAYS = (5, 10, 30, 60)
STABLE_RESET_SECONDS = 60.0
STATUS_INTERVAL = 5.0
GRACEFUL_TIMEOUT = 5.0
TERMINATE_TIMEOUT = 3.0
EXIT_OK = 0
EXIT_CONFIG = 3  # systemd RestartPreventExitStatus=3: 설정 오류는 재시작 반복하지 않음
MASK = "********"
SAFE_MEDIA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.mp4$")
KEY_FORBIDDEN = set("/?#\\")

log = logging.getLogger("long-live")


class ConfigError(Exception):
    pass


class RedactFilter(logging.Filter):
    def __init__(self, secrets):
        super().__init__()
        self.secrets = secrets  # list, 갱신 가능

    def filter(self, record):
        msg = record.getMessage()
        for s in self.secrets:
            if s:
                msg = msg.replace(s, MASK)
        record.msg, record.args = msg, None
        return True


def redact(text: str, secrets) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, MASK)
    return text


def build_output_url(ingest_url: str, key: str) -> str:
    url, key = (ingest_url or "").strip(), (key or "").strip()
    if not url or not key:
        raise ConfigError("송출 주소 또는 Stream Key가 비어 있습니다.")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("rtmp", "rtmps") or not parts.hostname or parts.query or parts.fragment:
        raise ConfigError("송출 주소는 rtmp:// 또는 rtmps:// 여야 합니다.")
    if any(c.isspace() or not c.isprintable() or c in KEY_FORBIDDEN for c in key):
        raise ConfigError("Stream Key 형식이 올바르지 않습니다.")
    return f"{url.rstrip('/')}/{key}"


def build_copy_command(ffmpeg: str, media: Path, target: str) -> list[str]:
    """DIRECT COPY (FFmpeg streamcopy). 인코더/필터 없음."""
    return [
        ffmpeg, "-hide_banner", "-loglevel", "warning",
        "-re", "-stream_loop", "-1", "-i", str(media),
        "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "copy", "-c:a", "copy",
        "-progress", "pipe:1", "-nostats",
        "-f", "flv", target,
    ]


def retry_delay(attempt: int, delays=RETRY_DELAYS) -> float:
    return delays[min(max(attempt, 0), len(delays) - 1)]


def _num(v):
    if v is None:
        return None
    v = v.strip().rstrip("x")
    try:
        return float(v)
    except ValueError:
        return None


class Worker:
    def __init__(self, *, config_path, key_path, media_dir, state_dir, ffmpeg="ffmpeg",
                 retry_delays=RETRY_DELAYS, debug_output=None, clock=time.monotonic):
        self.config_path = Path(config_path)
        self.key_path = Path(key_path)
        self.media_dir = Path(media_dir)
        self.state_dir = Path(state_dir)
        self.ffmpeg = ffmpeg
        self.retry_delays = tuple(retry_delays)
        self.debug_output = debug_output  # 테스트 전용: RTMP 대신 로컬 파일 출력
        self.clock = clock
        self.stop_event = threading.Event()
        self.secrets: list[str] = []
        self.errors = collections.deque(maxlen=30)
        self.state_history = collections.deque(maxlen=100)
        self.reconnect_history = collections.deque(maxlen=100)
        self.progress: dict[str, str] = {}
        self.state = "STOPPED"
        self.media_name = ""
        self.reconnects = 0
        self.attempt = 0
        self.last_exit_code = None
        self.last_error = ""
        self.retry_at = None
        self.session_started = None
        self.started_wall = None
        self.proc: subprocess.Popen | None = None
        self._last_status_write = 0.0

    # ---------- config ----------
    def load(self) -> tuple[Path, str]:
        try:
            cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ConfigError(f"설정 파일을 읽을 수 없습니다: {self.config_path}") from e
        if cfg.get("mode", "copy") != "copy":
            raise ConfigError("Cloud worker는 DIRECT COPY만 지원합니다 (LIVE READY 파일 필요).")
        name = str(cfg.get("media") or "")
        if not SAFE_MEDIA.match(name):
            raise ConfigError("영상 파일 이름이 올바르지 않습니다.")
        media = self.media_dir / name
        if not media.is_file() or media.stat().st_size == 0:
            raise ConfigError(f"영상 파일이 서버에 없습니다: {name}")
        try:
            key = self.key_path.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise ConfigError("Stream Key 파일을 읽을 수 없습니다.") from e
        if not key:
            raise ConfigError("Stream Key가 비어 있습니다.")
        self.secrets[:] = [key]
        target = build_output_url(str(cfg.get("ingest_url") or ""), key)
        self.media_name = name
        return media, (self.debug_output or target)

    # ---------- status ----------
    def _set_state(self, state: str, message: str = "") -> None:
        self.state = state
        message = redact(message, self.secrets)
        self.state_history.append((time.time(), state, message))
        log.info("state=%s %s", state, message)
        self.write_status(force=True)

    def snapshot(self) -> dict:
        p = self.progress
        now = self.clock()
        try:
            disk_free = shutil.disk_usage(self.media_dir).free
        except OSError:
            disk_free = None
        return {
            "worker_version": WORKER_VERSION,
            "state": self.state,
            "media": self.media_name,
            "mode": "DIRECT COPY",
            "started_at": self.started_wall,
            "runtime_seconds": round(now - self.session_started, 1) if self.session_started else 0,
            "fps": _num(p.get("fps")),
            "bitrate": p.get("bitrate") if p.get("bitrate") not in (None, "N/A") else None,
            "speed": _num(p.get("speed")),
            "out_time_seconds": (_num(p.get("out_time_us")) or 0) / 1_000_000 if p.get("out_time_us") else None,
            "reconnects": self.reconnects,
            "retry_in": round(max(0.0, self.retry_at - now), 1) if self.retry_at else None,
            "last_exit_code": self.last_exit_code,
            "last_error": redact(self.last_error, self.secrets),
            "recent_errors": [redact(e, self.secrets) for e in list(self.errors)[-10:]],
            "disk_free_bytes": disk_free,
            "pid": os.getpid(),
            "updated_at": time.time(),
        }

    def write_status(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._last_status_write < STATUS_INTERVAL:
            return
        self._last_status_write = now
        data = json.dumps(self.snapshot(), ensure_ascii=False)
        for s in self.secrets:
            if s and s in data:  # 이중 안전장치
                data = data.replace(s, MASK)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.state_dir / "status.json.part"
            tmp.write_text(data, encoding="utf-8")
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.state_dir / "status.json")
        except OSError:
            log.warning("status write failed")

    # ---------- ffmpeg ----------
    def _readers(self, proc):
        def out():
            try:
                for line in proc.stdout:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        self.progress[k] = v
            except (OSError, ValueError):
                pass

        def err():
            try:
                for line in proc.stderr:
                    line = redact(line.strip(), self.secrets)
                    if line:
                        self.errors.append(line[:300])
            except (OSError, ValueError):
                pass
        ts = [threading.Thread(target=out, daemon=True), threading.Thread(target=err, daemon=True)]
        for t in ts:
            t.start()
        return ts

    def _start_ffmpeg(self, media: Path, target: str):
        self.progress = {}
        cmd = build_copy_command(self.ffmpeg, media, target)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1)
        self._threads = self._readers(self.proc)
        log.info("ffmpeg started pid=%s media=%s", self.proc.pid, media.name)

    def _stop_ffmpeg(self) -> int | None:
        proc = self.proc
        if proc is None:
            return None
        if proc.poll() is None:
            try:
                proc.stdin.write("q")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(GRACEFUL_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(TERMINATE_TIMEOUT)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(TERMINATE_TIMEOUT)
        for t in getattr(self, "_threads", []):
            t.join(2)
        for s in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if s:
                    s.close()
            except (OSError, ValueError):
                pass
        self.proc = None
        return proc.returncode

    def request_stop(self) -> None:
        self.stop_event.set()

    # ---------- main loop ----------
    def run(self) -> int:
        try:
            media, target = self.load()
        except ConfigError as e:
            self.last_error = str(e)
            self._set_state("FAILED", str(e))
            return EXIT_CONFIG
        self.session_started = self.clock()
        self.started_wall = time.time()
        try:
            while not self.stop_event.is_set():
                self._set_state("STARTING")
                try:
                    self._start_ffmpeg(media, target)
                except OSError as e:
                    self.last_error = redact(f"FFmpeg를 실행할 수 없습니다: {e}", self.secrets)
                    self._set_state("FAILED", self.last_error)
                    return EXIT_CONFIG
                run_started = self.clock()
                self.retry_at = None
                self._set_state("RUNNING")
                while not self.stop_event.wait(1.0):
                    if self.proc.poll() is not None:
                        break
                    self.write_status()
                if self.stop_event.is_set():
                    break
                self.last_exit_code = self._stop_ffmpeg()
                self.last_error = self.errors[-1] if self.errors else f"FFmpeg 종료 (코드 {self.last_exit_code})"
                if self.clock() - run_started >= STABLE_RESET_SECONDS:
                    self.attempt = 0
                delay = retry_delay(self.attempt, self.retry_delays)
                self.attempt += 1
                self.retry_at = self.clock() + delay
                self.reconnect_history.append((time.time(), self.last_exit_code, delay))
                self._set_state("RECONNECT_WAIT", f"rc={self.last_exit_code} retry_in={delay}s")
                if self.stop_event.wait(delay):
                    break
                self.reconnects += 1
        finally:
            if self.proc is not None:
                self._set_state("STOPPING")
                self.last_exit_code = self._stop_ffmpeg()
            self.retry_at = None
            if self.state != "FAILED":
                self._set_state("STOPPED")
        return EXIT_OK


def self_check(args) -> int:
    ffmpeg = shutil.which(args.ffmpeg) or args.ffmpeg
    info = {"python": sys.version.split()[0], "ffmpeg": None, "dirs": {}}
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-version"], capture_output=True, text=True, timeout=20)
        info["ffmpeg"] = out.stdout.splitlines()[0] if out.returncode == 0 and out.stdout else None
    except (OSError, subprocess.TimeoutExpired):
        pass
    for name, p in (("media", args.media_dir), ("state", args.state_dir), ("logs", args.log_dir)):
        info["dirs"][name] = {"exists": os.path.isdir(p), "writable": os.access(p, os.W_OK)}
    print(json.dumps(info))
    ok = bool(info["ffmpeg"]) and info["dirs"]["state"]["writable"] and info["dirs"]["media"]["exists"]
    return 0 if ok else 1


def setup_logging(log_dir: str, secrets) -> None:
    log.setLevel(logging.INFO)
    f = RedactFilter(secrets)
    h = logging.StreamHandler(sys.stderr)  # journald
    h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    h.addFilter(f)
    log.addHandler(h)
    try:
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(os.path.join(log_dir, "worker.log"), maxBytes=512 * 1024,
                                                  backupCount=2, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        fh.addFilter(f)
        log.addHandler(fh)
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Long Live cloud worker")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--key-file", default=DEFAULT_KEY)
    ap.add_argument("--media-dir", default=DEFAULT_MEDIA)
    ap.add_argument("--state-dir", default=DEFAULT_STATE)
    ap.add_argument("--log-dir", default=DEFAULT_LOGS)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    args = ap.parse_args(argv)
    if args.self_check:
        return self_check(args)
    worker = Worker(config_path=args.config, key_path=args.key_file, media_dir=args.media_dir,
                    state_dir=args.state_dir, ffmpeg=shutil.which(args.ffmpeg) or args.ffmpeg)
    setup_logging(args.log_dir, worker.secrets)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: worker.request_stop())
    return worker.run()


if __name__ == "__main__":
    sys.exit(main())
