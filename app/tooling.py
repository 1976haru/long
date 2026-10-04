from __future__ import annotations

import ctypes
import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

from .settings import load_settings, save_settings


def _pair(ffmpeg: Path):
    ffmpeg = Path(ffmpeg)
    probe = ffmpeg.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    return (ffmpeg, probe) if ffmpeg.exists() and probe.exists() else None


def discover_ffmpeg(app_root: Path):
    data = load_settings()
    candidates = []
    if data.get("ffmpeg_path"):
        candidates.append(Path(data["ffmpeg_path"]))
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    candidates.append(Path(app_root) / "bin" / exe)
    p = shutil.which("ffmpeg")
    if p:
        candidates.append(Path(p))
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            root = Path(local) / "Microsoft" / "WinGet" / "Packages"
            if root.exists():
                candidates += list(root.glob("*FFmpeg*/**/bin/ffmpeg.exe"))
        candidates += [
            Path(r"C:\ProgramData\chocolatey\bin\ffmpeg.exe"),
            Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        ]
    for c in candidates:
        pair = _pair(c)
        if pair:
            data["ffmpeg_path"] = str(pair[0])
            save_settings(data)
            return pair
    return None


def remember_ffmpeg(path: Path):
    pair = _pair(path)
    if pair:
        data = load_settings()
        data["ffmpeg_path"] = str(pair[0])
        save_settings(data)
    return pair


@contextmanager
def prevent_windows_sleep(enabled=True):
    if os.name != "nt" or not enabled:
        yield
        return
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        yield
    finally:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


class FfmpegExecutionGuard:
    """장시간 제작과 LIVE 송출이 FFmpeg를 동시에 실행하지 않도록 막는 공용 잠금.

    acquire/release를 서로 다른 스레드에서 호출해도 된다 (threading.Lock 사용).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._owner: str | None = None

    def try_acquire(self, owner: str) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        self._owner = owner
        return True

    def release(self, owner: str) -> None:
        if self._owner != owner:
            return
        self._owner = None
        self._lock.release()

    @property
    def owner(self) -> str | None:
        return self._owner


FFMPEG_GUARD = FfmpegExecutionGuard()
