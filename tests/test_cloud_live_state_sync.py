"""Cloud LIVE stale overview hotfix regression tests (no real OCI/YouTube access)."""
from types import SimpleNamespace

import pytest

from app.cloud_client import ChannelLive, CloudLiveOverview, CloudStatus
from app.cloud_model import MAX_CONCURRENT_LIVE
from app.live_ui import OVERVIEW_INTERVAL_SECONDS, LiveWindow, cloud_runtime_text, cloud_start_decision


def overview(*, slots=0, lives=(), scheduled=()):
    return CloudLiveOverview(
        slots_used=slots,
        max_concurrent=MAX_CONCURRENT_LIVE,
        lives=[ChannelLive(profile_id=pid, live=live, state="RUNNING" if live else "STOPPED")
               for pid, live in lives],
        scheduled_running=list(scheduled),
    )


def test_cached_two_fresh_zero_allows_start():
    cached = overview(slots=2, lives=(("a", True), ("b", True)))
    fresh = overview(slots=0, lives=(("a", False), ("b", False)))
    assert cached.live_count == 2
    assert fresh.live_count == 0
    assert cloud_start_decision(fresh, "new") == "start"


def test_cached_zero_fresh_two_blocks_third_start():
    cached = overview(slots=0)
    fresh = overview(slots=2, lives=(("a", True), ("b", True)))
    assert cached.live_count == 0
    assert cloud_start_decision(fresh, "new") == "busy"


def test_fresh_check_failure_does_not_use_stale_cache():
    assert cloud_start_decision(None, "new") == "start"


def test_already_live_profile_syncs_instead_of_starting():
    fresh = overview(slots=1, lives=(("default", True),))
    assert cloud_start_decision(fresh, "default") == "sync"


def test_scheduled_live_counts_toward_two_channel_limit():
    fresh = overview(slots=0, lives=(("a", True),), scheduled=("scheduled-b",))
    assert fresh.live_count == 2
    assert cloud_start_decision(fresh, "new") == "busy"


def test_periodic_overview_interval_is_ten_to_fifteen_seconds():
    assert 10.0 <= OVERVIEW_INTERVAL_SECONDS <= 15.0
    # 실패해도 마지막 성공 시각이 아닌 마지막 요청 시각으로 rate-limit한다.
    import inspect

    assert "_overview_last_request" in inspect.getsource(LiveWindow._tick)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (CloudStatus(service_active=False, state="STOPPED", runtime_seconds=144 * 3600), "-"),
        (CloudStatus(service_active=True, state="RUNNING", runtime_seconds=65), "00:01:05"),
    ],
)
def test_runtime_is_only_current_for_live_states(status, expected):
    assert cloud_runtime_text(status) == expected


def test_overview_busy_guard_allows_only_one_worker(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        _overview_busy=True,
        _overview_refresh_pending=False,
    )
    assert LiveWindow._overview_async(fake, force=True) is False
    assert fake._overview_refresh_pending is True
    assert calls == []


def test_recheck_requests_profile_status_and_global_overview(monkeypatch):
    import app.live_ui as live_ui

    calls = []
    fake = SimpleNamespace(
        cloud=SimpleNamespace(client="old", check_async=lambda: calls.append("status")),
        _cloud_msg="",
        _overview_async=lambda **kw: calls.append(("overview", kw)),
        _update_cloud_line=lambda: calls.append("line"),
    )
    monkeypatch.setattr(live_ui, "load_cloud_profile", lambda: object())
    LiveWindow._cloud_recheck(fake)
    assert fake.cloud.client is None
    assert calls == ["status", ("overview", {"force": True}), "line"]


def test_late_overview_queue_result_never_touches_destroyed_tk():
    # Worker owns only the plain Queue captured before destroy; Tk is handled later by _tick.
    import inspect

    source = inspect.getsource(LiveWindow._overview_async)
    assert 'q.put(("overview",' in source
    assert ".after(" not in source and ".set(" not in source[source.index("def work"):]


def test_start_and_stop_success_request_immediate_overview_refresh():
    import inspect

    source = inspect.getsource(LiveWindow._drain_cloud)
    assert 'name in ("start", "stop")' in source
    assert "_overview_async(force=True)" in source
