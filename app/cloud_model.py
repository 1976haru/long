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
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .core import creationflags_no_window
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
SCHEDULER_SERVICE = "long-live-scheduler.service"
SCHEDULER_WORKER_VERSION = 3  # 예약 LIVE(Cloud scheduler)가 들어간 worker 버전
MULTI_CHANNEL_WORKER_VERSION = 4  # 여러 채널 동시 LIVE (채널별 경로/잠금, long-live@<id>.service)

# ---------------- 여러 채널 동시 Cloud LIVE (worker v4) ----------------
# 기본 채널(default)은 위의 기존 1채널 경로/서비스를 그대로 쓴다 (기존 사용자 설정·서버 상태 보존).
DEFAULT_LIVE_PROFILE = "default"
MAX_CONCURRENT_LIVE = 2  # cloud/long_live_worker.py와 같은 값 (OCI Free VM 보호, DIRECT COPY만)
REMOTE_CHANNELS_ETC = f"{REMOTE_ETC}/channels"
REMOTE_CHANNELS_STATE = f"{REMOTE_ROOT}/state/channels"
_LIVE_PROFILE_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
CONCURRENT_BUSY = (f"현재 Cloud에서 LIVE {MAX_CONCURRENT_LIVE}개가 실행 중입니다.\n"
                   f"동시 송출은 최대 {MAX_CONCURRENT_LIVE}개입니다.")


def validate_live_profile_id(profile_id) -> str:
    """채널 ID: 영문 소문자로 시작, 소문자/숫자/_ 32자 이하 (서버 경로·systemd 이름에 그대로 쓰므로 엄격히)."""
    p = DEFAULT_LIVE_PROFILE if profile_id in (None, "") else profile_id
    if p != DEFAULT_LIVE_PROFILE and (not isinstance(p, str) or not _LIVE_PROFILE_ID.match(p)):
        raise CloudConfigError("채널 ID 형식이 올바르지 않습니다 (영문 소문자/숫자/_).")
    return p


def is_default_profile(profile_id) -> bool:
    return validate_live_profile_id(profile_id) == DEFAULT_LIVE_PROFILE


def profile_service(profile_id) -> str:
    p = validate_live_profile_id(profile_id)
    return SERVICE if p == DEFAULT_LIVE_PROFILE else f"long-live@{p}.service"


def profile_remote_paths(profile_id) -> dict[str, str]:
    p = validate_live_profile_id(profile_id)
    if p == DEFAULT_LIVE_PROFILE:
        return {"etc": REMOTE_ETC, "key": REMOTE_KEY, "config": REMOTE_CONFIG, "status": REMOTE_STATUS}
    return {"etc": f"{REMOTE_CHANNELS_ETC}/{p}", "key": f"{REMOTE_CHANNELS_ETC}/{p}/stream.key",
            "config": f"{REMOTE_CHANNELS_ETC}/{p}/live.json", "status": f"{REMOTE_CHANNELS_STATE}/{p}/status.json"}

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


# ---------------- SSH 연결 도구 탐색 ----------------
# Windows OpenSSH가 없어도 Git for Windows / GitHub Desktop에 들어 있는 ssh.exe를 찾아 쓴다.
# 존재 여부만 보지 않고 `ssh -V`로 실제 동작을 확인(probe)한 후보만 사용한다.

SSH_SOURCE_LABELS = {
    "manual": "직접 지정한 SSH",
    "windows": "Windows OpenSSH",
    "path": "PATH의 SSH",
    "git": "Git for Windows SSH",
    "github_desktop": "GitHub Desktop SSH",
}
SSH_PROBE_TIMEOUT = 5.0
GIT_FOR_WINDOWS_URL = "https://git-scm.com/download/win"

SSH_MISSING = ("SSH 연결 도구를 찾지 못했습니다.\n\n"
               "[다시 찾기]를 눌러 Git/GitHub Desktop의 SSH를 찾아보거나\n"
               "[직접 선택]에서 ssh.exe를 지정하세요.\n\n"
               "Windows OpenSSH 설치는 필수가 아닙니다.")
SSH_INVALID = "선택한 파일은 SSH 연결 도구가 아닙니다 (ssh -V 확인 실패). ssh.exe를 선택하세요."


@dataclass(frozen=True)
class SshCandidate:
    path: Path
    source: str

    @property
    def label(self) -> str:
        return SSH_SOURCE_LABELS.get(self.source, "SSH")


@dataclass(frozen=True)
class SshTool:
    """probe에 성공한 SSH 실행 파일."""
    path: Path
    source: str
    version: str

    @property
    def label(self) -> str:
        return SSH_SOURCE_LABELS.get(self.source, "SSH")


def _classify_ssh(path) -> str:
    s = str(path).replace("/", "\\").lower()
    if "\\githubdesktop\\" in s:
        return "github_desktop"
    if "\\openssh\\ssh" in s and ("\\system32\\" in s or "\\sysnative\\" in s):
        return "windows"
    if "\\git\\usr\\bin\\" in s or "\\git\\mingw64\\bin\\" in s:
        return "git"
    return "path"


def _version_key(name: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", name))


def discover_ssh_candidates(*, saved: tuple[str, str] | None = None, env=None, which=shutil.which,
                            is_windows: bool | None = None) -> list[SshCandidate]:
    """우선순위 순서의 SSH 후보 (실제 존재하는 파일만, 중복 제거). 예외를 밖으로 내보내지 않는다."""
    env = os.environ if env is None else env
    is_windows = (os.name == "nt") if is_windows is None else is_windows
    raw: list[tuple[Path, str]] = []

    def add(p, source):
        try:
            if p:
                raw.append((Path(p), source))
        except (TypeError, ValueError):
            pass

    try:
        # 1. 사용자가 지정했거나 이전에 확인된 경로
        if saved and saved[0]:
            add(saved[0], saved[1] if saved[1] in SSH_SOURCE_LABELS else "manual")
        # 2. Windows 내장 OpenSSH
        if is_windows:
            root = Path(env.get("SystemRoot") or env.get("WINDIR") or r"C:\Windows")
            for sub in ("System32", "Sysnative"):
                add(root / sub / "OpenSSH" / "ssh.exe", "windows")
        # 3. PATH (위치를 보고 Git/GitHub Desktop/Windows로 이름 표시)
        try:
            found = which("ssh")
            add(found, _classify_ssh(found) if found else "path")
        except Exception:
            pass
        if is_windows:
            # 4. Git for Windows 기본 설치 위치 (PATH에 없어도 찾는다)
            for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
                if env.get(var):
                    add(Path(env[var]) / "Git" / "usr" / "bin" / "ssh.exe", "git")
            if env.get("LOCALAPPDATA"):
                add(Path(env["LOCALAPPDATA"]) / "Programs" / "Git" / "usr" / "bin" / "ssh.exe", "git")
        # 5. PATH의 git.exe에서 Git root 역산 (<root>\cmd\git.exe, <root>\mingw64\bin\git.exe 등)
        try:
            git = which("git")
        except Exception:
            git = None
        if git:
            for anc in list(Path(git).parents)[:3]:
                add(anc / "usr" / "bin" / "ssh.exe", "git")
                add(anc / "mingw64" / "bin" / "ssh.exe", "git")
        # 6. GitHub Desktop 내장 Git (최신 버전 우선)
        if is_windows and env.get("LOCALAPPDATA"):
            base = Path(env["LOCALAPPDATA"]) / "GitHubDesktop"
            try:
                apps = sorted(base.glob("app-*"), key=lambda p: _version_key(p.name), reverse=True)
            except OSError:
                apps = []
            for app_dir in apps:
                g = app_dir / "resources" / "app" / "git"
                add(g / "usr" / "bin" / "ssh.exe", "github_desktop")
                add(g / "mingw64" / "bin" / "ssh.exe", "github_desktop")
    except Exception:
        pass  # 탐색 실패로 앱이 종료되지 않게

    out, seen = [], set()
    for p, source in raw:
        try:
            key = os.path.normcase(os.path.abspath(str(p)))
            if key in seen or not p.is_file():
                continue
        except (OSError, ValueError):
            continue
        seen.add(key)
        out.append(SshCandidate(p, source))
    return out


def probe_ssh_executable(path, *, runner=subprocess.run, timeout: float = SSH_PROBE_TIMEOUT) -> str | None:
    """`ssh -V`를 실행해 OpenSSH 버전 문자열을 돌려준다. 동작하지 않으면 None (예외 없음)."""
    try:
        p = Path(path)
        if not p.is_file():
            return None
        res = runner([str(p), "-V"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                     timeout=timeout, check=False, creationflags=creationflags_no_window())
    except Exception:
        return None
    text = f"{res.stdout or ''}\n{res.stderr or ''}"
    for line in text.splitlines():
        if "openssh" in line.lower():
            return line.strip()[:120]
    return None


def load_saved_ssh() -> tuple[str, str] | None:
    """settings.json의 cloud_ssh {path, source}. 없거나 형식이 다르면 None (이전 설정 호환)."""
    d = load_settings().get("cloud_ssh")
    if isinstance(d, dict) and isinstance(d.get("path"), str) and d["path"]:
        return d["path"], str(d.get("source") or "manual")
    return None


def save_ssh_tool(tool: SshTool | None) -> None:
    """SSH 실행 파일 경로와 종류만 저장한다 (key/Stream Key/비밀번호 저장 없음)."""
    data = load_settings()
    if tool is None:
        data.pop("cloud_ssh", None)
    else:
        data["cloud_ssh"] = {"path": str(tool.path), "source": tool.source}
    save_settings(data)


def discover_ssh(*, saved: tuple[str, str] | None = None, probe=probe_ssh_executable, **kw) -> SshTool | None:
    for c in discover_ssh_candidates(saved=saved, **kw):
        version = probe(c.path)
        if version:
            return SshTool(c.path, c.source, version)
    return None


def validate_manual_ssh(path, *, probe=probe_ssh_executable) -> SshTool:
    version = probe(path)
    if not version:
        raise CloudConfigError(SSH_INVALID)
    return SshTool(Path(path), "manual", version)


def find_ssh(**kw) -> Path | None:
    """저장된 경로 → Windows OpenSSH → PATH → Git → GitHub Desktop 순서로 probe에 성공한 ssh."""
    kw.setdefault("saved", load_saved_ssh())
    tool = discover_ssh(**kw)
    return tool.path if tool else None


def known_hosts_file() -> Path:
    return settings_dir() / "cloud_known_hosts"


def ssh_option_path(path) -> str:
    """ssh -o 값용 경로 (shell quoting 아님, argument 하나로 전달).

    큰따옴표: 공백 있는 경로가 여러 파일로 나뉘지 않게 (OpenSSH 설정 문법). 공백이 없으면 붙이지 않는다 —
      Git for Windows(MSYS) ssh는 공백 없는 인자 안의 \\" 를 잘못 해석한다 ("invalid quotes").
    / 변환: 역슬래시 escape 규칙 회피 (Windows OpenSSH, Git/MSYS ssh 모두 C:/... 인식).
    %% : ssh의 %토큰 확장 방지.
    """
    s = str(path).replace("\\", "/").replace("%", "%%")
    return f'"{s}"' if any(c.isspace() for c in s) else s


def ssh_base_args(ssh: Path, profile: CloudProfile, *, connect_timeout: int = 10) -> list[str]:
    """ssh argument list. destination 앞에 `--`를 두어 옵션 주입을 막는다."""
    return [
        str(ssh),
        "-i", str(profile.key_path).replace("%", "%%"),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={ssh_option_path(known_hosts_file())}",
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
        "long-live-scheduler.service": root / "deploy" / "linux" / "long-live-scheduler.service",
        "long-live@.service": root / "deploy" / "linux" / "long-live@.service",
        "install.sh": root / "deploy" / "linux" / "install.sh",
        "uninstall.sh": root / "deploy" / "linux" / "uninstall.sh",
    }
