#!/usr/bin/env python3
"""Long Live Cloud worker — Linux 서버에서 LIVE READY MP4(1개 또는 Playlist)를 YouTube로 DIRECT COPY 송출한다.

- Python 표준 라이브러리만 사용. Tkinter/Windows API 없음. 단일 파일로 배포된다.
- Python은 영상 frame을 읽지 않는다. FFmpeg stream copy만 실행한다 (재인코딩 없음).
- FFmpeg/YouTube 연결 문제 → 이 worker의 watchdog (5/10/30/60초 재접속)
- worker 자체 crash → systemd Restart=on-failure
- 서버 reboot → systemd enable 상태면 자동 복구
- Stream Key는 /etc/long-live/stream.key (0600)에서만 읽고 로그/status에 절대 쓰지 않는다.

설정 (/etc/long-live/live.json)
  v1: {"media": "FILE.mp4", "ingest_url": "rtmps://...", "mode": "copy"}
  v2: {"schema_version": 2, "media": ["01.mp4", "02.mp4"], "play_mode": "sequential", "ingest_url": "...",
       "mode": "copy", "session_mode": "continuous" | "archive_safe", "session_id": "..."}
  media가 1개면 기존 단일 파일 명령 그대로. 2개 이상이면 서버가 만든 ffconcat으로 A→B→C 무한 반복.

보관 안전 모드 (archive_safe): 세션 시작(서버 wall clock, state/session.json에 저장)부터 11시간 50분이 되면
  FFmpeg에 q → 정상 종료 → 재접속하지 않고 exit 0 (systemd Restart=on-failure는 재시작하지 않음).
  같은 session_id로 다시 시작돼도(재부팅/crash) 완료된 세션은 다시 송출하지 않는다 → PC에서 [다음 세션 시작] 필요.
상태: /opt/long-live/state/status.json (원자적 교체, 최근 정보만)
"""
from __future__ import annotations

import argparse
import collections
import hashlib
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

WORKER_VERSION = "2"
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
EXIT_SESSION_COMPLETE = 0  # 보관 안전 종료는 정상 종료 (systemd 재시작 없음)
EXIT_CONFIG = 3  # systemd RestartPreventExitStatus=3: 설정 오류는 재시작 반복하지 않음
ARCHIVE_SAFE_SECONDS = 11 * 3600 + 50 * 60  # 42600 (app/live_session.py와 같은 값)
MAX_PLAYLIST = 20
AAC_FRAME_SAMPLES = 1024
MASK = "********"
SAFE_MEDIA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.mp4$")
SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
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


def build_copy_command(ffmpeg: str, media: Path, target: str, *, concat: bool = False) -> list[str]:
    """DIRECT COPY (FFmpeg streamcopy). 인코더/필터 없음. concat=True면 media는 ffconcat manifest."""
    return [
        ffmpeg, "-hide_banner", "-loglevel", "warning",
        "-re", "-stream_loop", "-1",
        *(["-f", "concat", "-safe", "0"] if concat else []),
        "-i", str(media),
        "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "copy", "-c:a", "copy",
        "-progress", "pipe:1", "-nostats",
        "-f", "flv", target,
    ]


def escape_ffconcat_path(path) -> str:
    s = str(path)
    if "\n" in s or "\r" in s:
        raise ConfigError("파일 경로가 올바르지 않습니다.")
    return "'" + s.replace("'", "'\\''") + "'"


def build_ffconcat(entries) -> str:
    """entries: (서버 media 디렉터리 안의 검증된 절대 경로, 항목 길이)."""
    lines = ["ffconcat version 1.0"]
    for path, duration in entries:
        lines.append(f"file {escape_ffconcat_path(path)}")
        lines.append(f"duration {float(duration):.6f}")
    return "\n".join(lines) + "\n"


def playlist_position(out_time, durations):
    """송출 위치 → (현재 index 0-based, 회차 1-based). app/live_session.py와 같은 계산."""
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


def parse_config(cfg: dict) -> dict:
    """v1/v2 설정 → 정규화. media 문자열은 [문자열]로. 모든 이름은 SAFE_MEDIA만 허용."""
    if not isinstance(cfg, dict):
        raise ConfigError("설정 형식이 올바르지 않습니다.")
    if cfg.get("mode", "copy") != "copy":
        raise ConfigError("Cloud worker는 DIRECT COPY만 지원합니다 (LIVE READY 파일 필요).")
    media = cfg.get("media")
    names = [media] if isinstance(media, str) else media
    if not isinstance(names, list) or not names:
        raise ConfigError("영상 목록이 비어 있습니다.")
    if len(names) > MAX_PLAYLIST:
        raise ConfigError(f"Playlist는 최대 {MAX_PLAYLIST}개입니다.")
    for n in names:
        if not isinstance(n, str) or not SAFE_MEDIA.match(n):
            raise ConfigError("영상 파일 이름이 올바르지 않습니다.")
    if cfg.get("play_mode", "sequential") != "sequential":
        raise ConfigError("재생 순서는 sequential만 지원합니다.")
    session_mode = cfg.get("session_mode", "continuous")
    if session_mode not in ("continuous", "archive_safe"):
        raise ConfigError("세션 모드가 올바르지 않습니다.")
    sid = cfg.get("session_id")
    if sid is not None and (not isinstance(sid, str) or not SAFE_SESSION_ID.match(sid)):
        raise ConfigError("세션 ID 형식이 올바르지 않습니다.")
    if not sid:  # v1/ID 없음: 같은 설정이면 같은 세션으로 본다 (완료 후 재부팅 시 다시 송출 방지)
        sid = hashlib.sha256(json.dumps([names, cfg.get("ingest_url"), session_mode]).encode()).hexdigest()[:16]
    return {"media": names, "ingest_url": str(cfg.get("ingest_url") or ""), "session_mode": session_mode,
            "session_id": sid}


class Worker:
    def __init__(self, *, config_path, key_path, media_dir, state_dir, ffmpeg="ffmpeg", ffprobe=None,
                 retry_delays=RETRY_DELAYS, debug_output=None, clock=time.monotonic, wall=time.time,
                 session_limit=ARCHIVE_SAFE_SECONDS):
        self.config_path = Path(config_path)
        self.key_path = Path(key_path)
        self.media_dir = Path(media_dir)
        self.state_dir = Path(state_dir)
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe or self._default_ffprobe(ffmpeg)
        self.retry_delays = tuple(retry_delays)
        self.debug_output = debug_output  # 테스트 전용: RTMP 대신 로컬 파일 출력
        self.clock = clock
        self.wall = wall
        self.archive_limit = float(session_limit)
        self.stop_event = threading.Event()
        self.secrets: list[str] = []
        self.errors = collections.deque(maxlen=30)
        self.state_history = collections.deque(maxlen=100)
        self.reconnect_history = collections.deque(maxlen=100)
        self.progress: dict[str, str] = {}
        self.state = "STOPPED"
        self.media_names: list[str] = []
        self.durations: list[float] = []
        self.session_mode = "continuous"
        self.session_id = ""
        self.session_wall_start: float | None = None
        self.reconnects = 0
        self.attempt = 0
        self.last_exit_code = None
        self.last_error = ""
        self.retry_at = None
        self.proc: subprocess.Popen | None = None
        self._last_status_write = 0.0

    @staticmethod
    def _default_ffprobe(ffmpeg) -> str:
        sib = Path(str(ffmpeg)).with_name("ffprobe")
        return str(sib) if sib.is_file() else (shutil.which("ffprobe") or "ffprobe")

    @property
    def media_name(self) -> str:
        return self.media_names[0] if len(self.media_names) == 1 else ""

    # ---------- config ----------
    def load(self) -> tuple[Path, str, bool]:
        """(입력 경로, 송출 대상, concat 여부)."""
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ConfigError(f"설정 파일을 읽을 수 없습니다: {self.config_path}") from e
        cfg = parse_config(raw)
        paths = []
        for name in cfg["media"]:
            media = self.media_dir / name
            if not media.is_file() or media.stat().st_size == 0:
                raise ConfigError(f"영상 파일이 서버에 없습니다: {name}")
            paths.append(media)
        try:
            key = self.key_path.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise ConfigError("Stream Key 파일을 읽을 수 없습니다.") from e
        if not key:
            raise ConfigError("Stream Key가 비어 있습니다.")
        self.secrets[:] = [key]
        target = build_output_url(cfg["ingest_url"], key)
        self.media_names = cfg["media"]
        self.session_mode = cfg["session_mode"]
        self.session_id = cfg["session_id"]
        if len(paths) == 1:
            self.durations = []
            return paths[0], (self.debug_output or target), False
        entries = [(p.resolve(), self._entry_duration(p)) for p in paths]
        self.durations = [d for _, d in entries]
        manifest = self.state_dir / "playlist.ffconcat"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        part = manifest.with_name(manifest.name + ".part")
        part.write_text(build_ffconcat(entries), encoding="utf-8", newline="\n")
        os.replace(part, manifest)
        return manifest, (self.debug_output or target), True

    def _entry_duration(self, path: Path) -> float:
        """파일 길이 + AAC 1프레임 (경계 DTS 겹침 방지, PC 쪽 live_playlist와 같은 규칙). ffprobe 메타데이터만 읽음."""
        try:
            out = subprocess.run([self.ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,sample_rate",
                                  "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
            data = json.loads(out.stdout or "{}")
            duration = float(data["format"]["duration"])
            rate = next((int(s.get("sample_rate") or 0) for s in data.get("streams", []) if s.get("codec_type") == "audio"), 0)
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as e:
            raise ConfigError(f"영상 정보를 읽을 수 없습니다: {path.name}") from e
        if duration <= 0:
            raise ConfigError(f"영상 길이가 0초입니다: {path.name}")
        return duration + AAC_FRAME_SAMPLES / (rate or 44100)

    # ---------- session (보관 안전 모드) ----------
    @property
    def session_file(self) -> Path:
        return self.state_dir / "session.json"

    def _read_session(self) -> dict | None:
        try:
            d = json.loads(self.session_file.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else None
        except (OSError, ValueError):
            return None

    def _write_session(self, data: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        part = self.session_file.with_name("session.json.part")
        part.write_text(json.dumps(data), encoding="utf-8")
        os.replace(part, self.session_file)

    def _begin_session(self) -> bool:
        """False면 이미 완료된 세션 → 송출하지 않는다."""
        old = self._read_session()
        if old and old.get("session_id") == self.session_id:
            if old.get("complete"):
                return False
            self.session_wall_start = float(old.get("started_at") or self.wall())  # crash/재부팅 후 같은 세션 이어서 계산
        else:
            self.session_wall_start = self.wall()
            self._write_session({"session_id": self.session_id, "started_at": self.session_wall_start,
                                 "session_mode": self.session_mode, "complete": False})
        return True

    @property
    def session_limit(self) -> float | None:
        return self.archive_limit if self.session_mode == "archive_safe" else None

    def session_elapsed(self) -> float:
        return max(0.0, self.wall() - self.session_wall_start) if self.session_wall_start else 0.0

    def session_remaining(self) -> float | None:
        lim = self.session_limit
        return None if lim is None else max(0.0, lim - self.session_elapsed())

    def _session_over(self) -> bool:
        lim = self.session_limit
        return lim is not None and self.session_elapsed() >= lim

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
        out_time = (_num(p.get("out_time_us")) or 0) / 1_000_000 if p.get("out_time_us") else None
        pos = playlist_position(out_time, self.durations) if self.durations else None
        n = len(self.media_names)
        current = (self.media_names[pos[0]] if pos else "") if n > 1 else self.media_name
        return {
            "worker_version": WORKER_VERSION,
            "state": self.state,
            "media": current,
            "mode": "DIRECT COPY",
            "started_at": self.session_wall_start,
            "runtime_seconds": round(self.session_elapsed(), 1),
            "fps": _num(p.get("fps")),
            "bitrate": p.get("bitrate") if p.get("bitrate") not in (None, "N/A") else None,
            "speed": _num(p.get("speed")),
            "out_time_seconds": out_time,
            "playlist_count": n,
            "current_playlist_index": pos[0] if pos else (0 if n == 1 else None),
            "current_media": current,
            "playlist_round": pos[1] if pos else None,
            "session_mode": self.session_mode,
            "session_limit": self.session_limit,
            "session_remaining": None if self.session_remaining() is None else round(self.session_remaining(), 1),
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

    def _start_ffmpeg(self, media: Path, target: str, concat: bool = False):
        self.progress = {}
        cmd = build_copy_command(self.ffmpeg, media, target, concat=concat)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1)
        self._threads = self._readers(self.proc)
        log.info("ffmpeg started pid=%s items=%d", self.proc.pid, len(self.media_names))

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

    def _finish_session(self) -> int:
        """11:50 도달: q 정상 종료, 세션 완료 기록, 재접속 없이 exit 0."""
        if self.proc is not None:
            self._set_state("STOPPING", "archive safe limit")
            self.last_exit_code = self._stop_ffmpeg()
        self.retry_at = None
        self._write_session({"session_id": self.session_id, "started_at": self.session_wall_start,
                             "session_mode": self.session_mode, "complete": True, "completed_at": self.wall()})
        self._set_state("SESSION_LIMIT_REACHED", "보관 안전 종료 — 다음 세션 대기")
        return EXIT_SESSION_COMPLETE

    def _wait(self, seconds: float) -> bool:
        """stop 요청 또는 세션 한도까지 기다림. True = 대기 중단(stop/세션 종료)."""
        rem = self.session_remaining()
        step = seconds if rem is None else max(0.0, min(seconds, rem))
        return self.stop_event.wait(step) or self._session_over()

    # ---------- main loop ----------
    def run(self) -> int:
        try:
            media, target, concat = self.load()
        except ConfigError as e:
            self.last_error = str(e)
            self._set_state("FAILED", str(e))
            return EXIT_CONFIG
        if not self._begin_session():
            self._set_state("SESSION_LIMIT_REACHED", "이미 완료된 보관 안전 세션 — 다음 세션 대기")
            return EXIT_SESSION_COMPLETE
        try:
            while not self.stop_event.is_set():
                if self._session_over():
                    return self._finish_session()
                self._set_state("STARTING")
                try:
                    self._start_ffmpeg(media, target, concat)
                except OSError as e:
                    self.last_error = redact(f"FFmpeg를 실행할 수 없습니다: {e}", self.secrets)
                    self._set_state("FAILED", self.last_error)
                    return EXIT_CONFIG
                run_started = self.clock()
                self.retry_at = None
                self._set_state("RUNNING")
                while not self._wait(1.0):
                    if self.proc.poll() is not None:
                        break
                    self.write_status()
                if self.stop_event.is_set():
                    break
                if self._session_over():  # 한도 도달: 재접속 금지
                    return self._finish_session()
                self.last_exit_code = self._stop_ffmpeg()
                self.last_error = self.errors[-1] if self.errors else f"FFmpeg 종료 (코드 {self.last_exit_code})"
                if self.clock() - run_started >= STABLE_RESET_SECONDS:
                    self.attempt = 0
                delay = retry_delay(self.attempt, self.retry_delays)
                self.attempt += 1
                self.retry_at = self.clock() + delay
                self.reconnect_history.append((time.time(), self.last_exit_code, delay))
                self._set_state("RECONNECT_WAIT", f"rc={self.last_exit_code} retry_in={delay}s")
                if self._wait(delay):
                    if self.stop_event.is_set():
                        break
                    return self._finish_session()
                self.reconnects += 1
        finally:
            if self.proc is not None:
                self._set_state("STOPPING")
                self.last_exit_code = self._stop_ffmpeg()
            self.retry_at = None
            if self.state not in ("FAILED", "SESSION_LIMIT_REACHED"):
                self._set_state("STOPPED")
        return EXIT_OK


def self_check(args) -> int:
    ffmpeg = shutil.which(args.ffmpeg) or args.ffmpeg
    info = {"python": sys.version.split()[0], "ffmpeg": None, "dirs": {}, "worker_version": WORKER_VERSION}
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
