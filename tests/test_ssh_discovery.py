"""SSH 연결 도구 자동 탐색 (Windows OpenSSH 없이 Git/GitHub Desktop ssh 사용) — 실제 OCI 접속 없음."""
import json
import subprocess
import time
from pathlib import Path

import pytest

from app.cloud_model import (
    GIT_FOR_WINDOWS_URL, SSH_INVALID, SSH_MISSING, CloudConfigError, CloudProfile, SshTool, discover_ssh,
    discover_ssh_candidates, find_ssh, load_cloud_profile, load_saved_ssh, probe_ssh_executable, save_cloud_profile,
    save_ssh_tool, ssh_base_args, ssh_option_path, validate_manual_ssh,
)


def touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")
    return p


class Env:
    """가짜 Windows 환경: 폴더 구조 + PATH(which) + probe 결과."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.env = {"SystemRoot": str(tmp / "Windows"), "ProgramFiles": str(tmp / "PF"),
                    "ProgramFiles(x86)": str(tmp / "PF86"), "LOCALAPPDATA": str(tmp / "LA")}
        self.path = {}
        self.ok: dict[str, str] = {}

    def which(self, name):
        return self.path.get(name)

    def probe(self, p):
        return self.ok.get(str(Path(p)))

    def make(self, rel, ok=True, version="OpenSSH_9.9p1"):
        p = touch(self.tmp / rel)
        if ok:
            self.ok[str(p)] = version
        return p

    def candidates(self, saved=None):
        return discover_ssh_candidates(saved=saved, env=self.env, which=self.which, is_windows=True)

    def discover(self, saved=None):
        return discover_ssh(saved=saved, probe=self.probe, env=self.env, which=self.which, is_windows=True)


@pytest.fixture
def fake(tmp_path):
    return Env(tmp_path)


def test_windows_openssh_found(fake):
    p = fake.make("Windows/System32/OpenSSH/ssh.exe")
    tool = fake.discover()
    assert tool.path == p and tool.source == "windows" and tool.label == "Windows OpenSSH"


def test_path_ssh_found(fake):
    p = fake.make("tools/ssh.exe")
    fake.path["ssh"] = str(p)
    tool = fake.discover()
    assert tool.path == p and tool.label == "PATH의 SSH"


def test_program_files_git_found_without_path(fake):
    p = fake.make("PF/Git/usr/bin/ssh.exe")
    tool = fake.discover()
    assert tool.path == p and tool.label == "Git for Windows SSH"


def test_program_files_x86_and_localappdata_git(fake):
    p86 = fake.make("PF86/Git/usr/bin/ssh.exe")
    assert fake.discover().path == p86
    fake.ok.clear()
    la = fake.make("LA/Programs/Git/usr/bin/ssh.exe")
    tool = fake.discover()
    assert tool.path == la and tool.source == "git"


@pytest.mark.parametrize("git_rel,ssh_rel", [
    ("D/MyGit/cmd/git.exe", "D/MyGit/usr/bin/ssh.exe"),
    ("D/MyGit/bin/git.exe", "D/MyGit/usr/bin/ssh.exe"),
    ("D/MyGit/mingw64/bin/git.exe", "D/MyGit/usr/bin/ssh.exe"),
    ("D/MyGit/cmd/git.exe", "D/MyGit/mingw64/bin/ssh.exe"),
])
def test_git_root_inferred_from_git_exe(fake, git_rel, ssh_rel):
    fake.path["git"] = str(touch(fake.tmp / git_rel))
    p = fake.make(ssh_rel)
    tool = fake.discover()
    assert tool.path == p and tool.label == "Git for Windows SSH"


def test_github_desktop_newest_version_first(fake):
    old = fake.make("LA/GitHubDesktop/app-3.6.2/resources/app/git/usr/bin/ssh.exe")
    new = fake.make("LA/GitHubDesktop/app-3.6.10/resources/app/git/usr/bin/ssh.exe")
    mid = fake.make("LA/GitHubDesktop/app-3.6.4/resources/app/git/mingw64/bin/ssh.exe")
    paths = [c.path for c in fake.candidates()]
    assert paths == [new, mid, old]  # 3.6.10 > 3.6.4 > 3.6.2 (숫자 비교)
    tool = fake.discover()
    assert tool.path == new and tool.label == "GitHub Desktop SSH"


def test_existing_but_broken_candidate_is_skipped(fake):
    fake.make("Windows/System32/OpenSSH/ssh.exe", ok=False)  # 파일은 있지만 ssh -V 실패
    good = fake.make("LA/GitHubDesktop/app-3.6.4/resources/app/git/usr/bin/ssh.exe")
    tool = fake.discover()
    assert tool.path == good


def test_priority_order(fake):
    saved = fake.make("custom/ssh.exe")
    win = fake.make("Windows/System32/OpenSSH/ssh.exe")
    pth = fake.make("tools/ssh.exe"); fake.path["ssh"] = str(pth)
    git = fake.make("PF/Git/usr/bin/ssh.exe")
    gd = fake.make("LA/GitHubDesktop/app-3.6.4/resources/app/git/usr/bin/ssh.exe")
    order = [c.path for c in fake.candidates(saved=(str(saved), "manual"))]
    assert order == [saved, win, pth, git, gd]
    assert fake.discover(saved=(str(saved), "manual")).path == saved
    assert fake.discover().path == win


def test_duplicates_removed_and_path_classified(fake):
    git = fake.make("PF/Git/usr/bin/ssh.exe")
    fake.path["ssh"] = str(git)  # Git usr/bin이 PATH에 있는 경우
    cands = fake.candidates()
    assert [c.path for c in cands] == [git]
    assert cands[0].label == "Git for Windows SSH"


def test_saved_invalid_path_falls_back(fake):
    gone = fake.tmp / "removed/ssh.exe"
    win = fake.make("Windows/System32/OpenSSH/ssh.exe")
    assert fake.discover(saved=(str(gone), "github_desktop")).path == win


def test_nothing_found_returns_none_without_crash(fake):
    fake.make("PF/Git/usr/bin/ssh.exe", ok=False)
    assert fake.discover() is None

    def boom(name):
        raise RuntimeError("which failed")
    assert discover_ssh(probe=fake.probe, env=fake.env, which=boom, is_windows=True) is None


def test_manual_selection_requires_probe(fake):
    good = fake.make("anywhere/my-ssh.exe")
    tool = validate_manual_ssh(good, probe=fake.probe)
    assert tool.source == "manual" and tool.label == "직접 지정한 SSH"
    named_ssh = touch(fake.tmp / "fake/ssh.exe")  # 이름만 ssh.exe
    with pytest.raises(CloudConfigError, match="ssh -V"):
        validate_manual_ssh(named_ssh, probe=fake.probe)
    with pytest.raises(CloudConfigError):
        validate_manual_ssh(fake.tmp / "none.exe", probe=fake.probe)


# ---------------- probe ----------------

def test_probe_reads_stderr_and_stdout(tmp_path):
    exe = touch(tmp_path / "ssh.exe")
    calls = []

    def runner(args, **kw):
        calls.append((args, kw))
        return subprocess.CompletedProcess(args, 0, "", "OpenSSH_for_Windows_9.5p1, LibreSSL 3.8.2\n")
    assert probe_ssh_executable(exe, runner=runner) == "OpenSSH_for_Windows_9.5p1, LibreSSL 3.8.2"
    args, kw = calls[0]
    assert args == [str(exe), "-V"] and kw.get("shell") is not True and kw["timeout"] <= 5
    assert "creationflags" in kw
    out = lambda a, **k: subprocess.CompletedProcess(a, 0, "OpenSSH_10.2p1\n", "")
    assert probe_ssh_executable(exe, runner=out) == "OpenSSH_10.2p1"


@pytest.mark.parametrize("behavior", ["notssh", "oserror", "timeout", "weird"])
def test_probe_failures_return_none(tmp_path, behavior):
    exe = touch(tmp_path / "ssh.exe")

    def runner(args, **kw):
        if behavior == "notssh":
            return subprocess.CompletedProcess(args, 0, "Python 3.14\n", "")
        if behavior == "oserror":
            raise OSError("bad exe")
        if behavior == "timeout":
            raise subprocess.TimeoutExpired(args, 5)
        raise RuntimeError("anything")
    assert probe_ssh_executable(exe, runner=runner) is None
    assert probe_ssh_executable(tmp_path / "missing.exe") is None


# ---------------- 저장 / 호환 ----------------

def test_saved_ssh_reload_and_only_path_stored(tmp_path, _isolated_settings):
    key = tmp_path / "oci.key"
    key.write_text("-----BEGIN FAKE-----\nSECRET-KEY-BODY-XYZ\n-----END FAKE-----")
    save_cloud_profile(CloudProfile("1.2.3.4", "ubuntu", str(key)))
    save_ssh_tool(SshTool(Path(r"C:\Program Files\Git\usr\bin\ssh.exe"), "git", "OpenSSH_9.9p1"))
    data = json.loads((_isolated_settings / "settings.json").read_text(encoding="utf-8"))
    assert data["cloud_ssh"] == {"path": r"C:\Program Files\Git\usr\bin\ssh.exe", "source": "git"}
    text = json.dumps(data)
    assert "SECRET-KEY-BODY-XYZ" not in text and "OpenSSH_9.9p1" not in text
    assert load_saved_ssh() == (r"C:\Program Files\Git\usr\bin\ssh.exe", "git")
    assert load_cloud_profile() == CloudProfile("1.2.3.4", "ubuntu", str(key))
    save_ssh_tool(None)
    assert load_saved_ssh() is None


def test_old_settings_backward_compatible(_isolated_settings):
    old = {"ffmpeg_path": "C:/ffmpeg/bin/ffmpeg.exe", "queue": [],
           "cloud": {"host": "1.2.3.4", "user": "ubuntu", "key_path": "C:/k.key", "provider": "OCI_ALWAYS_FREE"}}
    (_isolated_settings / "settings.json").write_text(json.dumps(old), encoding="utf-8")
    assert load_saved_ssh() is None
    assert load_cloud_profile() == CloudProfile("1.2.3.4", "ubuntu", "C:/k.key")
    (_isolated_settings / "settings.json").write_text(json.dumps({**old, "cloud_ssh": "garbage"}), encoding="utf-8")
    assert load_saved_ssh() is None


def test_find_ssh_uses_saved_path_first(tmp_path, _isolated_settings):
    exe = touch(tmp_path / "chosen/ssh.exe")
    save_ssh_tool(SshTool(exe, "manual", "OpenSSH_x"))
    seen = []
    assert find_ssh(probe=lambda p: seen.append(Path(p)) or "OpenSSH_x") == exe
    assert seen[0] == exe


# ---------------- 명령 인자 호환 (공백/드라이브 경로) ----------------

def test_ssh_option_path_quoting():
    assert ssh_option_path(r"C:\Users\Hong Gil\AppData\Roaming\PLVM\cloud_known_hosts") == \
        '"C:/Users/Hong Gil/AppData/Roaming/PLVM/cloud_known_hosts"'
    assert ssh_option_path(r"D:\50%\kh") == "D:/50%%/kh"
    # 공백이 없으면 따옴표를 붙이지 않는다 (Git/MSYS ssh가 \" 를 잘못 해석하는 문제 회피)
    assert ssh_option_path(r"C:\Users\user\AppData\Roaming\PLVM\cloud_known_hosts") == \
        "C:/Users/user/AppData/Roaming/PLVM/cloud_known_hosts"


def test_base_args_with_spaced_paths_are_single_arguments(tmp_path):
    d = tmp_path / "03 long" / "My Keys"
    key = touch(d / "oci key.key")
    args = ssh_base_args(Path(r"C:\Program Files\Git\usr\bin\ssh.exe"), CloudProfile("1.2.3.4", "ubuntu", str(key)))
    assert args[0] == r"C:\Program Files\Git\usr\bin\ssh.exe"
    assert args[args.index("-i") + 1] == str(key)  # 공백 포함 경로도 인자 하나
    kh = next(a for a in args if a.startswith("UserKnownHostsFile="))
    value = kh[len("UserKnownHostsFile="):]
    assert "\\" not in kh and (value.startswith('"') == any(c.isspace() for c in value))
    assert args[-2:] == ["--", "ubuntu@1.2.3.4"]


REAL_TOOLS = [c for c in discover_ssh_candidates() if probe_ssh_executable(c.path)]


@pytest.mark.skipif(not REAL_TOOLS, reason="no ssh executable on this machine")
@pytest.mark.parametrize("kh_rel", ["plain/cloud_known_hosts", "a  b/known hosts"])
def test_real_ssh_probe_and_option_parsing(tmp_path, monkeypatch, kh_rel):
    """Gate C: 이 PC의 실제 ssh 전부(Windows/Git/GitHub Desktop) — ssh -V, 그리고 `ssh -G`(접속 없이 설정만 해석)로
    앱이 만드는 인자 그대로 known_hosts(공백 유무)·key 경로가 올바르게 해석되는지 확인."""
    import app.cloud_model as cm
    kh = tmp_path / kh_rel
    monkeypatch.setattr(cm, "known_hosts_file", lambda: kh)
    key = touch(tmp_path / "03 long" / "k e y.key")
    for c in REAL_TOOLS:
        assert "OpenSSH" in probe_ssh_executable(c.path)
        args = ssh_base_args(c.path, CloudProfile("203.0.113.10", "ubuntu", str(key)))
        i = args.index("--")
        res = subprocess.run(args[:i] + ["-G"] + args[i:], capture_output=True, text=True, timeout=15,
                             stdin=subprocess.DEVNULL)
        assert res.returncode == 0, (c.path, res.stderr)
        conf = {l.split(" ", 1)[0]: l.split(" ", 1)[1] for l in res.stdout.splitlines() if " " in l}
        # 이중 공백까지 보존 = 공백 경로가 파일 1개로 해석됨
        assert conf["userknownhostsfile"].replace("\\", "/") == str(kh).replace("\\", "/"), c.path
        assert conf["batchmode"] == "yes" and conf["user"] == "ubuntu" and conf["hostname"] == "203.0.113.10"
        assert conf["stricthostkeychecking"] == "accept-new" and conf["identitiesonly"] == "yes"


# ---------------- Wizard STEP 3 (Gate D) ----------------

@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    r.withdraw()
    yield r
    r.destroy()


def pump(root, cond, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.02)
    return cond()


GIT_TOOL = SshTool(Path(r"C:\Program Files\Git\usr\bin\ssh.exe"), "git", "OpenSSH_9.9p1")


def wizard(root, **kw):
    from app.cloud_setup_ui import CloudSetupWizard
    opened = []
    wz = CloudSetupWizard(root, open_url=opened.append, **kw)
    wz._go(3)
    return wz, opened


def test_wizard_enables_connect_with_git_ssh_and_saves(root, _isolated_settings):
    wz, _ = wizard(root, ssh_discover=lambda: GIT_TOOL)
    assert pump(root, lambda: not wz.ssh_searching)
    assert wz.ssh_msg.get() == "✓ Git for Windows SSH"
    assert str(wz.btn_conn.cget("state")) == "normal"
    assert not wz.btn_git_help.winfo_manager()
    assert load_saved_ssh() == (str(GIT_TOOL.path), "git")
    assert not wz.lbl_ssh_path.winfo_manager()  # 경로는 [상세]에서만
    wz._toggle_ssh_detail(); root.update()
    assert str(GIT_TOOL.path) in wz.ssh_path_msg.get()
    wz.destroy()


def test_wizard_no_ssh_is_friendly_and_recovers(root):
    state = {"tool": None}
    wz, opened = wizard(root, ssh_discover=lambda: state["tool"])
    assert pump(root, lambda: not wz.ssh_searching)
    root.update()
    assert str(wz.btn_conn.cget("state")) == "disabled"
    assert wz.conn_msg.get() == SSH_MISSING and "필수가 아닙니다" in SSH_MISSING
    assert "선택적 기능" not in SSH_MISSING
    assert wz.btn_git_help.winfo_manager() == "pack"
    wz.btn_git_help.invoke()
    assert opened == [GIT_FOR_WINDOWS_URL]  # 설치는 안내만 (자동 설치 없음)
    state["tool"] = GIT_TOOL  # 사용자가 Git 설치 후 [다시 찾기] (재시작 불필요)
    wz.btn_ssh_find.invoke()
    assert pump(root, lambda: not wz.ssh_searching)
    assert str(wz.btn_conn.cget("state")) == "normal" and wz.conn_msg.get() == ""
    wz.destroy()


def test_wizard_discovery_exception_does_not_crash(root):
    def boom():
        raise RuntimeError("x")
    wz, _ = wizard(root, ssh_discover=boom)
    assert pump(root, lambda: not wz.ssh_searching)
    assert wz.ssh_tool is None and str(wz.btn_conn.cget("state")) == "disabled"
    wz.destroy()


def test_wizard_manual_selection(root, tmp_path, _isolated_settings):
    good = touch(tmp_path / "Git Portable/usr/bin/ssh.exe")
    bad = touch(tmp_path / "fake/ssh.exe")
    picks = [str(bad), str(good)]
    probe = lambda p: "OpenSSH_9.9p1" if Path(p) == good else None
    wz, _ = wizard(root, ssh_discover=lambda: None, ssh_probe=probe, pick_file=lambda **kw: picks.pop(0))
    assert pump(root, lambda: not wz.ssh_searching)
    wz.btn_ssh_pick.invoke()
    assert pump(root, lambda: not wz.ssh_searching)
    assert wz.conn_msg.get() == SSH_INVALID and wz.ssh_tool is None
    assert str(wz.btn_conn.cget("state")) == "disabled"
    wz.btn_ssh_pick.invoke()
    assert pump(root, lambda: not wz.ssh_searching)
    assert wz.ssh_tool.path == good and wz.ssh_msg.get() == "✓ 직접 지정한 SSH"
    assert str(wz.btn_conn.cget("state")) == "normal"
    assert load_saved_ssh() == (str(good), "manual")
    wz.destroy()


def test_wizard_connection_uses_discovered_ssh(root, tmp_path):
    from app.cloud_client import CloudClient
    key = touch(tmp_path / "oci.key")
    wz, _ = wizard(root, ssh_discover=lambda: GIT_TOOL)
    assert pump(root, lambda: not wz.ssh_searching)
    client = wz._client_factory(CloudProfile("1.2.3.4", "ubuntu", str(key)).validated())
    assert isinstance(client, CloudClient) and client.ssh == GIT_TOOL.path
    wz.destroy()


def _gc_in_worker_thread():
    import gc
    import threading
    t = threading.Thread(target=gc.collect)
    t.start()
    t.join(10)


@pytest.mark.parametrize("which", ["wizard", "live"])
def test_destroyed_windows_are_safe_from_background_gc(root, which):
    """원인 재현: 파괴된 창이 참조 순환에 남으면 cyclic GC가 다른 스레드에서 돌 때 StringVar.__del__이
    'main thread is not in main loop'를 일으켜 Tk가 깨진다 (간헐적 'no display'/멈춤의 원인).
    destroy 시 main thread에서 Variable을 정리하므로 다른 스레드 GC에서도 오류가 없어야 한다."""
    import gc
    import sys
    from app.cloud_setup_ui import CloudSetupWizard
    from app.live_secrets import SessionStreamKeyStore
    from app.live_ui import LiveWindow, default_cloud_client

    bad = []
    gc.collect()  # 이전 테스트가 남긴 Tk 쓰레기(파괴된 root 등)는 main thread에서 먼저 정리
    old_hook = sys.unraisablehook
    sys.unraisablehook = lambda u: bad.append(repr(u.exc_value))
    gc.disable()  # 이 창의 순환 해제를 아래 worker thread GC에서만 일어나게 고정
    try:
        if which == "wizard":
            w = CloudSetupWizard(root, open_url=lambda u: None, ssh_discover=lambda: GIT_TOOL)
            w._go(3)
            pump(root, lambda: not w.ssh_searching)
        else:
            w = LiveWindow(root, tools=lambda: (None, None), key_store=SessionStreamKeyStore())
        w._cycle = w  # 순환 참조를 확실히 만든다
        w.destroy()
        end = time.monotonic() + 0.6
        while time.monotonic() < end:
            root.update()
            time.sleep(0.05)
        del w
        _gc_in_worker_thread()
    finally:
        gc.enable()
        sys.unraisablehook = old_hook
    assert not any("main loop" in b for b in bad), bad
    assert not hasattr(default_cloud_client, "__self__")  # Cloud 스레드 factory는 창에 묶이지 않음
