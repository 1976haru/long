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


@pytest.fixture(autouse=True)
def _collect_tk_garbage_on_main_thread():
    """테스트가 만든 Tk root/창 쓰레기를 매 테스트 후 main thread에서 수거한다.

    그대로 두면 다음 테스트의 백그라운드 스레드(FFmpeg reader, SSH probe, polling)에서 cyclic GC가 돌며
    Tcl 객체를 다른 스레드에서 해제해 간헐적 Tk 오류('no display', 'main thread is not in main loop', Tcl abort)가 났다.
    """
    yield
    import gc
    gc.collect()
