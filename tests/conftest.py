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
