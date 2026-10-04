import json
import os
from pathlib import Path

import pytest

from app.live_secrets import (
    SessionStreamKeyStore, WindowsDpapiStreamKeyStore, default_key_store, dpapi_protect, dpapi_unprotect,
)

# 테스트 전용 dummy (실제 Stream Key 아님)
FAKE_KEY = "dummy-test-0000-not-real"

windows_only = pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")


@windows_only
def test_dpapi_round_trip_and_clear(tmp_path: Path):
    store = WindowsDpapiStreamKeyStore(tmp_path / "live_secret.dat")
    assert store.get() is None
    store.set(FAKE_KEY)
    assert store.has_saved()
    assert store.get() == FAKE_KEY
    store.clear()
    assert not store.has_saved()
    assert store.get() is None
    store.clear()  # 없는 파일 clear도 안전


@windows_only
def test_plaintext_key_not_stored(tmp_path: Path):
    path = tmp_path / "live_secret.dat"
    WindowsDpapiStreamKeyStore(path).set(FAKE_KEY)
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-16-le", "utf-16-be"):
        assert FAKE_KEY.encode(enc) not in raw
    assert not list(tmp_path.glob("*.tmp"))


@windows_only
def test_empty_set_clears(tmp_path: Path):
    store = WindowsDpapiStreamKeyStore(tmp_path / "s.dat")
    store.set(FAKE_KEY)
    store.set("   ")
    assert not store.has_saved()


@windows_only
def test_corrupted_blob_returns_none_without_leaking(tmp_path: Path, caplog):
    path = tmp_path / "s.dat"
    path.write_bytes(b"not a dpapi blob")
    assert WindowsDpapiStreamKeyStore(path).get() is None
    assert FAKE_KEY not in caplog.text


@windows_only
def test_dpapi_primitives():
    blob = dpapi_protect(b"abc")
    assert blob != b"abc"
    assert dpapi_unprotect(blob) == b"abc"


@windows_only
def test_store_repr_has_no_key(tmp_path: Path):
    store = WindowsDpapiStreamKeyStore(tmp_path / "s.dat")
    store.set(FAKE_KEY)
    assert FAKE_KEY not in repr(store)


def test_non_windows_fallback_is_memory_only(tmp_path: Path):
    store = default_key_store(is_windows=False)
    assert isinstance(store, SessionStreamKeyStore)
    assert store.persistent is False
    store.set(FAKE_KEY)
    assert store.get() == FAKE_KEY
    assert FAKE_KEY not in repr(store)
    store.clear()
    assert store.get() is None


def test_windows_default_uses_dpapi_file(tmp_path: Path):
    store = default_key_store(is_windows=True, path=tmp_path / "x.dat")
    assert isinstance(store, WindowsDpapiStreamKeyStore)
    assert store.persistent is True
    assert store.path.name == "x.dat"


@windows_only
def test_key_never_written_to_settings_json(tmp_path: Path, monkeypatch):
    """DPAPI 저장 파일은 settings.json과 분리되어 있고 settings.json에는 key가 없다."""
    import app.settings as settings
    monkeypatch.setattr(settings, "settings_dir", lambda: tmp_path)
    settings.save_settings({"queue": []})
    store = WindowsDpapiStreamKeyStore(tmp_path / "live_secret.dat")
    store.set(FAKE_KEY)
    text = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert FAKE_KEY not in text
    assert "stream" not in json.loads(text)
