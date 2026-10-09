"""테스트 전용: 실제 사용자 설정 폴더(%APPDATA%\\PlaylistLongVideoMaker)의 READ-ONLY 지문.

pytest 시작 때 settings.json / live_secret*.dat / youtube_token*.dat 의 존재·크기·mtime·SHA256을 기록하고
끝날 때 다시 비교한다. 파일을 수정/복원하지 않고, 내용(비밀)은 출력하지 않는다 (hash/mtime/존재만).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

APP_DIR_NAME = "PlaylistLongVideoMaker"
PATTERNS = ("settings.json", "live_secret*.dat", "youtube_token*.dat")


def real_settings_dir(appdata: str | None = None) -> Path | None:
    base = appdata if appdata is not None else os.environ.get("APPDATA")
    return Path(base) / APP_DIR_NAME if base else None


def snapshot(folder: Path | None) -> dict[str, tuple]:
    """파일 이름 → (크기, mtime_ns, sha256). 폴더가 없으면 {}."""
    out: dict[str, tuple] = {}
    if folder is None or not folder.is_dir():
        return out
    for pattern in PATTERNS:
        for p in sorted(folder.glob(pattern)):
            if p.is_file():
                st = p.stat()
                out[p.name] = (st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def diff(before: dict[str, tuple], after: dict[str, tuple]) -> list[str]:
    """바뀐 파일 설명 (이름 + 무엇이 바뀌었는지만, 내용 없음)."""
    problems = []
    for name in sorted(set(before) | set(after)):
        a, b = before.get(name), after.get(name)
        if a == b:
            continue
        if a is None:
            problems.append(f"{name}: created")
        elif b is None:
            problems.append(f"{name}: deleted")
        else:
            what = [k for k, x, y in zip(("size", "mtime", "sha256"), a, b) if x != y]
            problems.append(f"{name}: changed ({', '.join(what)})")
    return problems
