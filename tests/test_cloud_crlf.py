"""Linux로 보내는 stdin은 byte-level LF만 (실제 OCI에서 "$'\\r': command not found" 버그 재발 방지).

원인: Windows에서 subprocess text=True로 stdin을 쓰면 TextIOWrapper가 \\n을 \\r\\n으로 바꿔 보낸다.
가짜 runner는 번역 전 문자열만 보므로, 실제 자식 프로세스가 받은 bytes를 검사하는 테스트를 함께 둔다.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import app.cloud_client as cc
from app.cloud_client import (
    CONFIG_WRITE_CMD, ENV_SCRIPT, FFMPEG_SCRIPT, KEY_WRITE_CMD, STATUS_SCRIPT, CloudClient, lf_bytes, to_linux_bytes,
)
from app.cloud_model import CloudProfile

from cloud_fakes import FakeRemote, make_client

FAKE_KEY = "dummy-crlf-0000-not-real"
LINUX_SCRIPTS = {"ENV_SCRIPT": ENV_SCRIPT, "FFMPEG_SCRIPT": FFMPEG_SCRIPT, "STATUS_SCRIPT": STATUS_SCRIPT}


class Recorder:
    def __init__(self, stdout=b"", stderr=b"", rc=0):
        self.calls = []
        self.result = (rc, stdout, stderr)

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        rc, out, err = self.result
        return subprocess.CompletedProcess(args, rc, out, err)


def client(tmp_path, runner):
    key = tmp_path / "oci.key"
    key.write_text("-----BEGIN FAKE-----\nNOT-REAL-KEY-BODY\n-----END FAKE-----\n")
    ssh = tmp_path / "ssh.exe"
    ssh.write_bytes(b"")
    return CloudClient(CloudProfile("123.45.67.89", "ubuntu", str(key)).validated(), ssh=ssh, runner=runner)


@pytest.mark.parametrize("src,expected", [
    ("set -u\r\necho test\r\n\r\n", b"set -u\necho test\n\n"),   # CRLF
    ("set -u\recho test\r", b"set -u\necho test\n"),              # 단독 CR
    ("set -u\r\necho a\recho b\necho c\r\n", b"set -u\necho a\necho b\necho c\n"),  # 혼합
])
def test_script_sends_lf_only_bytes(tmp_path, src, expected):
    rec = Recorder()
    client(tmp_path, rec).script(src, "arg 1")
    args, kw = rec.calls[0]
    payload = kw["input"]
    assert isinstance(payload, bytes)
    assert payload == expected
    assert b"\r" not in payload and b"\n" in payload
    assert not kw.get("text") and "encoding" not in kw and kw.get("shell") is not True  # binary stdin
    assert args[-1] == "bash -s -- 'arg 1'"


def test_all_linux_scripts_are_lf_only_on_wire(tmp_path, monkeypatch):
    for name, text in LINUX_SCRIPTS.items():
        for variant in (text, text.replace("\n", "\r\n")):  # Windows에서 CRLF로 오염돼도
            rec = Recorder()
            client(tmp_path, rec).script(variant)
            payload = rec.calls[0][1]["input"]
            assert b"\r" not in payload, name
            assert payload == text.replace("\r\n", "\n").encode("utf-8"), name


def test_key_and_config_stdin_are_lf_bytes_and_never_in_argv(tmp_path):
    remote = FakeRemote()  # FakeRemote는 bytes·CR 없음·text mode 아님을 매 호출 assert
    c = make_client(tmp_path, remote)
    c.prepare()
    m = tmp_path / "x_LIVE_READY.mp4"
    m.write_bytes(b"\r\n binary media \r\n" * 50)  # 영상 bytes는 그대로 보내야 함
    up = c.upload_media(m)
    assert remote.files[f"/opt/long-live/media/{up.remote_name}"] == m.read_bytes()  # binary는 변환 금지
    c.start_live(remote_media=up.remote_name, ingest_url="rtmps://a.rtmps.youtube.com:443/live2",
                 stream_key=FAKE_KEY, sleep=lambda s: None)
    key_call = next(x for x in remote.calls if x["args"][-1] == KEY_WRITE_CMD)
    cfg_call = next(x for x in remote.calls if x["args"][-1] == CONFIG_WRITE_CMD)
    assert key_call["raw"] == (FAKE_KEY + "\n").encode()
    assert cfg_call["raw"].endswith(b"}\n") and b"\r" not in cfg_call["raw"]
    assert all(FAKE_KEY not in a for x in remote.calls for a in x["args"])
    assert FAKE_KEY not in "\n".join(c.detail) and FAKE_KEY not in repr(c)


def test_install_files_crlf_source_uploaded_as_lf(tmp_path, monkeypatch):
    src = {}
    for name in ("long_live_worker.py", "long-live.service", "install.sh", "uninstall.sh"):
        p = tmp_path / "bundle" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b"#!/usr/bin/env bash\r\nset -euo pipefail\r\necho INSTALL_OK\recho x\n")
        src[name] = p
    monkeypatch.setattr(cc, "worker_files", lambda: src)
    remote = FakeRemote()
    make_client(tmp_path, remote).install_worker()
    staged = {k.rsplit("/", 1)[1]: v for k, v in remote.files.items() if k.startswith("/tmp/long-live-install-")}
    assert set(staged) == set(src)
    for name, data in staged.items():
        assert b"\r" not in data, name
        assert data == b"#!/usr/bin/env bash\nset -euo pipefail\necho INSTALL_OK\necho x\n"


def test_stdout_stderr_utf8_decode(tmp_path):
    rec = Recorder(stdout="os_id=ubuntu\n한글 출력\n".encode("utf-8"), stderr=b"bad \xff byte\n", rc=0)
    res = client(tmp_path, rec).script("echo hi\n")
    assert res.out == "os_id=ubuntu\n한글 출력\n"
    assert res.err.startswith("bad ") and "\ufffd" in res.err


def test_payload_never_logged(tmp_path):
    rec = Recorder(stdout=b"os_id=ubuntu\n")
    c = client(tmp_path, rec)
    c.script(ENV_SCRIPT)
    c.run(KEY_WRITE_CMD, input_text=FAKE_KEY + "\n")
    detail = "\n".join(c.detail)
    assert 'echo "os_id=${ID:-unknown}"' not in detail  # 스크립트 본문 dump 없음
    assert FAKE_KEY not in detail and FAKE_KEY not in repr(c)
    assert "$ ssh ubuntu@123.45.67.89 bash -s --" in detail  # 명령 요약만


def test_helpers():
    assert to_linux_bytes("a\r\nb\rc\n") == b"a\nb\nc\n"
    assert lf_bytes(b"a\r\nb\rc\n") == b"a\nb\nc\n"


# ---------------- 실제 프로세스: Windows pipe가 받은 bytes 확인 ----------------

def _via(real_cmd_for):
    """CloudClient가 만든 인자/kwargs를 그대로 쓰되 ssh 대신 로컬 프로세스를 실행하는 runner."""
    def runner(args, **kw):
        return subprocess.run(real_cmd_for(args[-1]), **kw)
    return runner


def test_real_windows_pipe_delivers_no_cr(tmp_path):
    """수정 전 코드(text=True)는 여기서 b'set -u\\r\\n...'를 보냈다."""
    child = lambda remote_cmd: [sys.executable, "-c", "import sys;sys.stdout.write(sys.stdin.buffer.read().hex())"]
    c = client(tmp_path, _via(child))
    for name, text in LINUX_SCRIPTS.items():
        res = c.script(text)
        received = bytes.fromhex(res.out)
        assert b"\r" not in received, name
        assert received == text.encode("utf-8"), name


GIT_CAT = next((p for p in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "usr" / "bin" / "cat.exe",)
                if p.is_file()), None)


@pytest.mark.skipif(GIT_CAT is None, reason="Git for Windows not installed")
def test_real_git_msys_stdin_is_byte_transparent(tmp_path):
    """Git for Windows ssh.exe와 같은 MSYS runtime(cat.exe)이 binary stdin을 그대로 받는지 확인."""
    c = client(tmp_path, _via(lambda remote_cmd: [str(GIT_CAT)]))
    for name, text in LINUX_SCRIPTS.items():
        res = c.script(text)
        assert res.rc == 0
        assert "\r" not in res.out and res.out == text, name


def _linux_bash():
    if os.name == "nt" and shutil.which("wsl.exe"):
        try:
            out = subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True, timeout=20).stdout
            names = out.decode("utf-16-le", "ignore").replace("\x00", "").split()
        except (OSError, subprocess.SubprocessError):
            names = []
        if "Ubuntu" in names:
            return "wsl", lambda remote_cmd: ["wsl.exe", "-d", "Ubuntu", "--", "bash", "-c", remote_cmd]
    if os.name != "nt" and shutil.which("bash"):
        return "bash", lambda remote_cmd: ["bash", "-c", remote_cmd]
    return None, None


LINUX_KIND, LINUX_CMD = _linux_bash()
CRLF_ERRORS = ("$'\\r'", "command not found", "invalid option", "unexpected end of file", "syntax error")


@pytest.mark.skipif(LINUX_CMD is None, reason="no Linux bash (WSL Ubuntu) available")
def test_real_linux_bash_runs_env_and_status_scripts(tmp_path):
    """Gate C: CloudClient.script와 같은 payload를 실제 Linux `bash -s --`로 실행."""
    c = client(tmp_path, _via(LINUX_CMD))
    env = c.script(ENV_SCRIPT, timeout=120)
    assert env.rc == 0, env.err
    for bad in CRLF_ERRORS:
        assert bad not in env.err, env.err
    keys = dict(l.split("=", 1) for l in env.out.splitlines() if "=" in l)
    assert {"os_id", "arch", "disk_free_kb", "mem_kb", "python3", "sudo", "apt", "ffmpeg", "shape"} <= set(keys)
    assert keys["os_id"] in ("ubuntu", "debian")
    st = c.script(STATUS_SCRIPT, timeout=120)
    assert st.rc == 0, st.err
    import json
    d = json.loads(st.out.strip().splitlines()[-1])
    assert set(d) == {"active", "enabled", "installed", "status", "disk_free_bytes"}


@pytest.mark.skipif(LINUX_CMD is None, reason="no Linux bash (WSL Ubuntu) available")
def test_real_linux_bash_syntax_of_all_scripts(tmp_path):
    """서버를 바꾸지 않고(bash -n) 모든 Linux 스크립트/고정 명령의 문법을 실제 bash로 확인."""
    root = Path(__file__).resolve().parent.parent
    files = {n: lf_bytes(p.read_bytes()).decode() for n, p in
             (("install.sh", root / "deploy/linux/install.sh"), ("uninstall.sh", root / "deploy/linux/uninstall.sh"))}
    c = client(tmp_path, _via(lambda remote_cmd: LINUX_CMD("bash -n -s")))
    for name, text in {**LINUX_SCRIPTS, **files}.items():
        res = c.script(text, timeout=60)
        assert res.rc == 0 and not res.err.strip(), (name, res.err)
    for name, cmd in (("KEY_WRITE_CMD", KEY_WRITE_CMD), ("CONFIG_WRITE_CMD", CONFIG_WRITE_CMD)):
        res = subprocess.run(LINUX_CMD("bash -n -c " + __import__("shlex").quote(cmd)), capture_output=True, timeout=60)
        assert res.returncode == 0, (name, res.stderr)
