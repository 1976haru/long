"""무료 Cloud(OCI Always Free) 연결 모델 — Tk와 무관한 순수 로직.

무료 안전 원칙 (FREE SAFETY):
- 이 프로그램은 OCI/AWS/GCP/Azure API를 호출하지 않는다. 서버(VM)를 만들거나, 결제/유료 shape/스토리지를
  선택·생성·변경하지 않는다. 사용자가 Oracle Console에서 직접 만든 서버에 SSH로만 접속한다.
- 서버가 무료인지 프로그램은 단정하지 않는다 ("무료 여부를 Oracle Console에서 확인하세요").
- 회수(idle reclamation) 정책을 회피하는 가짜 부하/트래픽 기능을 만들지 않는다.

SSH 원칙:
- Windows 내장 ssh.exe를 subprocess argument list로 실행한다 (shell=True 금지, Python SSH 라이브러리 없음).
- 원격 명령은 고정 스크립트 + shlex.quote 인자만 사용한다.
- SSH private key 내용은 읽거나 저장하지 않는다. 경로만 settings.json에 저장한다.
- Stream Key는 SSH 명령줄에 넣지 않고 stdin으로만 보낸다.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .settings import load_settings, save_settings, settings_dir

PROVIDER_OCI_FREE = "OCI_ALWAYS_FREE"
PROVIDER_LOCAL = "LOCAL"
SUPPORTED_PROVIDERS = (PROVIDER_OCI_FREE, PROVIDER_LOCAL)

ORACLE_FREE_URL = "https://www.oracle.com/cloud/free/"
ORACLE_CONSOLE_URL = "https://cloud.oracle.com/"

REMOTE_ROOT = "/opt/long-live"
REMOTE_MEDIA = f"{REMOTE_ROOT}/media"
REMOTE_WORKER = f"{REMOTE_ROOT}/worker/long_live_worker.py"
REMOTE_STATUS = f"{REMOTE_ROOT}/state/status.json"
REMOTE_ETC = "/etc/long-live"
REMOTE_CONFIG = f"{REMOTE_ETC}/live.json"
REMOTE_KEY = f"{REMOTE_ETC}/stream.key"
SERVICE = "long-live.service"

FREE_NOTICE = "이 프로그램은 유료 Cloud 자원을 자동 생성하지 않습니다."
FREE_UNSURE = "무료 여부를 Oracle Console에서 확인하세요."
NO_CAPACITY = ("무료 서버 자리가 현재 없습니다.\n비용이 발생하는 서버를 만들지 말고\n"
               "[내 PC에서 LIVE]를 사용하세요.")
CLOUD_UNAVAILABLE = "무료 Cloud 서버를 사용할 수 없습니다."

_HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_SAFE_REMOTE = re.compile(r"[^A-Za-z0-9._-]+")


class CloudConfigError(ValueError):
    pass


def validate_host(host: str) -> str:
    host = (host or "").strip()
    if not host:
        raise CloudConfigError("서버 IP를 입력하세요.")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if host.startswith("-") or not _HOSTNAME.match(host):
        raise CloudConfigError("서버 IP 형식이 올바르지 않습니다 (예: 123.45.67.89).")
    return host


def validate_user(user: str) -> str:
    user = (user or "").strip()
    if not _USER.match(user):
        raise CloudConfigError("사용자 이름 형식이 올바르지 않습니다 (예: ubuntu).")
    return user


def validate_key_path(path: str) -> Path:
    p = Path((path or "").strip().strip('"'))
    if not str(p) or str(p) == ".":
        raise CloudConfigError("SSH Private Key 파일을 선택하세요.")
    if not p.is_file():
        raise CloudConfigError("SSH Private Key 파일을 찾을 수 없습니다.")
    if p.suffix.lower() == ".pub":
        raise CloudConfigError(".pub 파일은 공개키입니다. Private Key 파일(.key 등)을 선택하세요.")
    return p


@dataclass(frozen=True)
class CloudProfile:
    host: str
    user: str = "ubuntu"
    key_path: str = ""
    provider: str = PROVIDER_OCI_FREE

    def validated(self) -> "CloudProfile":
        if self.provider not in SUPPORTED_PROVIDERS:
            raise CloudConfigError("지원하지 않는 Cloud입니다.")
        return CloudProfile(validate_host(self.host), validate_user(self.user),
                            str(validate_key_path(self.key_path)), self.provider)

    @property
    def destination(self) -> str:
        host = self.host
        if ":" in host:  # IPv6
            host = f"[{host}]"
        return f"{self.user}@{host}"


def load_cloud_profile() -> CloudProfile | None:
    d = load_settings().get("cloud")
    if not isinstance(d, dict) or not d.get("host"):
        return None
    try:
        return CloudProfile(str(d["host"]), str(d.get("user") or "ubuntu"), str(d.get("key_path") or ""),
                            str(d.get("provider") or PROVIDER_OCI_FREE))
    except (KeyError, TypeError):
        return None


def save_cloud_profile(profile: CloudProfile | None) -> None:
    """host/user/key 경로만 저장한다. key 내용, Stream Key는 저장하지 않는다."""
    data = load_settings()
    if profile is None:
        data.pop("cloud", None)
    else:
        data["cloud"] = {k: v for k, v in asdict(profile).items() if k in ("host", "user", "key_path", "provider")}
    save_settings(data)


def find_ssh() -> Path | None:
    """Windows 내장 OpenSSH 우선, 없으면 PATH."""
    if os.name == "nt":
        root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        for sub in ("System32", "Sysnative"):
            p = root / sub / "OpenSSH" / "ssh.exe"
            if p.is_file():
                return p
    p = shutil.which("ssh")
    return Path(p) if p else None


SSH_MISSING = ("Windows의 OpenSSH 클라이언트(ssh.exe)를 찾을 수 없습니다.\n"
               "Windows 설정 → 시스템 → 선택적 기능 → 'OpenSSH 클라이언트'를 추가한 뒤 다시 시도하세요.")


def known_hosts_file() -> Path:
    return settings_dir() / "cloud_known_hosts"


def ssh_base_args(ssh: Path, profile: CloudProfile, *, connect_timeout: int = 10) -> list[str]:
    """ssh argument list. destination 앞에 `--`를 두어 옵션 주입을 막는다."""
    return [
        str(ssh),
        "-i", str(profile.key_path),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={known_hosts_file()}",
        "-o", f"ConnectTimeout={int(connect_timeout)}",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
        "-o", "LogLevel=ERROR",
        "--", profile.destination,
    ]


def safe_remote_name(local: Path) -> str:
    """서버 파일 이름: 영문/숫자/._- 만 사용 (따옴표·공백·한글 등은 치환 + 짧은 해시로 구분)."""
    local = Path(local)
    stem = _SAFE_REMOTE.sub("_", local.stem).strip("._-") or "video"
    if stem != local.stem:
        stem = f"{stem[:80]}_{hashlib.sha1(local.name.encode('utf-8')).hexdigest()[:8]}"
    stem = stem[:120]
    if not stem[0].isalnum():
        stem = "v" + stem
    return f"{stem}.mp4"


def sha256_file(path: Path, progress_cb=None, chunk: int = 1024 * 1024) -> str:
    """스트리밍 SHA256 (파일 전체를 메모리에 올리지 않음)."""
    h = hashlib.sha256()
    total = max(1, Path(path).stat().st_size)
    done = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress_cb:
                progress_cb(done / total)
    return h.hexdigest()


def resource_root() -> Path:
    """cloud/, deploy/ 위치 (PyInstaller EXE이면 _MEIPASS)."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(__file__).resolve().parent.parent


def worker_files() -> dict[str, Path]:
    root = resource_root()
    return {
        "long_live_worker.py": root / "cloud" / "long_live_worker.py",
        "long-live.service": root / "deploy" / "linux" / "long-live.service",
        "install.sh": root / "deploy" / "linux" / "install.sh",
        "uninstall.sh": root / "deploy" / "linux" / "uninstall.sh",
    }
