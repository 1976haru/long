from __future__ import annotations

import ctypes
import os
import shutil
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
