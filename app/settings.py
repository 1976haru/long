from __future__ import annotations

import json
import os
from pathlib import Path


def settings_dir() -> Path:
    if os.name == "nt" and os.environ.get("APPDATA"):
        p = Path(os.environ["APPDATA"]) / "PlaylistLongVideoMaker"
    else:
        p = Path.home() / ".config" / "PlaylistLongVideoMaker"
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings_file() -> Path:
    return settings_dir() / "settings.json"


def load_settings() -> dict:
    p = settings_file()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    settings_file().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
