"""이 프로그램이 오늘 호출한 YouTube API만 세는 참고용 카운터 (settings.json "api_usage", 비밀 없음).

Google Cloud의 실제 quota 집계(태평양 시간 자정 초기화, 다른 프로그램 사용분 포함)와 같지 않다 — 화면에도 그렇게 표시한다.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Callable, Iterable

from .settings import SETTINGS_LOCK, load_settings, save_settings
from .youtube_api import QUOTA_COSTS

KEY = "api_usage"


def _today(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock()).strftime("%Y-%m-%d")


def record_api_calls(calls: Iterable[str], clock: Callable[[], float] = time.time) -> None:
    """작업 1개가 끝날 때 그 api client의 calls 목록을 더한다. 실패해도 업로드에는 영향 없음."""
    try:
        calls = list(calls)
        if not calls:
            return
        with SETTINGS_LOCK:
            data = load_settings()
            cur = data.get(KEY) if isinstance(data.get(KEY), dict) else {}
            if cur.get("date") != _today(clock):
                cur = {"date": _today(clock), "units": 0, "uploads": 0}
            cur["units"] = int(cur.get("units", 0)) + sum(QUOTA_COSTS.get(c, 1) for c in calls)
            cur["uploads"] = int(cur.get("uploads", 0)) + sum(c == "videos.insert" for c in calls)
            data[KEY] = cur
            save_settings(data)
    except Exception:  # pragma: no cover - 참고용 카운터
        pass


def today_usage(clock: Callable[[], float] = time.time) -> dict:
    cur = load_settings().get(KEY)
    if not isinstance(cur, dict) or cur.get("date") != _today(clock):
        return {"units": 0, "uploads": 0}
    return {"units": int(cur.get("units", 0)), "uploads": int(cur.get("uploads", 0))}


def usage_text(clock: Callable[[], float] = time.time) -> str:
    u = today_usage(clock)
    return (f"오늘 이 프로그램: 영상 업로드 {u['uploads']}회 · API 예상 사용량 약 {u['units']:,} units "
            "(이 프로그램이 호출한 것만 센 참고값 · Google Cloud 실제 quota와 다를 수 있음)")
