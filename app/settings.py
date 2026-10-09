from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

# 업로드 대기열(작업 스레드)과 UI가 같은 파일을 쓴다. 같은 프로세스 안의 읽기/쓰기는 이 잠금으로 순서를 맞춘다
# (Windows는 다른 곳에서 파일을 열고 있으면 교체가 실패하므로 읽기도 잠금 안에서 한다).
_SAVE_LOCK = threading.RLock()
SETTINGS_LOCK = _SAVE_LOCK  # 중첩 값을 읽기-수정-쓰기 하는 곳에서 with SETTINGS_LOCK: 로 사용
_REPLACE_RETRIES = (0.05, 0.1, 0.2, 0.4, 0.8)  # 백신/색인 프로그램이 잠깐 파일을 잡고 있을 때


TEST_SETTINGS_ENV = "PLVM_TEST_SETTINGS_DIR"  # 테스트 전용: pytest 프로세스 전체가 실제 사용자 설정 폴더를 쓰지 않게


def settings_dir() -> Path:
    test_dir = os.environ.get(TEST_SETTINGS_ENV)
    if test_dir:  # 테스트에서만 설정된다 (프로덕션 실행에는 없음 → 아래 기존 경로 그대로)
        p = Path(test_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p
    if os.name == "nt" and os.environ.get("APPDATA"):
        p = Path(os.environ["APPDATA"]) / "PlaylistLongVideoMaker"
    else:
        p = Path.home() / ".config" / "PlaylistLongVideoMaker"
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings_file() -> Path:
    return settings_dir() / "settings.json"


def load_settings() -> dict:
    with _SAVE_LOCK:
        p = settings_file()
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def _replace(tmp: Path, p: Path) -> None:
    for delay in _REPLACE_RETRIES:
        try:
            os.replace(tmp, p)
            return
        except PermissionError:  # Windows sharing violation: 잠시 뒤 다시
            time.sleep(delay)
    os.replace(tmp, p)


def save_settings(data: dict) -> None:
    """임시 파일에 다 쓴 뒤 교체한다. 쓰는 도중 오류/종료가 나도 기존 settings.json은 그대로 남는다."""
    with _SAVE_LOCK:
        p = settings_file()
        text = json.dumps(data, ensure_ascii=False, indent=2)  # 직렬화 실패는 파일을 건드리기 전에
        tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            _replace(tmp, p)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


def update_settings(**updates) -> dict:
    """읽기-수정-쓰기를 잠금 안에서 한 번에 (다른 스레드의 저장과 섞이지 않게)."""
    with _SAVE_LOCK:
        data = load_settings()
        data.update(updates)
        save_settings(data)
        return data
