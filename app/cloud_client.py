"""무료 Cloud 서버 조작 (SSH). Tk 비의존 — UI는 CloudLiveController 이벤트 큐만 읽는다.

모든 원격 작업은 `ssh ... -- user@host "<고정 명령>"` 형태다.
- 동적 값은 shlex.quote 하거나 안전 문자만 허용된 이름만 쓴다.
- Stream Key/설정/파일 내용은 명령줄이 아닌 stdin으로 보낸다.
- 상세 기록(detail log)은 최근 200줄만 메모리에 보관하며 secret은 가려진다.
"""
from __future__ import annotations

import collections
import io
import json
import os
import queue
import secrets as pysecrets
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .cloud_model import (
    CLOUD_UNAVAILABLE, FREE_UNSURE, REMOTE_CONFIG, REMOTE_KEY, REMOTE_MEDIA, REMOTE_STATUS, REMOTE_WORKER,
    SERVICE, SSH_MISSING, CloudConfigError, CloudProfile, find_ssh, safe_remote_name, sha256_file,
    ssh_base_args, worker_files,
)
from .core import creationflags_no_window
from .live_core import build_output_url
from .live_playlist import MAX_PLAYLIST_ITEMS
from .live_profile import redact
from .live_session import SESSION_CONTINUOUS, SESSION_MODES

DETAIL_LIMIT = 200
LOG_LINES = 50
POLL_SECONDS = 10.0
UPLOAD_CHUNK = 1024 * 1024
MIN_FREE_BYTES = 2 * 1024**3
UPLOAD_MARGIN_BYTES = 256 * 1024**2
q = shlex.quote


def lf_bytes(data: bytes) -> bytes:
    """텍스트 bytes의 줄바꿈을 LF로 통일 (CRLF, 단독 CR 모두). 영상 등 binary에는 쓰지 않는다."""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def to_linux_bytes(text: str) -> bytes:
    """Linux로 보낼 텍스트 → LF-only UTF-8 bytes."""
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _decode(data) -> str:
    if data is None:
        return ""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", "replace")
    return str(data)


class CloudError(RuntimeError):
    """사용자에게 보여줄 한글 메시지. secret을 포함하지 않는다."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


@dataclass(frozen=True)
class RemoteResult:
    rc: int
    out: str
    err: str


def friendly_ssh_error(err: str) -> str:
    e = (err or "").lower()
    if "unprotected private key" in e or "bad permissions" in e:
        return "SSH Key 파일 권한이 너무 넓습니다. [키 파일 권한 고치기]를 눌러 주세요."
    if "permission denied" in e and "publickey" in e:
        return "SSH Key 또는 사용자 이름이 맞지 않습니다 (Ubuntu 서버는 보통 ubuntu)."
    if "host key verification failed" in e or "remote host identification has changed" in e:
        return "서버 식별 정보가 바뀌었습니다. 서버를 새로 만들었다면 처음 설정을 다시 진행하세요."
    if "could not resolve" in e:
        return "서버 주소를 찾을 수 없습니다. IP를 확인하세요."
    if "timed out" in e or "no route" in e or "connection refused" in e or "network is unreachable" in e:
        return ("서버에 연결할 수 없습니다. 서버가 켜져 있는지, IP가 맞는지,\n"
                "Oracle Console의 보안 목록에서 22번 포트(SSH)가 열려 있는지 확인하세요.")
    if "a password is required" in e or "sudo:" in e:
        return "서버 사용자에게 sudo 권한이 필요합니다 (Oracle Ubuntu 기본 ubuntu 사용자 사용 권장)."
    if "load key" in e or "invalid format" in e:
        return "SSH Private Key 파일을 읽을 수 없습니다. 서버 생성 시 받은 Private Key인지 확인하세요."
    return CLOUD_UNAVAILABLE


def fix_key_permissions(key_path: Path, runner=subprocess.run) -> bool:
    """Windows OpenSSH가 요구하는 키 파일 권한(본인만 읽기)으로 바꾼다. 사용자 확인 후에만 호출."""
    if os.name != "nt":
        try:
            os.chmod(key_path, 0o600)
            return True
        except OSError:
            return False
    user = os.environ.get("USERNAME", "")
    if not user:
        return False
    p = runner(["icacls", str(key_path), "/inheritance:r", "/grant:r", f"{user}:R"],
               capture_output=True, text=True, check=False, creationflags=creationflags_no_window())
    return p.returncode == 0


ENV_SCRIPT = r"""
set -u
. /etc/os-release 2>/dev/null || true
echo "os_id=${ID:-unknown}"
echo "os_version=${VERSION_ID:-}"
echo "arch=$(uname -m)"
echo "disk_free_kb=$(df -Pk / | awk 'NR==2{print $4}')"
echo "mem_kb=$(awk '/MemTotal/{print $2}' /proc/meminfo)"
echo "python3=$(python3 -c 'import sys;print(sys.version.split()[0])' 2>/dev/null || echo none)"
if sudo -n true 2>/dev/null; then echo "sudo=ok"; else echo "sudo=no"; fi
if command -v apt-get >/dev/null 2>&1; then echo "apt=yes"; else echo "apt=no"; fi
if command -v ffmpeg >/dev/null 2>&1; then echo "ffmpeg=yes"; else echo "ffmpeg=no"; fi
shape=$(curl -s -m 3 -H 'Authorization: Bearer Oracle' http://169.254.169.254/opc/v2/instance/shape 2>/dev/null || true)
echo "shape=${shape}"
"""

FFMPEG_SCRIPT = r"""
set -e
if ! command -v ffmpeg >/dev/null 2>&1; then
  sudo -n env DEBIAN_FRONTEND=noninteractive apt-get update -y -q >/dev/null
  sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y -q ffmpeg python3 >/dev/null
fi
ffmpeg -hide_banner -version | head -n 1
"""

STATUS_SCRIPT = r"""
python3 - <<'PYEOF'
import json, shutil, subprocess
def sh(*a):
    try:
        return subprocess.run(list(a), capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""
out = {"active": sh("systemctl", "is-active", "long-live.service"),
       "enabled": sh("systemctl", "is-enabled", "long-live.service"),
       "installed": bool(sh("systemctl", "cat", "long-live.service"))}
try:
    out["status"] = json.load(open("/opt/long-live/state/status.json"))
except Exception:
    out["status"] = None
try:
    out["disk_free_bytes"] = shutil.disk_usage("/opt/long-live/media").free
except Exception:
    out["disk_free_bytes"] = None
print(json.dumps(out))
PYEOF
"""

# 고정 명령: secret은 stdin으로만 들어온다.
KEY_WRITE_CMD = (
    "sudo -n sh -c " + q(
        f"umask 077; cat > {REMOTE_KEY}.part && chown longlive:longlive {REMOTE_KEY}.part && "
        f"chmod 600 {REMOTE_KEY}.part && mv -f {REMOTE_KEY}.part {REMOTE_KEY}"))
CONFIG_WRITE_CMD = (
    "sudo -n sh -c " + q(
        f"umask 027; cat > {REMOTE_CONFIG}.part && chown root:longlive {REMOTE_CONFIG}.part && "
        f"chmod 640 {REMOTE_CONFIG}.part && mv -f {REMOTE_CONFIG}.part {REMOTE_CONFIG}"))


@dataclass
class CloudStatus:
    reachable: bool = False
    installed: bool = False
    service_active: bool = False
    enabled: bool = False
    state: str = ""
    runtime_seconds: float = 0.0
    media: str = ""
    mode: str = ""
    fps: float | None = None
    bitrate: str | None = None
    speed: float | None = None
    reconnects: int = 0
    retry_in: float | None = None
    last_error: str = ""
    disk_free_bytes: int | None = None
    message: str = ""
    playlist_count: int = 0
    current_playlist_index: int | None = None  # 0-based
    playlist_round: int | None = None
    session_mode: str = ""
    session_limit: float | None = None
    session_remaining: float | None = None

    @property
    def live(self) -> bool:
        return self.service_active and self.state in ("STARTING", "RUNNING", "RECONNECT_WAIT")

    @property
    def session_complete(self) -> bool:
        return self.state == "SESSION_LIMIT_REACHED"


@dataclass
class UploadResult:
    remote_name: str
    skipped: bool
    sha256: str


class CloudClient:
    def __init__(self, profile: CloudProfile, *, ssh: Path | None = None, runner=subprocess.run,
                 popen=subprocess.Popen):
        self.profile = profile
        self.ssh = ssh if ssh is not None else find_ssh()
        self._runner = runner
        self._popen = popen
        self.detail: collections.deque[str] = collections.deque(maxlen=DETAIL_LIMIT)
        self._secrets: list[str] = []

    def __repr__(self) -> str:
        return f"CloudClient({self.profile.destination})"

    # ---------- primitives ----------
    def _base(self) -> list[str]:
        if self.ssh is None:
            raise CloudError(SSH_MISSING)
        return ssh_base_args(self.ssh, self.profile)

    def _note(self, text: str) -> None:
        for line in redact(text, self._secrets).splitlines()[-20:]:
            self.detail.append(line[:400])

    def run(self, remote_cmd: str, *, input_text: str | None = None, timeout: float = 60) -> RemoteResult:
        """원격 명령 1개. input_text(스크립트/Stream Key/설정)는 LF-only UTF-8 bytes로 stdin에 보낸다."""
        return self._run_bytes(remote_cmd, None if input_text is None else to_linux_bytes(input_text), timeout=timeout)

    def _run_bytes(self, remote_cmd: str, payload: bytes | None, *, timeout: float) -> RemoteResult:
        """binary subprocess (text=False).

        Windows에서 text=True로 stdin을 쓰면 TextIOWrapper가 \\n을 \\r\\n으로 바꿔 보낸다 →
        Linux bash가 "$'\\r': command not found"로 실패했다 (실제 OCI 테스트에서 확인).
        stdin payload는 로그/detail/repr 어디에도 남기지 않는다.
        """
        args = self._base() + [remote_cmd]
        self._note("$ ssh " + self.profile.destination + " " + (remote_cmd if len(remote_cmd) < 300 else remote_cmd[:300] + " …"))
        try:
            p = self._runner(args, input=payload, capture_output=True, timeout=timeout, check=False,
                             creationflags=creationflags_no_window())
        except subprocess.TimeoutExpired:
            raise CloudError("서버 응답 시간이 초과되었습니다. " + CLOUD_UNAVAILABLE) from None
        except OSError:
            raise CloudError(SSH_MISSING) from None
        res = RemoteResult(p.returncode, _decode(p.stdout), _decode(p.stderr))
        if res.out.strip():
            self._note(res.out.strip())
        if res.err.strip():
            self._note(res.err.strip())
        return res

    def script(self, script: str, *args: str, timeout: float = 120) -> RemoteResult:
        """Linux bash 스크립트를 `bash -s --`의 stdin으로 실행 (byte-level LF만 전달)."""
        cmd = "bash -s --" + "".join(" " + q(str(a)) for a in args)
        return self._run_bytes(cmd, to_linux_bytes(script), timeout=timeout)

    def _must(self, res: RemoteResult, fallback: str) -> RemoteResult:
        if res.rc == 255:  # ssh 연결 오류
            raise CloudError(friendly_ssh_error(res.err), res.err)
        if res.rc != 0:
            msg = friendly_ssh_error(res.err)
            raise CloudError(fallback if msg == CLOUD_UNAVAILABLE else msg, res.err)
        return res

    def upload_stream(self, remote_cmd: str, source, total: int, *, progress_cb=None, cancel=None,
                      timeout_per_chunk: float = 120) -> None:
        """source(파일 객체)를 1MB씩 ssh stdin으로 보낸다. 전체를 메모리에 올리지 않는다."""
        args = self._base() + [remote_cmd]
        self._note("$ ssh " + self.profile.destination + " " + remote_cmd)
        try:
            proc = self._popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=creationflags_no_window())
        except OSError:
            raise CloudError(SSH_MISSING) from None
        err_buf = collections.deque(maxlen=50)
        t = threading.Thread(target=lambda: [err_buf.append(l.decode("utf-8", "replace")) for l in proc.stderr], daemon=True)
        t.start()
        sent = 0
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise CloudError("업로드를 중지했습니다.")
                b = source.read(UPLOAD_CHUNK)
                if not b:
                    break
                proc.stdin.write(b)
                sent += len(b)
                if progress_cb:
                    progress_cb(sent / max(total, 1))
            proc.stdin.close()
            rc = proc.wait(timeout=timeout_per_chunk)
        except (BrokenPipeError, OSError):
            proc.kill()
            proc.wait()
            t.join(2)
            raise CloudError(friendly_ssh_error("".join(err_buf)), "".join(err_buf)) from None
        except BaseException:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            raise
        finally:
            t.join(2)
            for s in (proc.stdout, proc.stderr):
                try:
                    if s:
                        s.close()
                except OSError:
                    pass
        if rc != 0:
            raise CloudError(friendly_ssh_error("".join(err_buf)), "".join(err_buf))

    # ---------- checks ----------
    def check_connection(self) -> str:
        res = self.run("echo LONG_LIVE_OK", timeout=30)
        if res.rc != 0 or "LONG_LIVE_OK" not in res.out:
            raise CloudError(friendly_ssh_error(res.err), res.err)
        return "서버 연결 성공"

    def environment(self) -> dict[str, str]:
        res = self._must(self.script(ENV_SCRIPT, timeout=60), "서버 환경을 확인할 수 없습니다.")
        env = dict(l.split("=", 1) for l in res.out.splitlines() if "=" in l)
        if env.get("os_id") not in ("ubuntu", "debian"):
            raise CloudError("Ubuntu 서버가 필요합니다. Oracle Console에서 이미지를 Ubuntu로 선택해 서버를 만드세요.")
        if env.get("sudo") != "ok":
            raise CloudError(friendly_ssh_error("sudo: a password is required"))
        if env.get("python3", "none") == "none" and env.get("apt") != "yes":
            raise CloudError("서버에 python3가 없습니다.")
        return env

    # ---------- setup (4 STEP wizard: 6단계 자동 준비) ----------
    PREPARE_STEPS = ("서버 연결", "환경 확인", "FFmpeg 준비", "LIVE Worker 설치", "자동 복구 설정", "완료")

    def prepare(self, progress_cb: Callable[[int, str], None] | None = None) -> dict:
        def step(i):
            if progress_cb:
                progress_cb(i, self.PREPARE_STEPS[i - 1])
        step(1)
        self.check_connection()
        step(2)
        env = self.environment()
        if int(env.get("disk_free_kb") or 0) * 1024 < MIN_FREE_BYTES:
            raise CloudError("서버 저장 공간이 2GB보다 적습니다. 불필요한 파일을 지운 뒤 다시 시도하세요.")
        step(3)
        ff = self._must(self.script(FFMPEG_SCRIPT, timeout=1500), "FFmpeg를 설치하지 못했습니다.")
        env["ffmpeg_version"] = ff.out.strip().splitlines()[-1] if ff.out.strip() else ""
        step(4)
        self.install_worker()
        step(5)
        res = self._must(self.run(f"systemctl show -p LoadState {SERVICE}", timeout=30), "자동 복구 설정 확인 실패")
        if "LoadState=loaded" not in res.out:
            raise CloudError("자동 복구 서비스(systemd)가 등록되지 않았습니다.")
        step(6)
        hc = self._must(self.run(f"sudo -n -u longlive python3 {REMOTE_WORKER} --self-check", timeout=60),
                        "LIVE Worker 상태 확인 실패")
        try:
            env["health"] = json.loads(hc.out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise CloudError("LIVE Worker 상태 확인 실패") from None
        env["free_notice"] = FREE_UNSURE
        return env

    def install_worker(self) -> None:
        files = worker_files()
        missing = [n for n, p in files.items() if not p.is_file()]
        if missing:
            raise CloudError("프로그램 안의 Cloud Worker 파일이 없습니다: " + ", ".join(missing))
        stage = f"/tmp/long-live-install-{pysecrets.token_hex(6)}"
        self._must(self.run(f"mkdir -m 700 {q(stage)}"), "설치 준비 실패")
        try:
            for name, path in files.items():
                # 텍스트(.py/.sh/.service)만: Windows checkout/EXE 번들에 CRLF가 섞여도 서버에는 LF만
                data = lf_bytes(path.read_bytes())
                self.upload_stream(f"cat > {q(stage + '/' + name)}", io.BytesIO(data), len(data))
            res = self._must(self.run(f"sudo -n bash {q(stage + '/install.sh')} {q(self.profile.user)}", timeout=180),
                             "LIVE Worker 설치 실패")
            if "INSTALL_OK" not in res.out:
                raise CloudError("LIVE Worker 설치 실패", res.err)
        finally:
            try:
                self.run(f"rm -rf {q(stage)}", timeout=30)
            except CloudError:
                pass

    # ---------- media ----------
    def remote_sha256(self, remote_name: str, *, part: bool = False) -> str | None:
        path = f"{REMOTE_MEDIA}/{remote_name}" + (".part" if part else "")
        res = self.run(f"sha256sum -- {q(path)} 2>/dev/null | cut -d' ' -f1", timeout=600)
        if res.rc == 255:
            raise CloudError(friendly_ssh_error(res.err), res.err)
        v = res.out.strip()
        return v if len(v) == 64 else None

    def _remote_free_bytes(self) -> int:
        res = self._must(self.run(f"df -PB1 {q(REMOTE_MEDIA)} | awk 'NR==2{{print $4}}'"), "서버 저장 공간 확인 실패")
        try:
            return int(res.out.strip() or 0)
        except ValueError:
            return 0

    def upload_media(self, local: Path, *, progress_cb: Callable[[float, str], None] | None = None,
                     cancel: threading.Event | None = None, local_sha: str | None = None,
                     check_space: bool = True) -> UploadResult:
        local = Path(local)
        name = safe_remote_name(local)
        size = local.stat().st_size
        cb = progress_cb or (lambda f, t: None)
        if local_sha is None:
            local_sha = sha256_file(local, lambda f: cb(f * 0.1, "영상 확인 중"))
        if self.remote_sha256(name) == local_sha:
            cb(1.0, "이미 Cloud에 같은 영상이 있습니다 (업로드 생략)")
            return UploadResult(name, True, local_sha)
        free = self._remote_free_bytes() if check_space else 0
        if free and free < size + UPLOAD_MARGIN_BYTES:
            raise CloudError(f"서버 저장 공간이 부족합니다 (필요 {size / 1024**3:.1f}GB, 남음 {free / 1024**3:.1f}GB).")
        part = f"{REMOTE_MEDIA}/{name}.part"
        final = f"{REMOTE_MEDIA}/{name}"
        try:
            with open(local, "rb") as f:
                self.upload_stream(f"cat > {q(part)}", f, size, cancel=cancel,
                                   progress_cb=lambda x: cb(0.1 + x * 0.85, f"Cloud로 보내는 중 {x * 100:.0f}%"),
                                   timeout_per_chunk=600)
            cb(0.96, "Cloud에서 파일 검증 중")
            if self.remote_sha256(name, part=True) != local_sha:
                raise CloudError("업로드한 파일이 원본과 다릅니다 (SHA256 불일치). 다시 시도하세요.")
            self._must(self.run(f"mv -f -- {q(part)} {q(final)}"), "업로드 마무리 실패")
        except BaseException:
            try:
                self.run(f"rm -f -- {q(part)}", timeout=30)  # 기존 final은 건드리지 않는다
            except CloudError:
                pass
            raise
        cb(1.0, "Cloud 업로드 완료 (SHA256 검증)")
        return UploadResult(name, False, local_sha)

    def upload_many(self, paths, *, progress_cb: Callable[[int, int, float, str], None] | None = None,
                    cancel: threading.Event | None = None) -> list[UploadResult]:
        """Playlist 업로드: 각 파일 SHA256 → 서버에 같은 파일 있으면 생략 → 없는 파일 합계로 저장 공간 먼저 확인 →
        하나씩 .part 업로드·검증·이름 교체. 서버의 기존 파일은 지우지 않는다. 1MB씩 전송(메모리에 전체를 올리지 않음)."""
        paths = [Path(x) for x in paths]
        n = len(paths)
        cb = progress_cb or (lambda i, n_, f, t: None)
        names = [safe_remote_name(x) for x in paths]
        if len(set(names)) != n:
            raise CloudError("Playlist에 서버 파일 이름이 같은 영상이 있습니다. 파일 이름을 바꿔 주세요.")
        shas, missing = [], []
        for i, x in enumerate(paths, 1):
            if cancel is not None and cancel.is_set():
                raise CloudError("업로드를 중지했습니다.")
            cb(i, n, 0.0, f"{i}/{n} 영상 확인 중")
            sha = sha256_file(x)
            shas.append(sha)
            if self.remote_sha256(names[i - 1]) == sha:
                cb(i, n, 1.0, f"{i}/{n} 이미 있음")
            else:
                missing.append(i - 1)
        need = sum(paths[i].stat().st_size for i in missing)
        if missing:
            free = self._remote_free_bytes()
            margin = UPLOAD_MARGIN_BYTES + int(need * 0.05)
            if free and free < need + margin:
                raise CloudError(f"서버 저장 공간이 부족합니다 (필요 {(need + margin) / 1024**3:.1f}GB, "
                                 f"남음 {free / 1024**3:.1f}GB). 기존 영상은 자동으로 지우지 않습니다.")
        results = []
        for i, x in enumerate(paths):
            if i not in missing:
                results.append(UploadResult(names[i], True, shas[i]))
                continue
            k = i + 1
            r = self.upload_media(x, cancel=cancel, local_sha=shas[i], check_space=False,
                                  progress_cb=lambda f, t, k=k: cb(k, n, f, f"{k}/{n} {t}"))
            results.append(r)
        cb(n, n, 1.0, f"{n}/{n} 검증 완료")
        return results

    # ---------- live ----------
    def worker_version(self) -> int:
        res = self.run(f"grep -m1 '^WORKER_VERSION' {q(REMOTE_WORKER)} 2>/dev/null", timeout=30)
        digits = "".join(c for c in res.out if c.isdigit())
        return int(digits) if digits else 1

    def start_live(self, *, remote_media, ingest_url: str, stream_key: str, wait_seconds: float = 25,
                   sleep=time.sleep, session_mode: str = SESSION_CONTINUOUS, session_id: str | None = None) -> CloudStatus:
        """remote_media: 서버 파일 이름 1개(str) 또는 Playlist(list). 설정은 schema v2.

        단일 영상 + 계속 방송은 media를 문자열로 써서 기존(v1) worker와도 그대로 호환된다.
        Playlist/보관 안전 모드는 worker v2가 필요하다 (구버전이면 시작하지 않고 업데이트 안내)."""
        build_output_url(ingest_url, stream_key)  # 형식 검사 (예외에 key 없음)
        names = [remote_media] if isinstance(remote_media, str) else list(remote_media)
        if not names or len(names) > MAX_PLAYLIST_ITEMS:
            raise CloudError(f"Playlist는 1~{MAX_PLAYLIST_ITEMS}개입니다.")
        if session_mode not in SESSION_MODES:
            raise CloudError("세션 모드가 올바르지 않습니다.")
        self._secrets = [stream_key.strip()]
        chk = self.run(f"test -f {q(REMOTE_WORKER)} && systemctl cat {SERVICE} >/dev/null && echo OK", timeout=30)
        if chk.rc == 255:
            raise CloudError(friendly_ssh_error(chk.err), chk.err)
        if "OK" not in chk.out:
            raise CloudError("Cloud LIVE Worker가 설치되지 않았습니다. [처음 설정 도우미]를 진행하세요.")
        needs_v2 = len(names) > 1 or session_mode != SESSION_CONTINUOUS
        if needs_v2 and self.worker_version() < 2:
            raise CloudError("Playlist/보관 안전 모드는 Cloud LIVE Worker 업데이트가 필요합니다.\n"
                             "현재 방송이 끝난 뒤 [처음 설정 도우미] → [무료 Cloud 자동 준비]를 다시 실행하세요.")
        for name in names:
            chk = self.run(f"test -s {q(REMOTE_MEDIA + '/' + name)} && echo OK", timeout=30)
            if "OK" not in chk.out:
                raise CloudError("Cloud에 영상이 없습니다. [Cloud에 영상 보내기]를 먼저 진행하세요.")
        self._must(self.run(KEY_WRITE_CMD, input_text=stream_key.strip() + "\n"), "Stream Key 저장 실패")
        cfg = json.dumps({
            "schema_version": 2,
            "media": names[0] if len(names) == 1 else names,
            "play_mode": "sequential",
            "ingest_url": ingest_url.strip(),
            "mode": "copy",
            "session_mode": session_mode,
            "session_id": session_id or pysecrets.token_hex(8),
        })
        self._must(self.run(CONFIG_WRITE_CMD, input_text=cfg + "\n"), "LIVE 설정 저장 실패")
        self._must(self.run(f"sudo -n systemctl enable {SERVICE} >/dev/null 2>&1; sudo -n systemctl restart {SERVICE}"),
                   "Cloud LIVE 시작 실패")
        deadline = time.monotonic() + wait_seconds
        st = self.status()
        while time.monotonic() < deadline and st.state not in ("RUNNING", "FAILED", "SESSION_LIMIT_REACHED"):
            sleep(2)
            st = self.status()
        if st.state == "FAILED" or (not st.service_active and st.state != "RUNNING"):
            raise CloudError("Cloud LIVE를 시작하지 못했습니다: " + (st.last_error or "서비스가 실행되지 않았습니다."))
        return st

    def stop_live(self) -> None:
        self._must(self.run(f"sudo -n systemctl disable --now {SERVICE}", timeout=60), "Cloud LIVE 종료 실패")

    def status(self) -> CloudStatus:
        res = self.script(STATUS_SCRIPT, timeout=40)
        if res.rc != 0:
            return CloudStatus(reachable=False, message=friendly_ssh_error(res.err))
        try:
            d = json.loads(res.out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return CloudStatus(reachable=True, message="상태를 읽을 수 없습니다.")
        s = d.get("status") or {}
        return CloudStatus(
            reachable=True,
            installed=bool(d.get("installed")),
            service_active=d.get("active") == "active",
            enabled=d.get("enabled") == "enabled",
            state=str(s.get("state") or ("STOPPED" if d.get("active") != "active" else "")),
            runtime_seconds=float(s.get("runtime_seconds") or 0),
            media=str(s.get("media") or ""),
            mode=str(s.get("mode") or ""),
            fps=s.get("fps"),
            bitrate=s.get("bitrate"),
            speed=s.get("speed"),
            reconnects=int(s.get("reconnects") or 0),
            retry_in=s.get("retry_in"),
            last_error=redact(str(s.get("last_error") or ""), self._secrets),
            disk_free_bytes=d.get("disk_free_bytes"),
            playlist_count=int(s.get("playlist_count") or (1 if s.get("media") else 0)),
            current_playlist_index=s.get("current_playlist_index"),
            playlist_round=s.get("playlist_round"),
            session_mode=str(s.get("session_mode") or ""),
            session_limit=s.get("session_limit"),
            session_remaining=s.get("session_remaining"),
        )

    def logs(self, n: int = LOG_LINES) -> list[str]:
        n = max(1, min(int(n), LOG_LINES))
        res = self.run(f"sudo -n journalctl -u {SERVICE} -n {n} --no-pager -o cat", timeout=30)
        lines = (res.out or res.err).splitlines()[-n:]
        return [redact(l, self._secrets) for l in lines]


class CloudLiveController:
    """UI용: 원격 작업을 백그라운드 스레드에서 실행하고 결과를 이벤트 큐로 전달한다."""

    def __init__(self, client_factory: Callable[[], CloudClient], *, poll_seconds: float = POLL_SECONDS):
        self._factory = client_factory
        self.poll_seconds = poll_seconds
        self.events: queue.Queue = queue.Queue()
        self.status: CloudStatus | None = None
        self.live_started = False
        self._op: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()
        self.cancel = threading.Event()
        self.client: CloudClient | None = None

    @property
    def busy(self) -> bool:
        return bool(self._op and self._op.is_alive())

    @property
    def cloud_live_active(self) -> bool:
        if self.status is not None and self.status.reachable:
            return self.status.live
        return self.live_started

    def _client(self) -> CloudClient:
        if self.client is None:
            self.client = self._factory()
        return self.client

    def _run(self, name: str, fn) -> bool:
        if self.busy:
            return False
        self.cancel.clear()

        def body():
            try:
                result = fn()
                self.events.put(("op", name, True, result))
            except (CloudError, CloudConfigError) as e:
                self.events.put(("op", name, False, str(e)))
            except Exception as e:  # 예상 밖 오류도 UI가 멈추지 않게
                self.events.put(("op", name, False, f"{CLOUD_UNAVAILABLE}\n({type(e).__name__})"))
        self._op = threading.Thread(target=body, name=f"cloud-{name}", daemon=True)
        self._op.start()
        return True

    def check_async(self):
        return self._run("check", lambda: self._refresh())

    def _upload(self, local) -> list[UploadResult]:
        """단일 Path 또는 Playlist(list) 업로드 → UploadResult 목록."""
        c = self._client()
        if isinstance(local, (list, tuple)) and len(local) > 1:
            return c.upload_many(local, cancel=self.cancel,
                                 progress_cb=lambda i, n, f, t: self.events.put(("progress", "upload", (i - 1 + f) / n, t)))
        one = local[0] if isinstance(local, (list, tuple)) else local
        return [c.upload_media(one, cancel=self.cancel,
                               progress_cb=lambda f, t: self.events.put(("progress", "upload", f, t)))]

    def upload_async(self, local):
        return self._run("upload", lambda: self._upload(local))

    def start_async(self, *, local, ingest_url: str, stream_key: str, session_mode: str = SESSION_CONTINUOUS,
                    session_id: str | None = None):
        def fn():
            c = self._client()
            ups = self._upload(local)
            self.events.put(("progress", "start", 1.0, "Cloud LIVE 시작 중"))
            names = [u.remote_name for u in ups]
            st = c.start_live(remote_media=names[0] if len(names) == 1 else names, ingest_url=ingest_url,
                              stream_key=stream_key, session_mode=session_mode, session_id=session_id)
            self.live_started = True
            self.status = st
            return st
        return self._run("start", fn)

    def stop_async(self):
        def fn():
            self._client().stop_live()
            self.live_started = False
            return self._refresh()
        return self._run("stop", fn)

    def stop_blocking(self, timeout: float = 60) -> bool:
        """앱 종료 시 [LIVE도 종료] 선택용."""
        if self._op and self._op.is_alive():
            self._op.join(timeout)
        try:
            self._client().stop_live()
            self.live_started = False
            return True
        except CloudError:
            return False

    def _refresh(self) -> CloudStatus:
        st = self._client().status()
        self.status = st
        self.events.put(("status", st))
        return st

    def start_polling(self):
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._poll_stop.clear()

        def loop():
            while not self._poll_stop.wait(self.poll_seconds):
                if self.busy:
                    continue
                try:
                    self._refresh()
                except (CloudError, CloudConfigError) as e:
                    self.events.put(("status", CloudStatus(reachable=False, message=str(e))))
                except Exception as e:  # polling 스레드가 조용히 죽지 않게
                    self.events.put(("status", CloudStatus(reachable=False, message=f"{CLOUD_UNAVAILABLE} ({type(e).__name__})")))
        self._poll_thread = threading.Thread(target=loop, name="cloud-poll", daemon=True)
        self._poll_thread.start()

    def stop_polling(self):
        self._poll_stop.set()

    def drain_events(self) -> list[tuple]:
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out
