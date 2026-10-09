"""테스트 설정 격리 회귀: 테스트가 실제 사용자 설정 폴더에 쓸 수 없어야 한다.

- A: 테스트 본문은 테스트별 임시 폴더를 쓴다
- B: 테스트 본문이 끝난 뒤(격리 해제 후) 늦게 저장하는 background thread
- C: '실제' APPDATA 경로에는 파일 생성/변경이 없다 (이 테스트에서는 가짜 APPDATA로 흉내 — 진짜 폴더는 쓰지 않음)
- D: 늦은 저장은 pytest 세션 전용 폴더(PLVM_TEST_SETTINGS_DIR)로만 간다 + writer thread 종료(join) 확인
"""
import json
import os
import threading
import uuid
from pathlib import Path

from app import settings
from settings_guard import diff, snapshot

ORIGINAL_SETTINGS_DIR = settings.settings_dir  # 테스트별 fixture가 바꾸기 전의 실제 함수 (collection 시점)


def test_body_writes_go_to_per_test_dir(_isolated_settings):
    settings.update_settings(isolation_body=True)
    assert settings.settings_dir() == _isolated_settings
    assert json.loads((_isolated_settings / "settings.json").read_text(encoding="utf-8"))["isolation_body"] is True


def test_late_background_write_never_reaches_real_appdata(tmp_path, monkeypatch, _isolated_settings):
    fake_real = tmp_path / "fake_appdata"
    fake_real.mkdir()
    monkeypatch.setenv("APPDATA", str(fake_real))  # '실제' APPDATA 흉내
    key = f"late_{uuid.uuid4().hex[:8]}"
    go = threading.Event()
    errors = []

    def late_writer():
        go.wait(10)
        try:
            settings.update_settings(**{key: True})  # 예: 창이 닫힌 뒤 늦게 끝나는 autosave
        except Exception as e:  # pragma: no cover - 실패 원인 보고용
            errors.append(e)
    t = threading.Thread(target=late_writer, name="late-settings-writer", daemon=True)
    t.start()
    settings.update_settings(in_body=True)  # A
    # 테스트 teardown 흉내: 테스트별 격리(settings_dir 교체)가 풀린 상태
    monkeypatch.setattr(settings, "settings_dir", ORIGINAL_SETTINGS_DIR)
    go.set()  # B
    t.join(10)
    assert not t.is_alive() and not errors  # writer thread 종료 확인
    session_dir = Path(os.environ["PLVM_TEST_SETTINGS_DIR"])
    assert not (fake_real / "PlaylistLongVideoMaker").exists()  # C: 실제 경로 쪽에는 아무것도 생기지 않음
    assert json.loads((session_dir / "settings.json").read_text(encoding="utf-8"))[key] is True  # D
    body = json.loads((_isolated_settings / "settings.json").read_text(encoding="utf-8"))
    assert body.get("in_body") is True and key not in body


def test_production_settings_dir_unchanged_without_test_env(tmp_path, monkeypatch):
    """PLVM_TEST_SETTINGS_DIR이 없으면 기존 경로 그대로 (경로 계산만 확인, 파일은 쓰지 않음)."""
    monkeypatch.delenv("PLVM_TEST_SETTINGS_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    expected = (tmp_path / "appdata" / "PlaylistLongVideoMaker") if os.name == "nt" else (
        Path.home() / ".config" / "PlaylistLongVideoMaker")
    assert ORIGINAL_SETTINGS_DIR() == expected


def test_release_guard_detects_changes_without_printing_contents(tmp_path):
    d = tmp_path / "PlaylistLongVideoMaker"
    d.mkdir()
    (d / "settings.json").write_text('{"a": 1}', encoding="utf-8")
    (d / "live_secret.dat").write_bytes(b"SECRET-BLOB-not-real")
    (d / "youtube_token_abc.dat").write_bytes(b"TOKEN-BLOB-not-real")
    (d / "other.txt").write_text("ignored", encoding="utf-8")
    before = snapshot(d)
    assert sorted(before) == ["live_secret.dat", "settings.json", "youtube_token_abc.dat"]
    assert diff(before, snapshot(d)) == []
    (d / "settings.json").write_text('{"a": 2}', encoding="utf-8")  # 같은 크기, 다른 내용
    (d / "youtube_token_new.dat").write_bytes(b"x")
    (d / "live_secret.dat").unlink()
    problems = diff(before, snapshot(d))
    assert any(p.startswith("settings.json: changed") and "sha256" in p for p in problems)
    assert "youtube_token_new.dat: created" in problems and "live_secret.dat: deleted" in problems
    text = "\n".join(problems)
    assert "SECRET-BLOB" not in text and "TOKEN-BLOB" not in text and '"a"' not in text
    assert snapshot(tmp_path / "missing") == {}
