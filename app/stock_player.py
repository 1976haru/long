"""⑤ 실제 동영상 미리보기 — FFmpeg ffplay를 Tk 버튼으로 제어 (Python으로 프레임을 디코딩하지 않음).

재생 / 일시정지(현재 위치 기억 후 정지) / 탐색(그 위치부터 다시 열기) / 처음부터 / 반복 / 길이 표시.
- 재생 성공은 ffplay가 출력하는 재생 시계가 실제로 앞으로 갈 때만 '재생 확인'으로 본다 (정지 썸네일 ≠ 재생).
- 실패 원인 구분: 네트워크 / 제공처 접근 거부(HTTP) / 코덱·파일 형식 / 플레이어 없음.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .core import creationflags_no_window
from .stock_net import USER_AGENT

IDLE, OPENING, PLAYING, PAUSED, ENDED, ERROR = "대기", "여는 중", "재생 중", "일시정지", "끝", "오류"
_CLOCK = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s+(?:M-V|A-V|M-A|V-?)\s*:")


def classify_error(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"\b429\b|too many requests", t):
        return "제공처 요청 한도 초과 (HTTP 429) — 잠시 후 다시 시도하세요"
    if re.search(r"\b(403|401|forbidden|unauthorized)\b", t):
        return "제공처 접근 거부 (HTTP 403/401)"
    if re.search(r"\b404\b|not found", t):
        return "파일 없음 (HTTP 404)"
    if re.search(r"http error|server returned [45]\d\d", t):
        return "제공처 서버 오류 (HTTP)"
    if any(k in t for k in ("timed out", "timeout", "connection refused", "could not resolve", "failed to resolve",
                            "network is unreachable", "connection reset", "i/o error", "tls", "ssl", "end of file")):
        return "네트워크 연결 실패 / 시간 초과"
    if any(k in t for k in ("invalid data", "decoder", "codec", "unsupported", "moov atom", "no such file")):
        return "코덱·파일 형식 문제 (재생 불가 파일)"
    return "재생 실패 (원인 불명): " + (text.strip().splitlines() or [""])[-1][:120]


def net_args(src: str) -> list[str]:
    """네트워크 입력에만: 설명이 있는 User-Agent (Wikimedia·Archive 정책) + 읽기 시간 제한."""
    if src.startswith(("https://", "http://")):
        return ["-user_agent", USER_AGENT, "-rw_timeout", "15000000"]
    return []


def probe_media(ffprobe: Path, src: str, timeout: float = 25.0) -> tuple[float, str, str]:
    """(길이 초, 코덱, 오류 설명). 네트워크 주소도 ffprobe로 직접 확인한다."""
    try:
        p = subprocess.run([str(ffprobe), "-v", "error", *net_args(src), "-select_streams", "v:0",
                            "-show_entries", "stream=codec_name:format=duration", "-of", "json", src],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=creationflags_no_window())
    except subprocess.TimeoutExpired:
        return 0.0, "", "네트워크 연결 실패 / 시간 초과"
    except OSError as e:
        return 0.0, "", f"ffprobe 실행 실패: {e}"
    try:
        data = json.loads(p.stdout or "{}")
    except ValueError:
        data = {}
    streams = data.get("streams") or []
    if p.returncode != 0 or not streams:
        return 0.0, "", classify_error(p.stderr or "no video stream")
    try:
        dur = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    return dur, str(streams[0].get("codec_name") or ""), ""


def find_ffplay(ffmpeg: Path | None) -> Path | None:
    if ffmpeg:
        cand = Path(ffmpeg).with_name("ffplay.exe" if Path(ffmpeg).suffix.lower() == ".exe" else "ffplay")
        if cand.is_file():
            return cand
    w = shutil.which("ffplay")
    return Path(w) if w else None


class PreviewPlayer:
    """ffplay 프로세스 1개를 관리한다. 상태는 작업 스레드가 갱신하고 UI는 주기적으로 읽기만 한다."""

    STALL_SECONDS = 25.0  # 이 시간 안에 재생 시계가 안 움직이면 실패로 본다 (정지 화면을 재생으로 착각하지 않게)

    def __init__(self, ffplay: Path | None, *, ffmpeg: Path | None = None, env: dict | None = None,
                 extra_args: tuple[str, ...] = ()):
        self.ffplay = ffplay
        self.ffmpeg = ffmpeg      # 탐색(0초 이후): ffmpeg가 그 위치부터 원본 시간 그대로 넘겨주고 ffplay가 재생
        self.env = env
        self.extra_args = extra_args
        self.src = ""
        self.title = ""
        self.duration = 0.0
        self.loop = False
        self.state = IDLE
        self.message = ""
        self.position = 0.0
        self.confirmed = False      # 재생 시계가 실제로 진행됨을 확인했는가
        self._start_pos = 0.0
        self._first_clock: float | None = None
        self._proc: subprocess.Popen | None = None
        self._feeder: subprocess.Popen | None = None
        self._opened_at = 0.0
        self._lock = threading.Lock()
        self._gen = 0
        self._err_lines: list[str] = []

    # ---- 제어 ----
    def open(self, src: str, title: str = "", duration: float = 0.0) -> None:
        self.stop()
        self.src, self.title, self.duration = src, title, max(0.0, duration)
        self.position = 0.0
        self.confirmed = False
        self.state = IDLE
        self.message = ""

    def play(self, pos: float | None = None) -> bool:
        if not self.src:
            return False
        if not self.ffplay:
            self.state, self.message = ERROR, "ffplay를 찾지 못했습니다 (FFmpeg 전체 설치본에 포함 — FFmpeg 설정 확인)."
            return False
        p = self.position if pos is None else pos
        if self.duration and p >= self.duration - 0.3:
            p = 0.0
        self._kill()
        self._start_pos = max(0.0, p)
        self.position = self._start_pos
        self._first_clock = None
        self._err_lines = []
        self.confirmed = False  # 이번 재생에서 다시 확인
        title = f"⑤ 미리보기 - {self.title or 'video'}"[:120]
        feeder = None
        if self._start_pos > 0 and self.ffmpeg:
            # ffplay의 -ss는 일부 원격 WebM에서 멈춘다 → ffmpeg가 정확한 위치부터 미리보기용 360p로 가볍게 변환해 파이프로
            # 넘긴다 (-output_ts_offset으로 재생 시계 = 실제 위치). 복사(-c copy)는 앞 키프레임부터 다시 보여 쓰지 않는다.
            fcmd = [str(self.ffmpeg), "-hide_banner", "-v", "error", "-nostdin", *net_args(self.src),
                    "-ss", f"{self._start_pos:.2f}", "-i", self.src, "-map", "0:v:0", "-vf", "scale=-2:360",
                    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "30",
                    "-output_ts_offset", f"{self._start_pos:.2f}", "-f", "matroska", "pipe:1"]
            cmd = [str(self.ffplay), "-hide_banner", "-an", "-x", "640", "-y", "360", "-window_title", title,
                   "-autoexit", *self.extra_args, "-i", "pipe:0"]
        else:
            cmd = [str(self.ffplay), "-hide_banner", "-an", "-x", "640", "-y", "360", "-window_title", title,
                   "-loop", "0" if self.loop else "1", *net_args(self.src)]
            if not self.loop:
                cmd.append("-autoexit")
            if self._start_pos > 0:
                cmd += ["-ss", f"{self._start_pos:.2f}"]
            cmd += [*self.extra_args, "-i", self.src]
        try:
            if fcmd_used := (self._start_pos > 0 and self.ffmpeg):
                feeder = subprocess.Popen(fcmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          creationflags=creationflags_no_window())
            proc = subprocess.Popen(cmd, stdin=feeder.stdout if fcmd_used else subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=self.env)
            if feeder is not None:
                feeder.stdout.close()  # ffplay가 닫으면 ffmpeg도 끝나게
        except OSError as e:
            if feeder is not None:
                feeder.kill()
            self.state, self.message = ERROR, f"ffplay 실행 실패: {e}"
            return False
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._proc = proc
            self._feeder = feeder
        self.state, self.message = OPENING, "여는 중… (네트워크 영상은 몇 초 걸릴 수 있음)"
        self._opened_at = time.monotonic()
        threading.Thread(target=self._reader, args=(proc, gen), daemon=True).start()
        if feeder is not None:
            threading.Thread(target=self._feeder_errors, args=(feeder, gen), daemon=True).start()
        threading.Thread(target=self._watchdog, args=(gen,), daemon=True).start()
        return True

    def _feeder_errors(self, feeder: subprocess.Popen, gen: int) -> None:
        try:
            for raw in feeder.stderr:
                s = raw.decode("utf-8", "replace").strip()
                if s and gen == self._gen:
                    self._err_lines.append(s[:300])
        except (OSError, ValueError):
            pass

    def _watchdog(self, gen: int) -> None:
        """재생 시계가 STALL_SECONDS 안에 움직이지 않으면 멈추고 원인을 보여준다 (정지 화면 ≠ 재생)."""
        while time.monotonic() - self._opened_at < self.STALL_SECONDS:
            time.sleep(0.25)
            if gen != self._gen or self.confirmed:
                return
        if gen == self._gen and not self.confirmed:
            self._kill()
            self.state = ERROR
            why = classify_error("\n".join(self._err_lines[-6:])) if self._err_lines else ""
            self.message = (f"{int(self.STALL_SECONDS)}초 동안 영상이 재생되지 않았습니다 — "
                            + (why or "네트워크가 느리거나 제공처가 요청을 막았을 수 있습니다"))

    def pause(self) -> None:
        if self.state in (PLAYING, OPENING):
            self._kill()
            self.state, self.message = PAUSED, f"일시정지 · {fmt(self.position)}"

    def seek(self, pos: float) -> bool:
        pos = max(0.0, min(pos, self.duration - 0.5 if self.duration > 1 else pos))
        if self.state in (PLAYING, OPENING):
            return self.play(pos)
        self.position = pos
        self.message = f"{fmt(pos)}부터 재생합니다 — [▶ 재생]"
        return True

    def restart(self) -> bool:
        return self.play(0.0)

    def stop(self) -> None:
        self._kill()
        if self.state != ERROR:
            self.state = IDLE

    def _kill(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            feeder, self._feeder = getattr(self, "_feeder", None), None
            self._gen += 1
        for pr in (proc, feeder):
            if pr and pr.poll() is None:
                try:
                    pr.terminate()
                    pr.wait(timeout=3)
                except Exception:
                    try:
                        pr.kill()
                    except Exception:
                        pass

    @property
    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    # ---- ffplay 출력 읽기 ----
    def _reader(self, proc: subprocess.Popen, gen: int) -> None:
        buf = b""
        stream = proc.stderr
        while True:
            ch = stream.read(1) if stream else b""
            if not ch:
                break
            if ch in (b"\r", b"\n"):
                self._line(buf.decode("utf-8", "replace"), gen)
                buf = b""
            else:
                buf += ch
                if len(buf) > 4000:
                    buf = b""
        if buf:
            self._line(buf.decode("utf-8", "replace"), gen)
        rc = proc.wait()
        with self._lock:
            if gen != self._gen:
                return  # 사용자가 멈추거나 다시 연 프로세스
            self._proc = None
        if self.confirmed and (rc == 0 or self.position > self._start_pos):
            if self.loop and self._start_pos > 0:  # 탐색 위치에서 끝까지 본 뒤: 반복이면 처음부터 다시
                self.play(0.0)
                return
            self.state, self.message = ENDED, "재생이 끝났습니다 (창을 닫았거나 영상 끝)."
        else:
            self.state = ERROR
            self.message = classify_error("\n".join(self._err_lines[-6:]) or f"ffplay 종료 코드 {rc}")

    def _line(self, line: str, gen: int) -> None:
        if gen != self._gen:
            return
        m = _CLOCK.match(line)
        if m:
            try:
                clock = float(m.group(1))
            except ValueError:
                return
            if clock != clock or clock < 0:
                return
            if self._first_clock is None:
                self._first_clock = clock
            elapsed = clock - self._first_clock
            self.position = clock if clock >= self._start_pos - 1 else self._start_pos + elapsed
            if self.duration and self.loop and self.position > self.duration:
                self.position %= self.duration
            if elapsed >= 0.5:
                self.confirmed = True
                self.state = PLAYING
                self.message = "재생 중 (재생 확인됨)"
            return
        s = line.strip()
        if (s and not s.startswith(("Input #", "Stream #", "Duration:", "Metadata:"))
                and "fd=" not in s and "hardware accelerated renderer" not in s
                and not re.match(r"^[A-Za-z_][\w-]*\s+:\s", s)):  # 상태 줄·메타데이터 줄은 오류 근거가 아님
            self._err_lines.append(s[:300])
            self._err_lines = self._err_lines[-20:]


def fmt(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"
