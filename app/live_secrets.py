"""Stream Key 영구 저장소 (Windows DPAPI).

- Python 표준 라이브러리 + ctypes만 사용 (pywin32/keyring/cryptography 없음).
- 파일에는 CryptProtectData blob만 저장한다. 평문 key는 디스크에 쓰지 않는다.
- 현재 Windows 사용자 계정으로만 복호화된다.
- settings.json과 완전히 분리된 파일(live_secret.dat)을 쓴다.
"""
from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path

from .live_profile import MASK, MemoryStreamKeyStore, StreamKeyStore
from .settings import settings_dir

log = logging.getLogger(__name__)

SECRET_FILE_NAME = "live_secret.dat"
# 앱 고유 추가 엔트로피 (비밀값 아님: 같은 사용자 계정의 다른 앱이 blob을 바로 풀지 못하게 하는 용도)
_ENTROPY = b"PlaylistLongVideoMaker/live-stream-key/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class DpapiError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _crypt(fn_name: str, data: bytes) -> bytes:
    if os.name != "nt":
        raise DpapiError("DPAPI는 Windows에서만 사용할 수 있습니다.")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = getattr(crypt32, fn_name)
    fn.restype = ctypes.c_int
    fn.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_DataBlob),
    ]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    src, _src_buf = _blob(data)
    ent, _ent_buf = _blob(_ENTROPY)
    out = _DataBlob()
    if fn_name == "CryptProtectData":
        ok = fn(ctypes.byref(src), "PLVM LIVE", ctypes.byref(ent), None, None,
                _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    else:
        ok = fn(ctypes.byref(src), None, ctypes.byref(ent), None, None,
                _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise DpapiError(f"DPAPI 처리 실패 (Windows 오류 {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def dpapi_protect(plain: bytes) -> bytes:
    return _crypt("CryptProtectData", plain)


def dpapi_unprotect(blob: bytes) -> bytes:
    return _crypt("CryptUnprotectData", blob)


class WindowsDpapiStreamKeyStore:
    """DPAPI로 암호화한 Stream Key를 파일에 저장한다. StreamKeyStore 인터페이스 구현."""

    persistent = True

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else settings_dir() / SECRET_FILE_NAME

    def __repr__(self) -> str:
        return f"WindowsDpapiStreamKeyStore(path={self.path.name!r}, stored={self.has_saved()})"

    def has_saved(self) -> bool:
        return self.path.is_file()

    def get(self) -> str | None:
        if not self.path.is_file():
            return None
        try:
            value = dpapi_unprotect(self.path.read_bytes()).decode("utf-8").strip()
        except (OSError, DpapiError, UnicodeDecodeError):
            # 다른 PC/계정에서 복사된 파일이거나 손상: 키를 쓰지 않는다 (내용은 로그에 남기지 않음).
            log.warning("saved LIVE stream key could not be decrypted")
            return None
        return value or None

    def set(self, value: str) -> None:
        value = (value or "").strip()
        if not value:
            self.clear()
            return
        blob = dpapi_protect(value.encode("utf-8"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, self.path)

    def clear(self) -> None:
        for p in (self.path, self.path.with_name(self.path.name + ".tmp")):
            try:
                p.unlink()
            except FileNotFoundError:
                pass


class SessionStreamKeyStore(MemoryStreamKeyStore):
    """Windows 이외 환경 fallback: 저장 기능 없음 (메모리에서만)."""

    persistent = False

    def __repr__(self) -> str:
        return f"SessionStreamKeyStore(value={MASK if self.get() else None!r})"


def default_key_store(*, is_windows: bool | None = None, path: Path | None = None) -> StreamKeyStore:
    if is_windows is None:
        is_windows = os.name == "nt"
    if is_windows:
        return WindowsDpapiStreamKeyStore(path)
    return SessionStreamKeyStore()
