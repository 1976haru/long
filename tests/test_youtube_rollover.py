"""Gate 3~5: Broadcast lifecycle / 11:40 사전 준비 / 11:50 교체 / 실패 시 현재 방송 유지 (fake clock + fake YouTube)."""
import pytest

from app.live_session import ARCHIVE_SAFE_SECONDS, ManualSessionProvider
from app.youtube_api import QUOTA_COSTS, YouTubeApiClient, YouTubeApiError, estimate_daily_quota
from app.youtube_session import (
    ARCHIVE_RISK_WARNING, PREPARE_LEAD_SECONDS, TITLE_RULE_NUMBERED, BroadcastTemplate, RolloverState,
    YouTubeApiSessionProvider, YouTubeRolloverManager, session_seconds_for_run,
)
from youtube_fakes import FAKE_ACCESS, FakeClock, FakeYouTube

TITLE = "🍂 가을에 듣기 좋은 샹송 | 24H LIVE"


@pytest.fixture
def env():
    fake = FakeYouTube()
    clock = FakeClock()
    api = YouTubeApiClient(lambda force_refresh=False: FAKE_ACCESS, base_url=fake.api_base, sleep=clock.sleep, clock=clock)
    stream = api.ensure_reusable_stream()
    yield fake, clock, api, stream
    fake.close()


def manager(api, stream, clock, **kw):
    tmpl = kw.pop("template", BroadcastTemplate(TITLE, "설명", "unlisted"))
    return YouTubeRolloverManager(api, stream_id=stream.id, template=tmpl, clock=clock, sleep=clock.sleep,
                                  session_seconds=ARCHIVE_SAFE_SECONDS, **kw)


def advance_to(clock, m, seconds_into_session):
    clock.t = m.session_started_at + seconds_into_session


def test_go_live_first(env):
    fake, clock, api, stream = env
    m = manager(api, stream, clock)
    bid = m.go_live_first()
    assert fake.status_of(bid) == "live" and m.state is RolloverState.LIVE and m.session_number == 1
    assert fake.broadcasts[bid]["contentDetails"]["boundStreamId"] == stream.id
    assert fake.broadcasts[bid]["snippet"]["title"] == TITLE


def test_go_live_first_waits_for_stream_active(env):
    fake, clock, api, stream = env
    fake.stream_active = False
    m = manager(api, stream, clock, transition_timeout=30)
    with pytest.raises(YouTubeApiError) as e:
        m.go_live_first()
    assert e.value.reason == "errorStreamInactive"
    assert not any(op == "liveBroadcasts/transition" for _, op in fake.ops())  # active 전에는 live 전환 안 함


def test_prepare_at_1140_and_rollover_at_1150(env):
    fake, clock, api, stream = env
    m = manager(api, stream, clock)
    first = m.go_live_first()
    n_calls = len(fake.calls)
    advance_to(clock, m, ARCHIVE_SAFE_SECONDS - PREPARE_LEAD_SECONDS - 1)  # 11:39:59
    m.tick()
    later = fake.calls[n_calls:]
    assert not m.next_id and all(meth == "GET" for meth, _, _, _ in later)  # 5분 health(조회 1 unit)만, 생성/전환 없음
    advance_to(clock, m, ARCHIVE_SAFE_SECONDS - PREPARE_LEAD_SECONDS)  # 11:40
    assert m.tick() is RolloverState.NEXT_READY
    nxt = m.next_id
    assert fake.status_of(nxt) == "ready" and fake.status_of(first) == "live"  # 미리 준비, 현재 방송 그대로
    assert fake.broadcasts[nxt]["contentDetails"]["boundStreamId"] == stream.id  # 같은 reusable stream
    assert len(fake.streams) == 1  # stream은 새로 만들지 않음
    assert m.snapshot().next_ready and m.snapshot().label == "✓ 다음 세션 준비됨"
    start_calls = len(fake.calls)
    advance_to(clock, m, ARCHIVE_SAFE_SECONDS)  # 11:50
    assert m.tick() is RolloverState.LIVE
    assert fake.status_of(first) == "complete" and fake.status_of(nxt) == "live"
    assert m.current_id == nxt and m.session_number == 2 and not m.next_id
    seq = [(op, q.get("broadcastStatus"), q.get("id")) for _, op, q, _ in fake.calls[start_calls:] if op == "liveBroadcasts/transition"]
    assert seq == [("liveBroadcasts/transition", "complete", first), ("liveBroadcasts/transition", "live", nxt)]
    # complete 전에 stream active 확인
    ops = [op for _, op, _, _ in fake.calls[start_calls:]]
    assert ops.index("liveStreams") < ops.index("liveBroadcasts/transition")
    assert m.seconds_until_rollover() == pytest.approx(ARCHIVE_SAFE_SECONDS, abs=10)  # 새 세션 시계


def test_title_rules(env):
    fake, clock, api, stream = env
    m = manager(api, stream, clock, template=BroadcastTemplate(TITLE, "", "public", title_rule=TITLE_RULE_NUMBERED))
    m.go_live_first()
    advance_to(clock, m, ARCHIVE_SAFE_SECONDS)
    m.tick()
    m.tick()
    assert fake.broadcasts[m.current_id]["snippet"]["title"] == "🍂 가을에 듣기 좋은 샹송 | LIVE #02"
    long = BroadcastTemplate("가" * 100, title_rule=TITLE_RULE_NUMBERED)
    assert len(long.title_for(12)) == 100 and long.title_for(12).endswith(" | LIVE #12")
    assert BroadcastTemplate(TITLE).title_for(5) == TITLE  # 기본: 같은 제목 유지
    assert fake.broadcasts[m.current_id]["body"]["status"]["privacyStatus"] == "public"


def test_next_preparation_failure_keeps_current_live(env):
    fake, clock, api, stream = env
    m = manager(api, stream, clock)
    first = m.go_live_first()
    fake.fail.extend([("liveBroadcasts", 503, "backendError")] * 40)  # 준비 계속 실패
    for t in (ARCHIVE_SAFE_SECONDS - 600, ARCHIVE_SAFE_SECONDS - 300, ARCHIVE_SAFE_SECONDS, ARCHIVE_SAFE_SECONDS + 120):
        advance_to(clock, m, t)
        m.tick()
        assert fake.status_of(first) == "live"  # 절대 complete하지 않음
        assert not any(q.get("broadcastStatus") == "complete" for _, op, q, _ in fake.calls)
    assert m.state is RolloverState.ROLLOVER_FAILED and m.warning == ARCHIVE_RISK_WARNING
    assert "보관되지 않을 수" in m.snapshot().warning
    fake.fail.clear()  # 복구
    clock.t += 120
    m.tick()  # 다시 준비 → 이미 11:50 지났으므로 바로 교체
    m.tick()
    assert fake.status_of(first) == "complete" and m.session_number == 2 and m.state is RolloverState.LIVE


def test_prepare_retry_delays(env):
    fake, clock, api, stream = env
    m = manager(api, stream, clock)
    m.go_live_first()
    fake.fail.extend([("liveBroadcasts", 500, "backendError")] * 5 * 4)  # 4번의 준비 시도 x (1+4 retries)
    advance_to(clock, m, ARCHIVE_SAFE_SECONDS - 600)
    waits = []
    for _ in range(4):
        m.tick()
        waits.append(round(m.next_retry_at - clock.t))
        clock.t = m.next_retry_at
    assert waits == [5, 10, 30, 60]


def test_config_error_no_infinite_retry(env):
    fake, clock, api, stream = env
    m = manager(api, stream, clock)
    first = m.go_live_first()
    fake.fail.append(("liveBroadcasts", 403, "insufficientPermissions"))
    advance_to(clock, m, ARCHIVE_SAFE_SECONDS - 600)
    m.tick()
    assert m.blocked and "권한" in m.last_error
    n = len(fake.calls)
    for k in range(20):
        clock.t += 60
        m.tick()
    assert len(fake.calls) == n  # 사용자 조치 전 재시도 안 함
    assert fake.status_of(first) == "live"
    m.retry_now()  # 사용자가 권한을 고친 뒤
    m.tick()  # 이미 11:50이 지났으므로 준비 + 교체까지 한 번에
    assert m.state is RolloverState.LIVE and m.session_number == 2 and fake.status_of(first) == "complete"


def test_stream_inactive_at_1150_does_not_complete(env):
    fake, clock, api, stream = env
    m = manager(api, stream, clock, transition_timeout=10)
    first = m.go_live_first()
    advance_to(clock, m, ARCHIVE_SAFE_SECONDS - 600)
    m.tick()
    fake.stream_active = False  # 송출 신호 끊김
    advance_to(clock, m, ARCHIVE_SAFE_SECONDS)
    m.tick()
    assert fake.status_of(first) == "live" and m.state is RolloverState.ROLLOVER_FAILED
    fake.stream_active = True
    clock.t = m.next_retry_at
    m.tick()
    assert fake.status_of(first) == "complete" and m.state is RolloverState.LIVE


def test_next_live_failure_after_complete_retries_with_next_retained(env):
    fake, clock, api, stream = env
    m = manager(api, stream, clock)
    first = m.go_live_first()
    advance_to(clock, m, ARCHIVE_SAFE_SECONDS - 600)
    m.tick()
    nxt = m.next_id
    advance_to(clock, m, ARCHIVE_SAFE_SECONDS)
    fake.fail.append(("liveBroadcasts/transition", 503, "backendError"))  # complete는 재시도로 통과
    fake.fail.extend([])
    orig_route = fake.route

    def route(method, op, q, body):  # 다음 방송 live 전환만 한 번 실패
        if op == "liveBroadcasts/transition" and q.get("broadcastStatus") == "live" and not getattr(fake, "_failed", False):
            fake._failed = True
            return fake._err(403, "insufficientLivePermissions")
        return orig_route(method, op, q, body)
    fake.route = route
    m.tick()
    assert fake.status_of(first) == "complete"
    assert m.state is RolloverState.ROLLOVER_FAILED and m.next_id == nxt  # 다음 방송 정보 유지
    clock.t = m.next_retry_at
    m.tick()
    assert m.state is RolloverState.LIVE and fake.status_of(nxt) == "live" and m.current_id == nxt


def test_bind_failure_reuses_inserted_broadcast(env):
    fake, clock, api, stream = env
    m = manager(api, stream, clock)
    m.go_live_first()
    fake.fail.extend([("liveBroadcasts/bind", 503, "backendError")] * 5)
    advance_to(clock, m, ARCHIVE_SAFE_SECONDS - 600)
    m.tick()
    assert not m.next_id
    clock.t = m.next_retry_at
    m.tick()
    inserts = [1 for m_, op, _, _ in fake.calls if m_ == "POST" and op == "liveBroadcasts"]
    assert len(inserts) == 2  # 첫 방송 1 + 다음 방송 1 (재시도 때 새로 만들지 않음)
    assert m.next_id


def test_accelerated_24h_two_rollovers_quota(env):
    fake, clock, api, stream = env
    m = manager(api, stream, clock)
    m.go_live_first()
    api.calls.clear()
    start = clock.t
    while clock.t < start + 24 * 3600:
        clock.t += 10
        m.tick()
    assert m.session_number == 3 and m.state is RolloverState.LIVE
    used = sum(QUOTA_COSTS[c] for c in api.calls)
    assert used <= estimate_daily_quota() * 1.2, used
    assert sum(1 for s in fake.broadcasts.values() if s["status"]["lifeCycleStatus"] == "live") == 1


def test_dev_session_override_only_in_dev_mode(monkeypatch):
    monkeypatch.delenv("PLVM_DEV_MODE", raising=False)
    monkeypatch.setenv("PLVM_DEV_SESSION_SECONDS", "180")
    assert session_seconds_for_run() == ARCHIVE_SAFE_SECONDS == 42600
    monkeypatch.setenv("PLVM_DEV_MODE", "1")
    assert session_seconds_for_run() == 180
    monkeypatch.setenv("PLVM_DEV_SESSION_SECONDS", "5")
    assert session_seconds_for_run() == 60  # 최소 60초


def test_providers(env):
    fake, clock, api, stream = env
    assert "[다음 세션 시작]" in ManualSessionProvider().prepare_next_broadcast()  # Phase 3A 회귀
    m = manager(api, stream, clock)
    m.go_live_first()
    p = YouTubeApiSessionProvider(m, stream.rtmps_url)
    assert p.prepare_next_broadcast() == "✓ 다음 세션 준비됨" and m.next_id
    assert p.get_next_ingest("rtmps://manual") == stream.rtmps_url
    assert p.complete_current_broadcast() is None
