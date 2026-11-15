"""⑤ 제공처 API 키 저장 (Windows DPAPI · 사용자 계정 전용). settings.json·로그·Git에는 절대 쓰지 않는다.

기존 Nature Video Studio(V1) 키 호환: Windows 자격 증명 관리자 서비스 'NatureVideoStudio_2026'을
사용자가 [기존 키 불러오기]를 누를 때만 '읽기 전용'으로 조회해 이번 실행 동안 메모리에서만 쓴다.
V1 항목을 수정·삭제하지 않고, 사용자가 [이 키 저장]을 따로 누를 때만 이 프로그램의 DPAPI 파일에 저장한다.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from .live_secrets import DpapiError, dpapi_protect, dpapi_unprotect
from . import settings as _settings
from .stock_net import PEXELS, PIXABAY

KEY_PROVIDERS = (PEXELS, PIXABAY)
FILE_NAMES = {PEXELS: "stock_key_pexels.dat", PIXABAY: "stock_key_pixabay.dat"}
LEGACY_SERVICE = "NatureVideoStudio_2026"
LEGACY_USERNAMES = {PEXELS: ("pexels", "Pexels"), PIXABAY: ("pixabay", "Pixabay")}
SIGNUP = {PEXELS: "https://www.pexels.com/api/new/", PIXABAY: "https://pixabay.com/api/docs/"}


class KeyStore:
    def __init__(self, folder: Path | None = None):
        self.folder = Path(folder) if folder else _settings.settings_dir()
        self._session: dict[str, str] = {}
        self._lock = threading.Lock()

    def _path(self, provider: str) -> Path:
        return self.folder / FILE_NAMES[provider]

    def has(self, provider: str) -> bool:
        return bool(self._session.get(provider)) or (provider in FILE_NAMES and self._path(provider).is_file())

    def source(self, provider: str) -> str:
        if self._session.get(provider):
            return "이번 실행만 (저장 안 함)"
        return "저장됨 (DPAPI)" if provider in FILE_NAMES and self._path(provider).is_file() else "없음"

    def get(self, provider: str) -> str | None:
        with self._lock:
            if self._session.get(provider):
                return self._session[provider]
        p = self._path(provider) if provider in FILE_NAMES else None
        if not p or not p.is_file():
            return None
        try:
            return dpapi_unprotect(p.read_bytes()).decode("utf-8").strip() or None
        except (OSError, DpapiError, UnicodeDecodeError):
            return None

    def save(self, provider: str, key: str) -> None:
        key = (key or "").strip()
        if provider not in FILE_NAMES or not (8 <= len(key) <= 512) or any(ord(c) < 33 for c in key):
            raise ValueError("API 키 형식이 올바르지 않습니다.")
        blob = dpapi_protect(key.encode("utf-8"))
        p = self._path(provider)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, p)
        with self._lock:
            self._session.pop(provider, None)

    def clear(self, provider: str) -> None:
        with self._lock:
            self._session.pop(provider, None)
        for p in (self._path(provider), self._path(provider).with_name(FILE_NAMES[provider] + ".tmp")):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    def use_for_session(self, provider: str, key: str) -> None:
        with self._lock:
            self._session[provider] = key.strip()

    def lookup(self, provider: str):
        return lambda: self.get(provider)


def _win_cred_read(target: str):
    """Windows 자격 증명 관리자 GENERIC 항목 읽기 전용 (advapi32 CredReadW). (username, password) 또는 None."""
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    pcred = ctypes.POINTER(CREDENTIAL)()
    if not adv.CredReadW(ctypes.c_wchar_p(target), 1, 0, ctypes.byref(pcred)):  # 1 = CRED_TYPE_GENERIC
        return None
    try:
        c = pcred.contents
        blob = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize) if c.CredentialBlobSize else b""
        return (c.UserName or ""), blob.decode("utf-16-le", errors="ignore")
    finally:
        adv.CredFree(pcred)


class _WinVaultReader:
    """keyring(WinVaultKeyring)이 저장하는 항목 배치와 같은 방식으로 읽기만 한다 (외부 keyring 패키지 미사용).
    target=service 항목의 사용자명이 일치하면 그 값, 아니면 'username@service' 항목."""

    def __init__(self, reader=_win_cred_read):
        self.reader = reader

    def get_password(self, service: str, username: str):
        r = self.reader(service)
        if r and r[0] == username:
            return r[1]
        r = self.reader(f"{username}@{service}")
        return r[1] if r else None


def read_legacy_key(provider: str, keyring_module=None) -> tuple[str | None, str]:
    """사용자가 요청했을 때만 호출. (키 또는 None, 상태 문구). 키 값은 상태 문구·로그에 넣지 않는다.
    기존 항목은 읽기만 하고 수정·삭제·재저장하지 않는다."""
    if provider not in LEGACY_USERNAMES:
        return None, "지원하지 않는 제공처"
    kr = keyring_module
    if kr is None:
        if os.name != "nt":
            return None, "Windows 자격 증명 관리자를 쓸 수 없습니다."
        kr = _WinVaultReader()
    try:
        for user in LEGACY_USERNAMES[provider]:
            v = kr.get_password(LEGACY_SERVICE, user)
            if isinstance(v, str) and v.strip():
                return v.strip(), "기존 Nature Video Studio 키를 찾았습니다 (읽기만 함, V1 항목은 그대로)."
    except Exception:
        return None, "보안 저장소 접근 오류 (다시 시도하세요)."
    return None, "기존 Nature Video Studio에 저장된 키가 없습니다."
