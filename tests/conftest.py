"""모든 테스트에서 사용자의 실제 설정 폴더(%APPDATA%)를 건드리지 않도록 임시 폴더로 격리한다."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    import app.cloud_model as cloud_model
    import app.live_secrets as live_secrets
    import app.settings as settings
    d = tmp_path / "_settings"
    d.mkdir()
    for mod in (settings, cloud_model, live_secrets):
        monkeypatch.setattr(mod, "settings_dir", lambda: d)
    yield d


@pytest.fixture(autouse=True, scope="session")
def _retry_transient_tcl_init():
    """이 개발 PC의 백신(파일 접근 감시)이 전체 테스트 중 드물게 Tcl의 init.tcl 읽기를 순간 실패시킨다
    ('couldn't read file .../init.tcl: no such file or directory', 단독 150회 생성은 0회 실패).
    그 경우에만 Tk() 생성을 잠깐 뒤 다시 시도한다 — 앱 코드는 바꾸지 않는다."""
    import time
    import tkinter

    original = tkinter.Tk.__init__

    def patched(self, *args, **kwargs):
        for attempt in range(5):
            try:
                return original(self, *args, **kwargs)
            except tkinter.TclError as e:
                transient = "Can't find a usable" in str(e) or ".tcl" in str(e)  # init.tcl / tk.tcl 순간 읽기 실패
                if not transient or attempt == 4:
                    raise
                time.sleep(0.3)
    tkinter.Tk.__init__ = patched
    yield
    tkinter.Tk.__init__ = original


@pytest.fixture(autouse=True)
def _stop_leftover_cloud_workers():
    """테스트가 assert에서 실패해도 Cloud worker가 띄운 FFmpeg를 남기지 않는다.
    (서버에서는 systemd cgroup이 정리하지만, Windows 테스트에서는 pytest 종료 후 고아 프로세스가 될 수 있었다)"""
    yield
    import gc
    import sys
    mod = sys.modules.get("long_live_worker")
    if mod is None:
        return
    for obj in gc.get_objects():
        if isinstance(obj, mod.Worker) and obj.proc is not None:
            obj.request_stop()
            try:
                obj._stop_ffmpeg()
            except Exception:
                pass


def _ffmpeg_children_of(pid: int) -> list[int]:
    import os
    import subprocess
    if os.name == "nt":
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe' and ParentProcessId={pid}\" "
                              "| ForEach-Object { $_.ProcessId }"], capture_output=True, text=True, timeout=60).stdout
        return [int(x) for x in out.split() if x.strip().isdigit()]
    found = []
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                stat = open(f"/proc/{d}/stat").read()
                if "(ffmpeg)" in stat and int(stat.rsplit(")", 1)[1].split()[1]) == pid:
                    found.append(int(d))
            except (OSError, ValueError, IndexError):
                pass
    return found


@pytest.fixture(autouse=True, scope="session")
def _no_orphan_ffmpeg_from_this_test_session():
    """세션 끝: 이 pytest 프로세스가 띄운 ffmpeg가 남아 있으면 정리하고 실패시킨다 (다른 프로그램의 ffmpeg는 건드리지 않음)."""
    yield
    import os
    left = _ffmpeg_children_of(os.getpid())
    for pid in left:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    assert not left, f"테스트가 종료되지 않은 ffmpeg를 남겼습니다: {left}"


@pytest.fixture(autouse=True)
def _collect_tk_garbage_on_main_thread():
    """테스트가 만든 Tk root/창 쓰레기를 매 테스트 후 main thread에서 수거한다.

    그대로 두면 다음 테스트의 백그라운드 스레드(FFmpeg reader, SSH probe, polling)에서 cyclic GC가 돌며
    Tcl 객체를 다른 스레드에서 해제해 간헐적 Tk 오류('no display', 'main thread is not in main loop', Tcl abort)가 났다.
    """
    yield
    import gc
    gc.collect()
