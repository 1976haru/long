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

예약 LIVE (v3, --scheduler / long-live-scheduler.service): PC가 꺼져 있어도 예약 시각에 송출을 시작한다.
  예약 작업(job)은 PC가 `sudo python3 worker --add-job`(stdin JSON)으로 /etc/long-live/jobs/<job_id>.json에 저장한다.
  job에는 Stream Key가 없다 (key_fingerprint = SHA256 앞 16자리만, 기존 stream.key와 같은지 확인용).
  scheduler 상태는 /opt/long-live/state/jobs/<job_id>.state.json. T-120초 영상 SHA256 확인 → T-30초 최종 확인
  → T(예약 시각)에 FFmpeg 송출 시작 (YouTube enableAutoStart가 방송을 LIVE로) → stop_at에 q 정상 종료 (enableAutoStop).
  예약 시각 + 5분이 지나도록 시작 못 했으면 MISSED (늦게 시작하지 않음). 같은 job은 한 번만 실행 (worker.lock).
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
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

try:  # Linux 서버
    import fcntl
except ImportError:  # Windows 테스트 환경: 같은 프로세스 안의 잠금만 (서버는 항상 fcntl)
    fcntl = None
_LOCAL_LOCKS: set[str] = set()
_LOCAL_LOCKS_GUARD = threading.Lock()

WORKER_VERSION = "3"
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


class FileLock:
    """프로세스 간 배타 잠금 (non-blocking). 같은 파일을 두 번 잡을 수 없다 → FFmpeg 송출/scheduler 중복 실행 방지.
    프로세스가 죽으면 OS가 잠금을 풀어 준다 (stale lock 없음)."""

    def __init__(self, path):
        self.path = Path(path)
        self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return False
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                with _LOCAL_LOCKS_GUARD:
                    key = os.path.normcase(os.path.abspath(str(self.path)))
                    if key in _LOCAL_LOCKS:
                        raise OSError("locked")
                    _LOCAL_LOCKS.add(key)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                with _LOCAL_LOCKS_GUARD:
                    _LOCAL_LOCKS.discard(os.path.normcase(os.path.abspath(str(self.path))))
        except OSError:
            pass
        os.close(fd)


LOCK_BUSY = "다른 LIVE 송출이 이미 실행 중입니다 (예약 LIVE 또는 직접 시작한 Cloud LIVE)."


class Worker:
    def __init__(self, *, config_path, key_path, media_dir, state_dir, ffmpeg="ffmpeg", ffprobe=None,
                 retry_delays=RETRY_DELAYS, debug_output=None, clock=time.monotonic, wall=time.time,
                 session_limit=ARCHIVE_SAFE_SECONDS, deadline_wall: float | None = None, lock_path=None):
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
        # 예약 LIVE: 이 wall 시각(stop_at)에 q 정상 종료 → 세션 완료 (재접속/재시작 없음)
        self.deadline = None if deadline_wall is None else float(deadline_wall)
        # 같은 서버에서 FFmpeg 송출은 하나만 (수동 Cloud LIVE와 예약 LIVE가 같은 잠금 파일을 쓴다)
        self.lock = FileLock(lock_path if lock_path is not None else self.state_dir / "worker.lock")
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
        rems = []
        lim = self.session_limit
        if lim is not None:
            rems.append(lim - self.session_elapsed())
        if self.deadline is not None:
            rems.append(self.deadline - self.wall())
        return None if not rems else max(0.0, min(rems))

    def _session_over(self) -> bool:
        rem = self.session_remaining()
        return rem is not None and rem <= 0

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
        scheduled_end = self.deadline is not None and self.wall() >= self.deadline
        if self.proc is not None:
            self._set_state("STOPPING", "scheduled stop time" if scheduled_end else "archive safe limit")
            self.last_exit_code = self._stop_ffmpeg()
        self.retry_at = None
        self._write_session({"session_id": self.session_id, "started_at": self.session_wall_start,
                             "session_mode": self.session_mode, "complete": True, "completed_at": self.wall()})
        self._set_state("SESSION_LIMIT_REACHED", "예약 LIVE 종료 시각 — 정상 종료" if scheduled_end
                        else "보관 안전 종료 — 다음 세션 대기")
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
        if not self.lock.acquire():
            self.last_error = LOCK_BUSY
            self._set_state("FAILED", LOCK_BUSY)
            return EXIT_CONFIG
        try:
            return self._run_locked(media, target, concat)
        finally:
            self.lock.release()

    def _run_locked(self, media, target, concat) -> int:
        if not self._begin_session():
            self._set_state("SESSION_LIMIT_REACHED", "이미 완료된 세션 — 다시 송출하지 않음")
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


# ======================= 예약 LIVE (Cloud scheduler, v3) =======================

DEFAULT_JOBS = "/etc/long-live/jobs"
JOB_SCHEMA = 1
SCHED_IDLE_SECONDS = 30.0  # 대기 중 최대 sleep (busy polling 없음)
SCHED_RUNNING_SECONDS = 5.0  # 송출 중 상태(heartbeat) 기록 간격
PREPARE_LEAD = 120.0  # T-120초: 영상/SHA256/manifest 확인
PREFLIGHT_LEAD = 30.0  # T-30초: 최종 확인 (파일/Stream Key)
LATE_GRACE_SECONDS = 300  # 예약 시각 + 5분까지만 늦은 시작 허용, 넘으면 MISSED
MAX_LATE_GRACE = 900
MAX_JOB_SECONDS = 12 * 3600
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SHA_HEX = re.compile(r"^[0-9a-f]{64}$")
FP_HEX = re.compile(r"^[0-9a-f]{16}$")

J_PENDING, J_PREPARING, J_STARTING, J_LIVE = "PENDING", "PREPARING", "STARTING", "LIVE"
J_STOPPING, J_COMPLETE, J_FAILED, J_CANCELLED, J_MISSED = "STOPPING", "COMPLETE", "FAILED", "CANCELLED", "MISSED"
JOB_TERMINAL = frozenset((J_COMPLETE, J_FAILED, J_CANCELLED, J_MISSED))
JOB_ACTIVE = frozenset((J_STARTING, J_LIVE, J_STOPPING))


def key_fingerprint(key: str) -> str:
    """Stream Key 확인용 지문 (SHA256 앞 16자리). key 자체는 job/상태/로그에 저장하지 않는다."""
    return hashlib.sha256((key or "").strip().encode("utf-8")).hexdigest()[:16]


def parse_utc(value) -> float:
    s = str(value or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ConfigError("예약 시각 형식이 올바르지 않습니다.") from None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ConfigError("예약 시각에 시간대(UTC)가 없습니다.")
    return dt.timestamp()


def parse_job(d) -> dict:
    """PC가 보낸 예약 job 검증 → 정규화 (start/stop은 epoch 초). 비밀 값은 받지 않는다."""
    if not isinstance(d, dict) or d.get("schema") != JOB_SCHEMA:
        raise ConfigError("예약 작업 형식이 올바르지 않습니다.")
    if any(k in d for k in ("stream_key", "key", "streamName")):
        raise ConfigError("예약 작업에 Stream Key를 넣을 수 없습니다.")
    jid = d.get("job_id")
    if not isinstance(jid, str) or not SAFE_ID.match(jid):
        raise ConfigError("예약 작업 ID 형식이 올바르지 않습니다.")
    for k in ("broadcast_id", "stream_id"):
        v = d.get(k, "")
        if not isinstance(v, str) or (v and not SAFE_ID.match(v)):
            raise ConfigError("YouTube 방송/스트림 ID 형식이 올바르지 않습니다.")
    start, stop = parse_utc(d.get("scheduled_at_utc")), parse_utc(d.get("stop_at_utc"))
    if not 0 < stop - start <= MAX_JOB_SECONDS:
        raise ConfigError("방송 길이는 1초 ~ 12시간이어야 합니다.")
    items = d.get("playlist")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_PLAYLIST:
        raise ConfigError(f"Playlist는 1~{MAX_PLAYLIST}개입니다.")
    playlist = []
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("name"), str) or not SAFE_MEDIA.match(it["name"]):
            raise ConfigError("영상 파일 이름이 올바르지 않습니다.")
        sha = str(it.get("sha256") or "")
        if sha and not SHA_HEX.match(sha):
            raise ConfigError("영상 SHA256 형식이 올바르지 않습니다.")
        size = it.get("size")
        if size is not None and (not isinstance(size, int) or size <= 0):
            raise ConfigError("영상 크기 형식이 올바르지 않습니다.")
        playlist.append({"name": it["name"], "sha256": sha, "size": size})
    if d.get("ingest_mode", "copy") != "copy":
        raise ConfigError("예약 LIVE는 DIRECT COPY만 지원합니다.")
    ingest = str(d.get("ingest_url") or "")
    parts = urlsplit(ingest)
    if parts.scheme.lower() not in ("rtmp", "rtmps") or not parts.hostname or parts.query or parts.fragment:
        raise ConfigError("송출 주소는 rtmp:// 또는 rtmps:// 여야 합니다.")
    fp = str(d.get("key_fingerprint") or "")
    if fp and not FP_HEX.match(fp):
        raise ConfigError("Stream Key 지문 형식이 올바르지 않습니다.")
    grace = d.get("grace_seconds", LATE_GRACE_SECONDS)
    if not isinstance(grace, int) or not 0 <= grace <= MAX_LATE_GRACE:
        raise ConfigError("늦은 시작 허용 시간이 올바르지 않습니다.")
    return {"schema": JOB_SCHEMA, "job_id": jid, "broadcast_id": d.get("broadcast_id", ""),
            "stream_id": d.get("stream_id", ""), "scheduled_at_utc": d["scheduled_at_utc"],
            "stop_at_utc": d["stop_at_utc"], "start": start, "stop": stop, "playlist": playlist,
            "ingest_url": ingest, "ingest_mode": "copy", "key_fingerprint": fp, "grace": grace,
            "created_at": str(d.get("created_at") or "")}


def _atomic_write(path: Path, text: str, mode: int = 0o644, group: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(text, encoding="utf-8", newline="\n")
    try:
        os.chmod(part, mode)
        if group and hasattr(os, "geteuid") and os.geteuid() == 0:
            import grp
            os.chown(part, 0, grp.getgrnam(group).gr_gid)
    except (OSError, KeyError, ImportError):
        pass
    os.replace(part, path)


class JobStore:
    """job 명세(/etc, PC가 root로 저장, scheduler는 읽기만) + job 상태(/opt/long-live/state/jobs, scheduler가 기록)."""

    def __init__(self, jobs_dir, state_dir):
        self.jobs_dir = Path(jobs_dir)
        self.state_root = Path(state_dir) / "jobs"

    def spec_path(self, jid: str) -> Path:
        return self.jobs_dir / f"{jid}.json"

    def cancel_path(self, jid: str) -> Path:
        return self.jobs_dir / f"{jid}.cancel"

    def state_path(self, jid: str) -> Path:
        return self.state_root / f"{jid}.state.json"

    def run_dir(self, jid: str) -> Path:
        return self.state_root / jid

    def specs(self) -> list[dict]:
        try:
            files = sorted(self.jobs_dir.glob("*.json"))
        except OSError:
            return []
        out = []
        for p in files:
            try:
                out.append(parse_job(json.loads(p.read_text(encoding="utf-8"))))
            except (OSError, ValueError, ConfigError):
                log.warning("invalid job file %s", p.name)
        return sorted(out, key=lambda j: (j["start"], j["job_id"]))

    def read_state(self, jid: str) -> dict:
        try:
            d = json.loads(self.state_path(jid).read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def write_state(self, jid: str, data: dict) -> None:
        _atomic_write(self.state_path(jid), json.dumps(data, ensure_ascii=False))

    def cancel_requested(self, jid: str) -> bool:
        return self.cancel_path(jid).exists()

    # ---- 관리 명령 (PC → sudo python3 worker --add-job/--cancel-job/--list-jobs) ----
    def add(self, raw: dict, media_dir) -> dict:
        job = parse_job(raw)
        media_dir = Path(media_dir)
        for it in job["playlist"]:
            p = media_dir / it["name"]
            if not p.is_file() or p.stat().st_size == 0:
                raise ConfigError(f"Cloud에 영상이 없습니다: {it['name']}")
            if it["size"] is not None and p.stat().st_size != it["size"]:
                raise ConfigError(f"Cloud 영상 크기가 다릅니다: {it['name']}")
        jid = job["job_id"]
        if self.spec_path(jid).exists() and self.read_state(jid).get("state", J_PENDING) != J_PENDING:
            raise ConfigError("이미 시작되었거나 끝난 예약은 바꿀 수 없습니다.")
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        stored = {k: raw[k] for k in ("schema", "job_id", "broadcast_id", "stream_id", "scheduled_at_utc",
                                      "stop_at_utc", "playlist", "ingest_url", "ingest_mode", "key_fingerprint",
                                      "grace_seconds", "created_at") if k in raw}
        _atomic_write(self.spec_path(jid), json.dumps(stored, ensure_ascii=False), 0o640, "longlive")
        return self.describe(job, media_dir)

    def request_cancel(self, jid: str) -> dict:
        if not SAFE_ID.match(jid or "") or not self.spec_path(jid).exists():
            raise ConfigError("예약 작업을 찾을 수 없습니다.")
        _atomic_write(self.cancel_path(jid), json.dumps({"cancelled_at": time.time()}), 0o640, "longlive")
        return {"job_id": jid, "cancel_requested": True, "state": self.read_state(jid).get("state", J_PENDING)}

    def describe(self, job: dict, media_dir) -> dict:
        st = self.read_state(job["job_id"])
        media_dir = Path(media_dir)
        media_ok = all((media_dir / it["name"]).is_file() for it in job["playlist"])
        return {"job_id": job["job_id"], "broadcast_id": job["broadcast_id"], "stream_id": job["stream_id"],
                "scheduled_at_utc": job["scheduled_at_utc"], "stop_at_utc": job["stop_at_utc"],
                "media": [it["name"] for it in job["playlist"]], "media_count": len(job["playlist"]),
                "media_ok": media_ok, "manifest_ok": self.spec_path(job["job_id"]).is_file(),
                "state": st.get("state", J_PENDING), "message": st.get("message", ""),
                "retry_count": int(st.get("retry_count") or 0), "late_seconds": st.get("late_seconds"),
                "cancel_requested": self.cancel_requested(job["job_id"])}

    def listing(self, media_dir) -> list[dict]:
        return [self.describe(j, media_dir) for j in self.specs()]


class JobRunner:
    """job 1개 송출: 기존 Worker(DIRECT COPY, concat, 재접속 watchdog)를 그대로 쓰고 stop_at에 정상 종료."""

    def __init__(self, job: dict, *, store: JobStore, media_dir, key_path, lock_path, ffmpeg="ffmpeg",
                 secrets=None, wall=time.time, worker_cls=None, worker_kwargs=None):
        self.job = job
        self.store = store
        self.media_dir, self.key_path, self.lock_path = media_dir, key_path, lock_path
        self.ffmpeg = ffmpeg
        self.secrets = secrets if secrets is not None else []
        self.wall = wall
        self.worker_cls = worker_cls or Worker
        self.worker_kwargs = worker_kwargs or {}
        self.worker = None
        self.thread = None
        self.stop_requested = False
        self.error = ""

    def start(self) -> None:
        jid = self.job["job_id"]
        d = self.store.run_dir(jid)
        names = [it["name"] for it in self.job["playlist"]]
        cfg = {"schema_version": 2, "media": names[0] if len(names) == 1 else names, "play_mode": "sequential",
               "ingest_url": self.job["ingest_url"], "mode": "copy", "session_mode": "continuous", "session_id": jid}
        _atomic_write(d / "live.json", json.dumps(cfg))
        self.worker = self.worker_cls(config_path=d / "live.json", key_path=self.key_path, media_dir=self.media_dir,
                                      state_dir=d, ffmpeg=self.ffmpeg, deadline_wall=self.job["stop"],
                                      lock_path=self.lock_path, wall=self.wall, **self.worker_kwargs)
        self.worker.secrets = self.secrets  # scheduler 로그 RedactFilter와 같은 목록 (key가 로그에 남지 않게)
        self.thread = threading.Thread(target=self._run, name=f"job-{jid}", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            self.worker.run()
        except Exception as e:  # worker 예외로 scheduler가 죽지 않게
            self.error = redact(f"송출 오류 ({type(e).__name__})", self.secrets)

    @property
    def done(self) -> bool:
        return self.thread is not None and not self.thread.is_alive()

    @property
    def worker_state(self) -> str:
        return getattr(self.worker, "state", "")

    @property
    def last_error(self) -> str:
        return self.error or redact(str(getattr(self.worker, "last_error", "") or ""), self.secrets)

    def stop(self) -> None:
        self.stop_requested = True
        if self.worker is not None:
            self.worker.request_stop()

    def join(self, timeout: float | None = None) -> None:
        if self.thread is not None:
            self.thread.join(timeout)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


class Scheduler:
    """예약 job 상태 기계. tick()은 짧게 끝나고 다음 대기 초를 돌려준다 (가짜 시계로 테스트 가능).

    PENDING → (T-120) PREPARING → (T) STARTING → LIVE → (stop_at) COMPLETE
    취소 → CANCELLED, 예약 시각+5분 초과 → MISSED, 실패 → FAILED.
    재부팅/재시작 후 LIVE였던 job은 마지막 기록 후 5분 안이면 이어서 송출, 아니면 FAILED (중복 시작 없음).
    """

    def __init__(self, store: JobStore, *, media_dir, key_path, runner_factory, wall=time.time, sha_fn=_file_sha256,
                 status_path=None):
        self.store = store
        self.media_dir = Path(media_dir)
        self.key_path = Path(key_path)
        self.runner_factory = runner_factory
        self.wall = wall
        self.sha_fn = sha_fn
        self.status_path = Path(status_path) if status_path else None
        self.running: tuple[str, object] | None = None
        self._sha_cache: dict[tuple, str] = {}
        self.shutting_down = False

    # ---- state helpers ----
    def _set(self, jid: str, **changes) -> dict:
        st = self.store.read_state(jid)
        prev = st.get("state")
        st.update(changes)
        st["job_id"] = jid
        st["updated_at"] = self.wall()
        self.store.write_state(jid, st)
        if changes.get("state") and changes["state"] != prev:
            log.info("job %s %s→%s %s", jid, prev or "-", changes["state"], changes.get("message", ""))
        return st

    def _media_problem(self, job: dict, *, verify_sha: bool) -> str:
        for it in job["playlist"]:
            p = self.media_dir / it["name"]
            try:
                stat = p.stat()
            except OSError:
                return f"Cloud에 영상이 없습니다: {it['name']}"
            if stat.st_size == 0 or (it["size"] is not None and stat.st_size != it["size"]):
                return f"Cloud 영상 크기가 다릅니다: {it['name']}"
            if verify_sha and it["sha256"]:
                ck = (it["name"], stat.st_size, stat.st_mtime_ns)
                if ck not in self._sha_cache:
                    try:
                        self._sha_cache[ck] = self.sha_fn(p)
                    except OSError:
                        return f"영상을 읽을 수 없습니다: {it['name']}"
                if self._sha_cache[ck] != it["sha256"]:
                    return f"Cloud 영상이 예약 때와 다릅니다 (SHA256 불일치): {it['name']}"
        return ""

    def _key_problem(self, job: dict) -> str:
        try:
            key = self.key_path.read_text(encoding="utf-8").strip()
        except OSError:
            return "Stream Key 파일을 읽을 수 없습니다."
        if not key:
            return "Stream Key가 비어 있습니다."
        if job["key_fingerprint"] and key_fingerprint(key) != job["key_fingerprint"]:
            return "Cloud의 Stream Key가 예약 때와 다릅니다 (다른 방송용 Key로 바뀜)."
        return ""

    # ---- main ----
    def tick(self) -> float:
        now = self.wall()
        waits = []
        self._poll_running(now)
        for job in self.store.specs():
            w = self._step(job, now)
            if w is not None:
                waits.append(w)
        if self.running is not None:
            waits.append(SCHED_RUNNING_SECONDS)
        self._write_status(now)
        return max(0.5, min([SCHED_IDLE_SECONDS] + waits))

    def _write_status(self, now: float) -> None:
        if self.status_path is None:
            return
        try:
            _atomic_write(self.status_path, json.dumps({"pid": os.getpid(), "updated_at": now,
                                                        "running_job": self.running[0] if self.running else None,
                                                        "worker_version": WORKER_VERSION}))
        except OSError:
            log.warning("scheduler status write failed")

    def _poll_running(self, now: float) -> None:
        if self.running is None:
            return
        jid, r = self.running
        if self.store.cancel_requested(jid) and not r.stop_requested:
            r.stop()  # 기존 stop safety: FFmpeg에 q → 정상 종료
            self._set(jid, state=J_STOPPING, message="사용자 요청으로 송출을 멈추는 중")
        if not r.done:
            ws = r.worker_state
            state = J_STOPPING if r.stop_requested else (J_LIVE if ws in ("RUNNING", "RECONNECT_WAIT") else J_STARTING)
            self._set(jid, state=state, heartbeat=now)
            return
        self.running = None
        ws = r.worker_state
        if self.shutting_down and not self.store.cancel_requested(jid):
            self._set(jid, heartbeat=now)  # 서비스 종료/재부팅: 상태 유지 → 다시 켜지면 이어서 송출
        elif r.stop_requested:
            self._set(jid, state=J_CANCELLED, message="사용자가 송출을 중지했습니다.", finished_at=now)
        elif ws == "SESSION_LIMIT_REACHED":
            self._set(jid, state=J_COMPLETE, message="예약한 방송 시간이 끝나 정상 종료했습니다.", finished_at=now)
        elif ws == "FAILED" or r.error:
            self._set(jid, state=J_FAILED, message=r.last_error or "송출을 시작하지 못했습니다.", finished_at=now)
        else:
            self._set(jid, state=J_FAILED, message="송출이 예정보다 일찍 끝났습니다.", finished_at=now)

    def _step(self, job: dict, now: float) -> float | None:
        jid = job["job_id"]
        if self.running is not None and self.running[0] == jid:
            return None
        st = self.store.read_state(jid)
        state = st.get("state", J_PENDING)
        if state in JOB_TERMINAL:
            return None
        start, stop, grace = job["start"], job["stop"], job["grace"]
        if self.store.cancel_requested(jid):
            self._set(jid, state=J_CANCELLED, message="사용자가 예약을 취소했습니다.", finished_at=now)
            return None
        if state in JOB_ACTIVE:  # 이 프로세스에는 runner가 없음 → 재부팅/서비스 재시작 후 복구
            hb = float(st.get("heartbeat") or st.get("started_at") or 0)
            if now >= stop:
                done = hb >= stop - 90
                self._set(jid, state=J_COMPLETE if done else J_FAILED, finished_at=now,
                          message="예약한 방송 시간이 끝났습니다." if done else "송출 중 서버가 멈춰 끝까지 송출하지 못했습니다.")
                return None
            if now - hb > grace:
                self._set(jid, state=J_FAILED, finished_at=now,
                          message="송출 중 서버가 오래 멈춰 자동으로 다시 시작하지 않았습니다.")
                return None
            return self._start(job, now, st, resume=True)
        # PENDING / PREPARING
        if now >= stop or now - start > grace:
            self._set(jid, state=J_MISSED, finished_at=now,
                      message=f"예약 시각이 {int(grace // 60)}분 넘게 지나 자동 시작하지 않았습니다.")
            return None
        if now < start - PREPARE_LEAD:
            return start - PREPARE_LEAD - now
        if not st.get("prepared"):
            err = self._media_problem(job, verify_sha=True)
            if err:
                self._set(jid, state=J_FAILED, message=err, finished_at=now)
                return None
            st = self._set(jid, state=J_PREPARING, prepared=True, prepared_at=now, message="영상 확인 완료")
        if now < start - PREFLIGHT_LEAD:
            return start - PREFLIGHT_LEAD - now
        if not st.get("preflight"):
            err = self._media_problem(job, verify_sha=False) or self._key_problem(job)
            if err:
                self._set(jid, state=J_FAILED, message=err, finished_at=now)
                return None
            st = self._set(jid, preflight=True, preflight_at=now, message="최종 확인 완료 · 예약 시각 대기")
        if now < start:
            return start - now  # 송출(RTMPS)은 예약 시각에 시작: 미리 보내면 enableAutoStart로 일찍 LIVE가 될 수 있다
        return self._start(job, now, st)

    def _start(self, job: dict, now: float, st: dict, *, resume: bool = False) -> float | None:
        jid = job["job_id"]
        if self.running is not None:
            self._set(jid, state=J_FAILED, finished_at=now, message="다른 예약 LIVE가 송출 중이라 시작하지 않았습니다.")
            return None
        err = self._media_problem(job, verify_sha=False) or self._key_problem(job)
        if err:
            self._set(jid, state=J_FAILED, message=err, finished_at=now)
            return None
        late = now - job["start"]
        changes = {"state": J_STARTING, "heartbeat": now, "message": "송출 시작"}
        if resume:
            changes["retry_count"] = int(st.get("retry_count") or 0) + 1
            changes["message"] = "서버 재시작 후 송출 이어서 시작"
        else:
            changes["started_at"] = now
            changes["late_seconds"] = round(late, 1) if late >= 1 else 0
            if late >= 1:
                changes["message"] = f"예약 시각보다 {int(late)}초 늦게 송출 시작 (허용 범위)"
        self._set(jid, **changes)
        try:
            runner = self.runner_factory(job)
            runner.start()
        except Exception as e:
            self._set(jid, state=J_FAILED, finished_at=now, message=f"송출을 시작하지 못했습니다 ({type(e).__name__}).")
            return None
        self.running = (jid, runner)
        return SCHED_RUNNING_SECONDS

    def shutdown(self, timeout: float = 15) -> None:
        """서비스 종료(SIGTERM): 송출 중이면 FFmpeg 정상 종료. job 상태는 유지 (재시작 후 5분 안이면 이어서)."""
        self.shutting_down = True
        if self.running is not None:
            _, r = self.running
            if r.worker is not None:
                r.worker.request_stop()
            r.join(timeout)
            self._poll_running(self.wall())


def run_scheduler(args, *, stop_event: threading.Event | None = None, secrets: list | None = None) -> int:
    state_dir = Path(args.state_dir)
    instance = FileLock(state_dir / "scheduler.lock")
    if not instance.acquire():
        log.warning("scheduler already running — second instance blocked")
        return EXIT_CONFIG
    stop_event = stop_event or threading.Event()
    secrets = secrets if secrets is not None else []
    store = JobStore(args.jobs_dir, state_dir)
    ffmpeg = shutil.which(args.ffmpeg) or args.ffmpeg

    def factory(job):
        return JobRunner(job, store=store, media_dir=args.media_dir, key_path=args.key_file,
                         lock_path=state_dir / "worker.lock", ffmpeg=ffmpeg, secrets=secrets)
    sched = Scheduler(store, media_dir=args.media_dir, key_path=args.key_file, runner_factory=factory,
                      status_path=state_dir / "scheduler.json")
    log.info("scheduler started version=%s", WORKER_VERSION)
    try:
        while not stop_event.is_set():
            try:
                wait = sched.tick()
            except Exception:  # 한 번의 오류로 scheduler가 멈추지 않게
                log.exception("scheduler tick failed")
                wait = SCHED_IDLE_SECONDS
            stop_event.wait(wait)
    finally:
        sched.shutdown()
        instance.release()
    return EXIT_OK


def admin_command(args) -> int:
    """PC가 sudo로 호출하는 관리 명령. 결과는 JSON 한 줄 (Stream Key 출력 없음)."""
    store = JobStore(args.jobs_dir, args.state_dir)
    try:
        if args.add_job:
            raw = json.loads(sys.stdin.read() or "{}")
            out = {"ok": True, "job": store.add(raw, args.media_dir)}
        elif args.cancel_job:
            out = {"ok": True, "job": store.request_cancel(args.cancel_job)}
        elif args.list_jobs:
            try:
                sched = json.loads((Path(args.state_dir) / "scheduler.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                sched = None
            out = {"ok": True, "jobs": store.listing(args.media_dir), "scheduler": sched,
                   "worker_version": WORKER_VERSION}
        else:  # --key-fingerprint
            try:
                key = Path(args.key_file).read_text(encoding="utf-8").strip()
            except OSError:
                key = ""
            out = {"ok": True, "fingerprint": key_fingerprint(key) if key else ""}
    except (ConfigError, ValueError) as e:
        print(json.dumps({"ok": False, "error": str(e) if isinstance(e, ConfigError) else "예약 정보 형식 오류"},
                         ensure_ascii=False))
        return EXIT_CONFIG
    print(json.dumps(out, ensure_ascii=False))
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


def setup_logging(log_dir: str, secrets, log_name: str = "worker.log") -> None:
    log.setLevel(logging.INFO)
    f = RedactFilter(secrets)
    h = logging.StreamHandler(sys.stderr)  # journald
    h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    h.addFilter(f)
    log.addHandler(h)
    try:
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(os.path.join(log_dir, log_name), maxBytes=512 * 1024,
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
    ap.add_argument("--scheduler", action="store_true", help="예약 LIVE scheduler (long-live-scheduler.service)")
    ap.add_argument("--jobs-dir", default=DEFAULT_JOBS)
    ap.add_argument("--add-job", action="store_true", help="stdin JSON 예약 job 저장 (root)")
    ap.add_argument("--list-jobs", action="store_true")
    ap.add_argument("--cancel-job", default="")
    ap.add_argument("--key-fingerprint", action="store_true")
    args = ap.parse_args(argv)
    if args.self_check:
        return self_check(args)
    if args.add_job or args.list_jobs or args.cancel_job or args.key_fingerprint:
        return admin_command(args)
    if args.scheduler:
        secrets: list[str] = []
        setup_logging(args.log_dir, secrets, "scheduler.log")
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        return run_scheduler(args, stop_event=stop, secrets=secrets)
    worker = Worker(config_path=args.config, key_path=args.key_file, media_dir=args.media_dir,
                    state_dir=args.state_dir, ffmpeg=shutil.which(args.ffmpeg) or args.ffmpeg)
    setup_logging(args.log_dir, worker.secrets)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: worker.request_stop())
    return worker.run()


if __name__ == "__main__":
    sys.exit(main())
