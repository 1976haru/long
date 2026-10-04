"""Cloud/LIVE 보안 전수 검사: shell=True 금지, 유료 Cloud API 금지, Stream Key 노출 금지."""
import ast
from pathlib import Path

from cloud_fakes import FakeRemote, make_client

from app.cloud_client import CONFIG_WRITE_CMD, KEY_WRITE_CMD

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted(list((ROOT / "app").glob("*.py")) + list((ROOT / "cloud").glob("*.py")))
FAKE_KEY = "dummy-sec-0000-not-real"


def test_no_shell_true_anywhere():
    for f in SOURCES:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "shell":
                        assert not (isinstance(kw.value, ast.Constant) and kw.value.value is True), f"shell=True in {f.name}"
        assert "os.system(" not in f.read_text(encoding="utf-8"), f.name


def test_no_cloud_provider_api_or_ssh_library():
    banned_imports = {"oci", "boto3", "botocore", "google", "azure", "paramiko", "fabric", "asyncssh", "requests", "keyring", "cryptography"}
    for f in SOURCES:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            assert not (set(names) & banned_imports), f"{f.name}: {names}"
        text = f.read_text(encoding="utf-8").lower()
        for api in ("iaas.", "/20160918/", "launchinstance", "billing", "subscribe"):
            assert api not in text, f"{f.name}: {api}"


def test_worker_has_no_gui_or_windows_imports():
    tree = ast.parse((ROOT / "cloud" / "long_live_worker.py").read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    assert not mods & {"tkinter", "ctypes", "winreg", "msvcrt", "app"}


def test_key_write_command_is_fixed_and_atomic():
    assert FAKE_KEY not in KEY_WRITE_CMD
    for part in ("umask 077", "stream.key.part", "chmod 600", "chown longlive:longlive", "mv -f"):
        assert part in KEY_WRITE_CMD
    assert KEY_WRITE_CMD.index("chmod 600") < KEY_WRITE_CMD.index("mv -f")
    assert "live.json.part" in CONFIG_WRITE_CMD and "chmod 640" in CONFIG_WRITE_CMD


def test_stream_key_never_on_command_line_or_detail_or_status(tmp_path):
    remote = FakeRemote()
    c = make_client(tmp_path, remote)
    c.prepare()
    m = tmp_path / "x_LIVE_READY.mp4"
    m.write_bytes(b"data" * 100)
    up = c.upload_media(m)
    c.start_live(remote_media=up.remote_name, ingest_url="rtmps://a.rtmps.youtube.com:443/live2",
                 stream_key=FAKE_KEY, sleep=lambda s: None)
    for call in remote.calls:
        assert not any(FAKE_KEY in a for a in call["args"])
    assert FAKE_KEY not in "\n".join(c.detail)
    assert FAKE_KEY not in repr(c.status())
    assert FAKE_KEY not in "\n".join(c.logs())


def test_gitignore_and_repo_have_no_real_secrets():
    for f in ROOT.rglob("*"):
        if any(part in (".git", "__pycache__", ".pytest_cache") for part in f.parts) or not f.is_file():
            continue
        if f.suffix.lower() in (".py", ".md", ".json", ".sh", ".service", ".txt", ".bat", ".yml"):
            text = f.read_text(encoding="utf-8", errors="ignore")
            assert "stream.key" not in f.name
            for marker in ("RSA", "OPENSSH", "EC"):
                assert ("BEGIN " + marker + " PRIVATE KEY") not in text, f
