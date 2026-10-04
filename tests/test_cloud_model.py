import hashlib
import json
from pathlib import Path

import pytest

from app.cloud_model import (
    REMOTE_KEY, CloudConfigError, CloudProfile, find_ssh, known_hosts_file, load_cloud_profile,
    safe_remote_name, save_cloud_profile, sha256_file, ssh_base_args, validate_host, validate_user, worker_files,
)


@pytest.mark.parametrize("host", ["123.45.67.89", "2001:db8::1", "my-server.example.com"])
def test_valid_hosts(host):
    assert validate_host(f"  {host} ") == host


@pytest.mark.parametrize("host", ["", "  ", "-oProxyCommand=calc", "a b", "x;rm -rf /", "host$(id)", "a@b", "../x", "h\nx"])
def test_invalid_hosts(host):
    with pytest.raises(CloudConfigError):
        validate_host(host)


@pytest.mark.parametrize("user,ok", [("ubuntu", True), ("opc", True), ("-oX", False), ("root;id", False), ("Ubuntu", False), ("", False)])
def test_users(user, ok):
    if ok:
        assert validate_user(user) == user
    else:
        with pytest.raises(CloudConfigError):
            validate_user(user)


def test_profile_validation_key_path(tmp_path):
    with pytest.raises(CloudConfigError, match="찾을 수 없"):
        CloudProfile("1.2.3.4", "ubuntu", str(tmp_path / "none.key")).validated()
    pub = tmp_path / "k.pub"
    pub.write_text("ssh-rsa AAA")
    with pytest.raises(CloudConfigError, match="공개키"):
        CloudProfile("1.2.3.4", "ubuntu", str(pub)).validated()
    assert CloudProfile("2001:db8::1", "ubuntu", "k").destination == "ubuntu@[2001:db8::1]"


def test_profile_saved_without_key_content(tmp_path, _isolated_settings):
    key = tmp_path / "oci.key"
    key.write_text("-----BEGIN FAKE-----\nSECRET-PRIVATE-KEY-BODY\n-----END FAKE-----")
    save_cloud_profile(CloudProfile("1.2.3.4", "ubuntu", str(key)))
    text = (_isolated_settings / "settings.json").read_text(encoding="utf-8")
    assert "SECRET-PRIVATE-KEY-BODY" not in text
    assert json.loads(text)["cloud"] == {"host": "1.2.3.4", "user": "ubuntu", "key_path": str(key), "provider": "OCI_ALWAYS_FREE"}
    assert load_cloud_profile() == CloudProfile("1.2.3.4", "ubuntu", str(key))
    save_cloud_profile(None)
    assert load_cloud_profile() is None


def test_ssh_args_safe(tmp_path):
    key = tmp_path / "k"
    key.write_text("x")
    args = ssh_base_args(Path("ssh.exe"), CloudProfile("1.2.3.4", "ubuntu", str(key)))
    assert args[-2:] == ["--", "ubuntu@1.2.3.4"]  # 옵션 주입 차단
    joined = " ".join(args)
    assert "BatchMode=yes" in joined and "StrictHostKeyChecking=accept-new" in joined
    # 공백 경로가 여러 파일로 나뉘지 않도록 큰따옴표 + / 경로 (SSH 탐색 hotfix에서 수정)
    kh = str(known_hosts_file()).replace(chr(92), "/")
    assert (f'UserKnownHostsFile="{kh}"' if " " in kh else f"UserKnownHostsFile={kh}") in args


@pytest.mark.parametrize("name", ["CHILI LAB EP001.mp4", "곡 모음 1.mp4", "a'; rm -rf ~.mp4", "$(reboot).mp4", "-rf.mp4", "ok_name-1.0.mp4"])
def test_safe_remote_name(name):
    r = safe_remote_name(Path(name))
    assert r.endswith(".mp4")
    assert all(c.isalnum() or c in "._-" for c in r)
    assert r[0].isalnum()
    assert safe_remote_name(Path(name)) == r  # 결정적


def test_safe_remote_name_keeps_clean_names():
    assert safe_remote_name(Path("EP001_LIVE_READY.mp4")) == "EP001_LIVE_READY.mp4"
    assert safe_remote_name(Path("a b.mp4")) != safe_remote_name(Path("a_b.mp4"))


def test_sha256_streaming(tmp_path):
    p = tmp_path / "f.bin"
    data = b"0123456789" * 300_000
    p.write_bytes(data)
    seen = []
    assert sha256_file(p, seen.append, chunk=65536) == hashlib.sha256(data).hexdigest()
    assert len(seen) > 10 and seen[-1] == 1.0


def test_worker_files_present():
    files = worker_files()
    assert all(p.is_file() for p in files.values()), files
    svc = files["long-live.service"].read_text(encoding="utf-8")
    for line in ("User=longlive", "Restart=on-failure", "RestartSec=5", "RestartPreventExitStatus=3", "KillMode=mixed", "MemoryMax=512M"):
        assert line in svc
    inst = files["install.sh"].read_text(encoding="utf-8")
    assert "useradd --system" in inst and "/opt/long-live/media" in inst and "-m 0750 -o root -g longlive /etc/long-live" in inst
    assert "systemctl enable" not in inst  # 설치만, LIVE 시작 시 enable
    assert REMOTE_KEY == "/etc/long-live/stream.key"


def test_find_ssh_returns_path_or_none():
    p = find_ssh()
    assert p is None or p.exists()
