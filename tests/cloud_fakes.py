"""가짜 SSH 서버 (실제 OCI/SSH 접속 없이 CloudClient 명령/보안 동작을 검증)."""
import hashlib
import io
import json
import shlex
import subprocess
from pathlib import Path

from app.cloud_client import CONFIG_WRITE_CMD, ENV_SCRIPT, FFMPEG_SCRIPT, KEY_WRITE_CMD, STATUS_SCRIPT, CloudClient
from app.cloud_model import CloudProfile


class FakeRemote:
    def __init__(self, *, reachable=True, os_id="ubuntu", sudo=True, ssh_error=""):
        self.reachable = reachable
        self.os_id = os_id
        self.sudo = sudo
        self.ssh_error = ssh_error
        self.files: dict[str, bytes] = {}
        self.calls: list[dict] = []  # {"args":[...], "input":...}
        self.installed = False
        self.active = False
        self.enabled = False
        self.key = None
        self.config = None
        self.state = "STOPPED"
        self.corrupt_upload = False
        self.fail_upload = False
        self.free = 50 * 1024**3
        self.worker_version = 2

    # subprocess.run 대체
    def run(self, args, input=None, **kw):
        # 실제 ssh와 같은 binary 경로만 허용: text mode면 Windows가 stdin에 CR을 끼워 넣는다
        assert kw.get("shell") is not True
        assert not kw.get("text") and not kw.get("universal_newlines") and "encoding" not in kw
        assert isinstance(args, list)
        assert input is None or isinstance(input, bytes), type(input)
        assert input is None or b"\r" not in input, "CR on the wire"
        text = None if input is None else input.decode("utf-8")
        self.calls.append({"args": list(args), "input": text, "raw": input})
        cmd = args[-1]
        if not self.reachable:
            err = self.ssh_error or "ssh: connect to host x port 22: Connection timed out"
            return subprocess.CompletedProcess(args, 255, b"", err.encode())
        out, rc, err = self.handle(cmd, text)
        return subprocess.CompletedProcess(args, rc, out.encode("utf-8"), err.encode("utf-8"))

    def handle(self, cmd, input):
        if cmd == "echo LONG_LIVE_OK":
            return "LONG_LIVE_OK\n", 0, ""
        if cmd.startswith("bash -s"):
            if input == ENV_SCRIPT.replace("\r\n", "\n"):
                return (f"os_id={self.os_id}\nos_version=24.04\narch=aarch64\ndisk_free_kb={40 * 1024 * 1024}\n"
                        f"mem_kb=1000000\npython3=3.12.3\nsudo={'ok' if self.sudo else 'no'}\napt=yes\nffmpeg=no\nshape=VM.Standard.A1.Flex\n"), 0, ""
            if input == FFMPEG_SCRIPT.replace("\r\n", "\n"):
                return "ffmpeg version 6.1.1\n", 0, ""
            if input == STATUS_SCRIPT.replace("\r\n", "\n"):
                st = {"state": self.state, "media": json.loads(self.config)["media"] if self.config else "",
                      "mode": "DIRECT COPY", "runtime_seconds": 12.5, "fps": 30.0, "bitrate": "8100.0kbits/s",
                      "speed": 1.0, "reconnects": 0, "last_error": ""} if self.installed else None
                return json.dumps({"active": "active" if self.active else "inactive",
                                   "enabled": "enabled" if self.enabled else "disabled",
                                   "installed": self.installed, "status": st, "disk_free_bytes": self.free}), 0, ""
        if cmd == KEY_WRITE_CMD:
            self.key = input
            return "", 0, ""
        if cmd == CONFIG_WRITE_CMD:
            self.config = input
            return "", 0, ""
        p = shlex.split(cmd)
        if p[:2] == ["sha256sum", "--"]:
            data = self.files.get(p[2])
            return (hashlib.sha256(data).hexdigest() + "\n") if data is not None else "", 0, ""
        if p[:2] == ["grep", "-m1"] and "WORKER_VERSION" in p[2]:
            return (f'WORKER_VERSION = "{self.worker_version}"\n' if self.installed else ""), 0, ""
        if p[:2] == ["df", "-PB1"]:
            return f"{self.free}\n", 0, ""
        if p[:3] == ["mv", "-f", "--"]:
            self.files[p[4]] = self.files.pop(p[3])
            return "", 0, ""
        if p[:3] == ["rm", "-f", "--"]:
            self.files.pop(p[3], None)
            return "", 0, ""
        if p[:2] in (["mkdir", "-m"], ["rm", "-rf"]):
            return "", 0, ""
        if p[:3] == ["sudo", "-n", "bash"] and p[3].endswith("/install.sh"):
            self.installed = True
            return "INSTALL_OK\n", 0, ""
        if p[:3] == ["systemctl", "show", "-p"]:
            return ("LoadState=loaded\n" if self.installed else "LoadState=not-found\n"), 0, ""
        if "--self-check" in cmd:
            return json.dumps({"python": "3.12.3", "ffmpeg": "ffmpeg version 6.1.1", "dirs": {}}) + "\n", 0, ""
        if p[:1] == ["test"] and "-f" in p:
            return ("OK\n" if self.installed else ""), 0, ""
        if p[:2] == ["test", "-s"]:
            return ("OK\n" if p[2] in self.files else ""), 1 if p[2] not in self.files else 0, ""
        if "systemctl restart" in cmd:
            self.active = self.enabled = True
            self.state = "RUNNING"
            return "", 0, ""
        if "systemctl disable --now" in cmd:
            self.active = self.enabled = False
            self.state = "STOPPED"
            return "", 0, ""
        if "journalctl" in cmd:
            return "\n".join(f"INFO line {i}" for i in range(200)), 0, ""
        return "", 0, ""

    # subprocess.Popen 대체 (stdin 업로드)
    def popen(self, args, **kw):
        assert kw.get("shell") is not True
        self.calls.append({"args": list(args), "input": "<stream>"})
        remote = self

        class Stdin:
            def __init__(self):
                self.buf = bytearray()

            def write(self, b):
                if remote.fail_upload and len(self.buf) > 0:
                    raise BrokenPipeError()
                self.buf += b

            def close(self):
                p = shlex.split(args[-1])
                assert p[0] == "cat" and p[1] == ">"
                data = bytes(self.buf)
                if remote.corrupt_upload:
                    data = data[:-1] + b"X"
                remote.files[p[2]] = data

        class Proc:
            def __init__(self):
                self.stdin = Stdin()
                self.stdout = io.BytesIO(b"")
                self.stderr = io.BytesIO(b"")
                self.returncode = None

            def wait(self, timeout=None):
                self.returncode = 255 if not remote.reachable else 0
                return self.returncode

            def poll(self):
                return self.returncode

            def kill(self):
                self.returncode = -9

        return Proc()


def make_client(tmp_path: Path, remote: FakeRemote) -> CloudClient:
    key = tmp_path / "oci.key"
    if not key.exists():
        key.write_text("-----BEGIN FAKE PRIVATE KEY-----\nNOT-A-REAL-KEY-CONTENT\n-----END FAKE PRIVATE KEY-----\n")
    ssh = tmp_path / "ssh.exe"
    ssh.write_bytes(b"")
    profile = CloudProfile("123.45.67.89", "ubuntu", str(key)).validated()
    return CloudClient(profile, ssh=ssh, runner=remote.run, popen=remote.popen)
