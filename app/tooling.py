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


class KeepAwake:
    """LIVE 전체 수명 동안 Windows 절전을 막는다.

    SetThreadExecutionState는 호출한 스레드에 묶이므로 enable/disable은 항상 같은
    (Tk main) 스레드에서 호출한다. 장시간 제작의 prevent_windows_sleep()과는 독립적이다.
    """

    def __init__(self, setter=None):
        self._setter = setter
        self.active = False

    def _set(self, flags: int) -> None:
        if self._setter is not None:
            self._setter(flags)
        elif os.name == "nt":
            ctypes.windll.kernel32.SetThreadExecutionState(flags)

    def enable(self) -> None:
        if not self.active:
            self._set(0x80000000 | 0x00000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            self.active = True

    def disable(self) -> None:
        if self.active:
            self._set(0x80000000)  # ES_CONTINUOUS
            self.active = False


def release_tk_variables(obj) -> None:
    """창 destroy 시 Tk main thread에서 tkinter Variable(StringVar 등)을 미리 정리한다.

    파괴된 창이 참조 순환 안에 남으면 cyclic GC가 아무 스레드(FFmpeg reader, Cloud polling 등)에서
    실행될 수 있고, 그때 Variable.__del__이 다른 스레드에서 Tcl을 호출해
    "main thread is not in main loop"로 Tk 상태가 깨진다. 여기서 미리 해제해 두면 이후 GC는 아무 Tcl 호출도 하지 않는다.
    """
    import tkinter as tk

    def finalize(v):
        tkapp = getattr(v, "_tk", None)
        if tkapp is None:
            return
        try:
            if tkapp.getboolean(tkapp.call("info", "exists", v._name)):
                tkapp.globalunsetvar(v._name)
            for name in getattr(v, "_tclCommands", None) or ():
                tkapp.deletecommand(name)
        except Exception:
            pass
        v._tclCommands = None
        v._tk = None  # Variable.__del__은 _tk가 None이면 아무것도 하지 않는다

    for value in list(vars(obj).values()):
        items = value.values() if isinstance(value, dict) else value if isinstance(value, (list, tuple)) else (value,)
        for item in items:
            if isinstance(item, tk.Variable):
                finalize(item)
