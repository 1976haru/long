"""Gate 2: YouTube REST client against a local fake server (real HTTP, 실제 YouTube 접속 없음)."""
import pytest

from app.youtube_api import (
    DAILY_QUOTA, QUOTA_COSTS, STREAM_MARKER, TITLE_MAX, YouTubeApiClient, YouTubeApiError, estimate_daily_quota,
    redact_headers, validate_title,
)
from youtube_fakes import FAKE_ACCESS, FAKE_STREAM_NAME, FakeYouTube


@pytest.fixture
def fake():
    f = FakeYouTube()
    yield f
    f.close()


def api(fake, provider=None, sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    return YouTubeApiClient(provider or (lambda force_refresh=False: FAKE_ACCESS), base_url=fake.api_base,
                            sleep=sleeps.append)


def test_channel_lookup(fake):
    ch = api(fake).get_channel()
    assert ch.title == "Old Pop Lounge" and ch.id == "UCfake0001"
    assert fake.auth_headers[-1] == f"Bearer {FAKE_ACCESS}"


def test_reusable_stream_create_then_reuse(fake):
    a = api(fake)
    s = a.ensure_reusable_stream()
    assert s.is_reusable and s.description == STREAM_MARKER
    body = fake.calls[-1][3]
    assert body["cdn"] == {"ingestionType": "rtmp", "resolution": "1080p", "frameRate": "30fps"}
    assert body["contentDetails"] == {"isReusable": True}
    assert s.stream_name == FAKE_STREAM_NAME and FAKE_STREAM_NAME not in repr(s)  # Stream Key 노출 최소화
    assert s.rtmps_url.startswith("rtmps://")
    n = len(fake.streams)
    assert a.ensure_reusable_stream(saved_id=s.id).id == s.id  # 저장된 id 재사용
    assert a.ensure_reusable_stream().id == s.id  # 표식으로 재사용
    assert len(fake.streams) == n  # 매번 새로 만들지 않음
    assert a.ensure_reusable_stream(saved_id="stream-deleted").id == s.id


def test_broadcast_insert_body(fake):
    b = api(fake).insert_broadcast(title="🍂 가을에 듣기 좋은 샹송 | 24H LIVE", description="설명",
                                   privacy="unlisted", made_for_kids=False, scheduled_start=1_800_000_000)
    body = fake.broadcasts[b.id]["body"]
    assert body["snippet"]["title"] == "🍂 가을에 듣기 좋은 샹송 | 24H LIVE"
    assert body["snippet"]["scheduledStartTime"] == "2027-01-15T08:00:00.000Z"
    assert body["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False}
    cd = body["contentDetails"]
    assert cd["enableAutoStart"] is False and cd["enableAutoStop"] is False
    assert cd["recordFromStart"] is True and cd["enableDvr"] is True
    assert cd["monitorStream"] == {"enableMonitorStream": False}
    assert b.life_cycle_status == "created"


@pytest.mark.parametrize("privacy", ["public", "unlisted", "private"])
def test_privacy_values(fake, privacy):
    b = api(fake).insert_broadcast(title="t", privacy=privacy, made_for_kids=True)
    assert fake.broadcasts[b.id]["body"]["status"] == {"privacyStatus": privacy, "selfDeclaredMadeForKids": True}


def test_title_and_privacy_validated_before_call(fake):
    a = api(fake)
    with pytest.raises(YouTubeApiError, match="100자"):
        a.insert_broadcast(title="가" * (TITLE_MAX + 1))
    with pytest.raises(YouTubeApiError):
        a.insert_broadcast(title="  ")
    with pytest.raises(YouTubeApiError):
        a.insert_broadcast(title="ok", privacy="friends")
    assert fake.calls == []  # API 호출 전에 막음
    assert validate_title("가" * TITLE_MAX) == "가" * TITLE_MAX


def test_bind_get_transition_lifecycle(fake):
    a = api(fake)
    s = a.ensure_reusable_stream()
    b = a.insert_broadcast(title="t")
    assert a.bind_broadcast(b.id, s.id).bound_stream_id == s.id
    assert a.get_broadcast(b.id).life_cycle_status == "ready"
    fake.stream_active = False
    with pytest.raises(YouTubeApiError) as e:
        a.transition_broadcast(b.id, "live")
    assert e.value.kind == "transition" and e.value.reason == "errorStreamInactive" and not e.value.retryable
    fake.stream_active = True
    a.transition_broadcast(b.id, "live")
    assert a.get_broadcast(b.id).life_cycle_status == "live"  # liveStarting → live
    a.complete_broadcast(b.id)
    assert fake.status_of(b.id) == "complete"


@pytest.mark.parametrize("status,reason", [(429, "rateLimitExceeded"), (500, "backendError"), (503, "backendError"),
                                           (403, "rateLimitExceeded"), (403, "userRateLimitExceeded")])
def test_transient_errors_retry_with_backoff(fake, status, reason):
    sleeps = []
    fake.fail.extend([("channels", status, reason)] * 2)
    assert api(fake, sleeps=sleeps).get_channel().title == "Old Pop Lounge"
    assert sleeps == [1.0, 2.0]  # exponential backoff
    assert [op for _, op in fake.ops()].count("channels") == 3


def test_transient_errors_give_up_eventually(fake):
    sleeps = []
    fake.fail.extend([("channels", 503, "backendError")] * 10)
    with pytest.raises(YouTubeApiError) as e:
        api(fake, sleeps=sleeps).get_channel()
    assert e.value.retryable and sleeps == [1.0, 2.0, 4.0, 8.0]


@pytest.mark.parametrize("status,reason,needle", [
    (403, "insufficientPermissions", "권한"),
    (403, "liveStreamingNotEnabled", "LIVE 스트리밍이 활성화"),
    (403, "quotaExceeded", "사용량 한도"),
    (403, "forbidden", "거부"),
])
def test_config_errors_not_retried(fake, status, reason, needle):
    sleeps = []
    fake.fail.append(("channels", status, reason))
    with pytest.raises(YouTubeApiError) as e:
        api(fake, sleeps=sleeps).get_channel()
    assert e.value.kind == "config" and needle in str(e.value) and sleeps == []
    assert len(fake.calls) == 1  # 무한 재시도 없음


def test_401_refreshes_token_once(fake):
    calls = []

    def provider(force_refresh=False):
        calls.append(force_refresh)
        return FAKE_ACCESS if force_refresh else "expired-token"
    assert api(fake, provider).get_channel().title == "Old Pop Lounge"
    assert calls == [False, True]


def test_network_error_is_transient(fake):
    from app.youtube_oauth import TransportError
    n = [0]

    def flaky(method, url, headers, body, timeout):
        n[0] += 1
        if n[0] == 1:
            raise TransportError("URLError")
        from app.youtube_oauth import urllib_transport
        return urllib_transport(method, url, headers, body, timeout)
    sleeps = []
    a = YouTubeApiClient(lambda force_refresh=False: FAKE_ACCESS, base_url=fake.api_base, sleep=sleeps.append, transport=flaky)
    assert a.get_channel().id == "UCfake0001" and sleeps == [1.0]


def test_quota_estimator():
    assert QUOTA_COSTS["liveBroadcasts.insert"] == 50 and QUOTA_COSTS["liveBroadcasts.bind"] == 50
    assert QUOTA_COSTS["liveBroadcasts.transition"] == 50 and QUOTA_COSTS["liveStreams.insert"] == 50
    assert QUOTA_COSTS["liveBroadcasts.list"] == 1 and QUOTA_COSTS["liveStreams.list"] == 1
    daily = estimate_daily_quota()
    assert daily < DAILY_QUOTA * 0.15, daily  # 기본 운영(하루 약 2회 교체, 5분 health) ≈ 1,000 units
    # 10~15초 상시 polling을 broadcast+stream 둘 다 하면 기본 한도를 넘는다 → 상시 polling은 5분으로 둔 이유
    assert estimate_daily_quota(health_poll_seconds=15) > DAILY_QUOTA


def test_redact_headers():
    h = redact_headers({"Authorization": f"Bearer {FAKE_ACCESS}", "Accept": "application/json"})
    assert FAKE_ACCESS not in str(h) and h["Accept"] == "application/json"
