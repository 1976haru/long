"""Phase 3B.1 (예약 LIVE) WIP 모듈: 메타데이터 검증/템플릿/썸네일, 시간대/반복 규칙, 예약 생성(부분 성공), rollover 메타데이터."""
from datetime import date, datetime, time as dtime, timezone

import pytest

from app.youtube_api import YouTubeApiClient, YouTubeApiError
from app.youtube_metadata import (
    BroadcastMetadata, MetadataError, MetadataTemplate, THUMB_ROTATE, check_template, parse_tags, pick_thumbnail,
    render_template, tags_length, validate_thumbnail, validate_title,
)
from app.youtube_schedule import (
    CUSTOM_WEEKDAYS, DAILY, ONCE, WEEKDAYS, ReservationRecord, ReservationStore, ScheduleError, ScheduleRule,
    create_reservation, local_to_utc, occurrences, plan_top_up, top_up,
)
from app.settings import load_settings, save_settings
from app.youtube_session import BroadcastTemplate, YouTubeRolloverManager
from youtube_fakes import FAKE_ACCESS, FakeClock, FakeYouTube

def png(path, w=1280, h=720):
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x02" + b"\x00" * 20)
    return path


def jpeg(path, w=1280, h=720):
    path.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
                     + b"\xff\xc0\x00\x11\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x03" + b"\x00" * 9 + b"\xff\xd9")
    return path


@pytest.fixture
def fake():
    f = FakeYouTube()
    yield f
    f.close()


@pytest.fixture
def api(fake):
    return YouTubeApiClient(lambda force_refresh=False: FAKE_ACCESS, base_url=fake.api_base, sleep=lambda s: None)


# ---------------- 메타데이터 ----------------

def test_title_rules():
    assert validate_title("  샹송 LIVE ") == "샹송 LIVE"
    for bad in ("", "a" * 101, "<b>"):
        with pytest.raises(MetadataError):
            validate_title(bad)
    with pytest.raises(MetadataError, match="영상 제목"):
        validate_title("", "영상 제목")


def test_tags_parse_dedupe_and_limit():
    assert parse_tags("샹송, #chanson, 샹송 ,, Jazz,jazz") == ["샹송", "chanson", "Jazz"]
    assert tags_length(["a b", "c"]) == 3 + 2 + 1 + 1
    with pytest.raises(MetadataError):
        parse_tags(",".join(f"tag{i:03d}" for i in range(80)))
    with pytest.raises(MetadataError):
        parse_tags("<x>")


def test_template_render_and_unknown_variable():
    from zoneinfo import ZoneInfo
    t = datetime(2026, 10, 6, 7, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert render_template("{date} ({weekday}) LIVE #{session} {channel}", local_start=t, session=3, channel="CH") \
        == "2026.10.06 (화) LIVE #03 CH"
    with pytest.raises(MetadataError, match="알 수 없는 변수"):
        check_template("{nope}")
    with pytest.raises(MetadataError):
        render_template("{date}", local_start=datetime(2026, 1, 1), session=1)


def test_thumbnail_validation(tmp_path):
    info = validate_thumbnail(png(tmp_path / "a.png"))
    assert (info.mime, info.width, info.height) == ("image/png", 1280, 720) and "권장" in info.note
    j = validate_thumbnail(jpeg(tmp_path / "b.jpg", 1000, 1000))
    assert (j.mime, j.width, j.height) == ("image/jpeg", 1000, 1000) and "16:9" in j.note
    (tmp_path / "c.png").write_bytes(b"not an image")
    with pytest.raises(MetadataError):
        validate_thumbnail(tmp_path / "c.png")
    jpeg(tmp_path / "d.png")
    with pytest.raises(MetadataError, match="확장자"):
        validate_thumbnail(tmp_path / "d.png")
    (tmp_path / "e.gif").write_bytes(b"GIF89a")
    with pytest.raises(MetadataError):
        validate_thumbnail(tmp_path / "e.gif")
    with pytest.raises(MetadataError):
        validate_thumbnail(tmp_path / "missing.png")


def test_thumbnail_rotation():
    paths = ["1.png", "2.png", "3.png", "4.png"]
    assert [pick_thumbnail(THUMB_ROTATE, paths, "", i) for i in range(4)] == ["1.png", "2.png", "3.png", "1.png"]


def test_template_round_trip_has_no_secrets():
    t = MetadataTemplate(name="기본", title_template="LIVE #{session}", tags=["a"]).validate()
    d = t.to_dict()
    assert MetadataTemplate.from_dict(d) == t
    assert not {"token", "stream_key", "refresh_token"} & set(d)


# ---------------- 시간대 / 반복 ----------------

def test_local_to_utc_kst_and_jst():
    assert local_to_utc(date(2026, 10, 6), dtime(7, 0), "Asia/Seoul") == datetime(2026, 10, 5, 22, 0, tzinfo=timezone.utc)
    assert local_to_utc(date(2026, 10, 6), dtime(7, 0), "Asia/Tokyo") == datetime(2026, 10, 5, 22, 0, tzinfo=timezone.utc)
    with pytest.raises(ScheduleError):
        local_to_utc(date(2026, 10, 6), dtime(7, 0), "Mars/Base")


def test_dst_gap_moves_forward():
    # 2026-03-08 02:30은 뉴욕에 없는 시각 → 실제 존재하는 시각(UTC 07:30 = EDT 03:30)
    u = local_to_utc(date(2026, 3, 8), dtime(2, 30), "America/New_York")
    assert u == datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)


def test_rules_and_rolling_window():
    now = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)  # 월 09:00 KST
    daily = ScheduleRule(mode=DAILY, start_date="2026-10-05", start_time_local="07:00")
    occ = occurrences(daily, now)
    assert len(occ) == 7 and occ[0].local_start.day == 6  # 오늘 07:00은 이미 지남
    wk = occurrences(ScheduleRule(mode=WEEKDAYS, start_date="2026-10-05", start_time_local="10:00"), now)
    assert all(o.local_start.weekday() < 5 for o in wk)
    cu = occurrences(ScheduleRule(mode=CUSTOM_WEEKDAYS, start_date="2026-10-05", start_time_local="10:00",
                                  custom_weekdays=["SAT"]), now)
    assert [o.local_start.weekday() for o in cu] == [5]
    once = occurrences(ScheduleRule(mode=ONCE, start_date="2026-12-25", start_time_local="10:00"), now)
    assert len(once) == 1  # 7일보다 멀어도 한 번 예약은 허용
    assert len(plan_top_up(daily, {occ[0].key, occ[1].key}, now)) == 5
    with pytest.raises(ScheduleError):
        ScheduleRule(mode=CUSTOM_WEEKDAYS, start_date="2026-10-05").validate()
    with pytest.raises(ScheduleError):
        ScheduleRule(start_date="2026/10/05").validate()


# ---------------- 예약 생성 (fake YouTube) ----------------

def test_create_reservation_full(api, fake, tmp_path):
    stream = api.ensure_reusable_stream()
    occ = occurrences(ScheduleRule(mode=ONCE, start_date="2030-01-02", start_time_local="07:00"),
                      datetime(2029, 12, 31, tzinfo=timezone.utc))[0]
    md = BroadcastMetadata(title="샹송 LIVE", description="설명", tags=["샹송", "jazz"], category_id="10",
                           thumbnail_path=str(png(tmp_path / "t.png")), default_language="fr")
    r = create_reservation(api, md, occ, stream_id=stream.id)
    assert r.complete and r.thumbnail_ok is True
    b = fake.broadcasts[r.broadcast_id]
    assert b["snippet"]["scheduledStartTime"] == "2030-01-01T22:00:00.000Z"
    assert b["snippet"]["tags"] == ["샹송", "jazz"] and b["snippet"]["defaultLanguage"] == "fr"
    assert b["snippet"]["title"] == "샹송 LIVE" and b["snippet"]["description"] == "설명"  # 기존 snippet 보존
    assert b["contentDetails"]["boundStreamId"] == stream.id
    assert fake.thumbnails[r.broadcast_id].startswith(b"\x89PNG")
    assert "✓ 방송 예약 생성" in r.summary_lines()


def test_create_reservation_partial_keeps_broadcast(api, fake, tmp_path):
    stream = api.ensure_reusable_stream()
    occ = occurrences(ScheduleRule(mode=ONCE, start_date="2030-01-02"), datetime(2029, 12, 31, tzinfo=timezone.utc))[0]
    fake.fail.append(("videos", 400, "invalidCategoryId"))
    md = BroadcastMetadata(title="T", thumbnail_path=str(png(tmp_path / "t.png")))
    r = create_reservation(api, md, occ, stream_id=stream.id)
    assert r.broadcast_ok and not r.metadata_ok and r.thumbnail_ok and not r.complete
    assert r.broadcast_id in fake.broadcasts  # 지우지 않음


def test_insert_failure_reports_error(api, fake):
    occ = occurrences(ScheduleRule(mode=ONCE, start_date="2030-01-02"), datetime(2029, 12, 31, tzinfo=timezone.utc))[0]
    fake.fail.append(("liveBroadcasts", 403, "userBroadcastsExceedLimit"))
    r = create_reservation(api, BroadcastMetadata(title="T"), occ)
    assert not r.broadcast_ok and "한도" in r.errors["broadcast"]


def test_top_up_and_store(api, fake):
    store = ReservationStore(load_settings, lambda **kw: save_settings({**load_settings(), **kw}))
    rule = ScheduleRule(mode=DAILY, start_date="2030-01-01", start_time_local="07:00")
    tmpl = MetadataTemplate(name="d", title_template="{date} LIVE #{session}")
    now = datetime(2029, 12, 31, 12, 0, tzinfo=timezone.utc)  # 21:00 KST → 첫 회차 2030-01-01 07:00 KST
    res = top_up(api, store, rule_id="r1", rule=rule, template=tmpl, now=now)
    assert len(res) == 7 and all(r.broadcast_ok for r in res)
    assert [r.session for r in store.all()] == list(range(1, 8))
    assert store.all()[0].title == "2030.01.01 LIVE #01"
    assert top_up(api, store, rule_id="r1", rule=rule, template=tmpl, now=now) == []  # 이미 7개
    store.remove(store.all()[0].broadcast_id)
    assert len(store.all()) == 6
    assert ReservationRecord("x", "t", "2030-01-01T00:00:00+00:00").youtube_url.endswith("v=x")


# ---------------- rollover + 메타데이터 템플릿 ----------------

def test_rollover_applies_metadata_template(fake, tmp_path):
    clock = FakeClock()
    api = YouTubeApiClient(lambda force_refresh=False: FAKE_ACCESS, base_url=fake.api_base, sleep=clock.sleep, clock=clock)
    stream = api.ensure_reusable_stream()
    tmpl = MetadataTemplate(name="m", title_template="샹송 LIVE #{session}", tags=["샹송"],
                            thumbnail_paths=[str(png(tmp_path / "1.png")), str(png(tmp_path / "2.png"))],
                            thumbnail_mode=THUMB_ROTATE)
    m = YouTubeRolloverManager(api, stream_id=stream.id, template=BroadcastTemplate("x"), clock=clock, sleep=clock.sleep,
                               metadata_template=tmpl)
    bid = m.go_live_first()
    assert fake.broadcasts[bid]["snippet"]["title"] == "샹송 LIVE #01"
    assert fake.broadcasts[bid]["snippet"]["tags"] == ["샹송"] and bid in fake.thumbnails
    assert m.metadata_warnings == []


def test_rollover_metadata_failure_does_not_stop_live(fake, tmp_path):
    clock = FakeClock()
    api = YouTubeApiClient(lambda force_refresh=False: FAKE_ACCESS, base_url=fake.api_base, sleep=clock.sleep, clock=clock)
    stream = api.ensure_reusable_stream()
    events = []
    m = YouTubeRolloverManager(api, stream_id=stream.id, template=BroadcastTemplate("x"), clock=clock, sleep=clock.sleep,
                               metadata_template=MetadataTemplate(name="m", title_template="T"),
                               on_event=lambda k, msg: events.append(k))
    fake.fail.append(("videos", 400, "invalidCategoryId"))
    bid = m.go_live_first()
    assert fake.status_of(bid) == "live" and m.metadata_warnings and "metadata_partial" in events


def test_go_live_existing_reservation(api, fake):
    stream = api.ensure_reusable_stream()
    b = api.insert_broadcast(title="예약 방송")
    m = YouTubeRolloverManager(api, stream_id=stream.id, template=BroadcastTemplate("x"), sleep=lambda s: None)
    assert m.go_live_existing(b.id, session_number=2) == b.id
    assert fake.status_of(b.id) == "live" and m.session_number == 2
    fake.stream_active = False
    b2 = api.insert_broadcast(title="예약 2")
    m2 = YouTubeRolloverManager(api, stream_id=stream.id, template=BroadcastTemplate("x"), sleep=lambda s: None,
                                transition_timeout=0)
    with pytest.raises(YouTubeApiError):
        m2.go_live_existing(b2.id)
    assert fake.status_of(b2.id) != "live"


def test_api_extras(api, fake):
    b = api.insert_broadcast(title="원래 제목", description="원래 설명")
    api.update_video_metadata(b.id, tags=["a"], category_id="24")
    sn = fake.broadcasts[b.id]["snippet"]
    assert sn["title"] == "원래 제목" and sn["description"] == "원래 설명" and sn["categoryId"] == "24"
    with pytest.raises(YouTubeApiError):
        api.set_thumbnail(b.id, b"x", "image/gif")
    assert api.calls.count("videos.update") == 1
