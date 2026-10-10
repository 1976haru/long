"""채널별 YouTube LIVE 방송 정보 — 저장/격리/검증/카테고리/재생목록/적용/부분 실패/재시도 (가짜 YouTube만, 실제 접속 없음)."""
import json

import pytest

from app import settings as app_settings
from app.youtube_api import YouTubeApiClient
from app.youtube_metadata import MetadataError
from app.youtube_metadata_control import (
    CATEGORY_CACHE_KEY, NO_PLAYLIST, SETTINGS_KEY, STEP_CATEGORY, STEP_PLAYLIST, STEP_PRIVACY, STEP_TAGS, STEP_THUMBNAIL,
    STEP_TITLE, LiveMetadata, apply_after_create, apply_metadata_steps, broadcast_label, cached_categories,
    category_id_for, category_label, change_lines, fetch_categories, has_saved_metadata, list_target_broadcasts,
    load_metadata, normalize_tags, playlist_choices, privacy_label, privacy_value, requested_steps, save_metadata,
    title_count_text,
)
from tests.youtube_fakes import FAKE_ACCESS, FAKE_REFRESH, FAKE_STREAM_NAME, FakeYouTube

WRITE_OPS = {("PUT", "videos"), ("POST", "liveBroadcasts"), ("POST", "playlistItems"), ("POST", "upload/thumbnails/set"),
             ("PUT", "liveBroadcasts"), ("DELETE", "liveBroadcasts"), ("POST", "playlists")}


def png(path, w=1280, h=720):
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big")
                     + b"\x08\x02\x00\x00\x00" + b"\x00" * 16)
    return str(path)


@pytest.fixture
def fake():
    f = FakeYouTube()
    yield f
    f.close()


def api_for(fake, token=FAKE_ACCESS):
    return YouTubeApiClient(lambda force_refresh=False: token, base_url=fake.api_base, sleep=lambda s: None)


def writes(fake):
    return [c for c in fake.ops() if c in WRITE_OPS]


def md_tokyo(**kw):
    d = dict(title="Tokyo Chill LIVE", description="첫 줄\n둘째 줄\n\n넷째 줄", tags=["Tokyo Chill", "R&B"],
             category_id="10", privacy="public")
    d.update(kw)
    return LiveMetadata(**d)


# ---------------- A/B/H 저장 · 복원 · 채널 격리 ----------------

def test_per_channel_save_restore_and_isolation():
    assert not has_saved_metadata("default") and load_metadata("default") == LiveMetadata()  # 저장 전: 기본값
    save_metadata("default", LiveMetadata(title="시니어 추억의 가요", tags=["트로트"], category_id="24", privacy="private",
                                          youtube_playlist_id="PLsenior", youtube_playlist_title="시니어 LIVE"))
    save_metadata("tokyo", md_tokyo(youtube_playlist_id="PLtokyo", youtube_playlist_title="도쿄칠 LIVE"))
    save_metadata("oldpop", LiveMetadata(title="Old Pop Lounge 24H"))
    raw = json.loads(app_settings.settings_file().read_text(encoding="utf-8"))[SETTINGS_KEY]  # 재실행 = 파일에서 다시 읽기
    assert set(raw) == {"default", "tokyo", "oldpop"}
    s, t, o = load_metadata("default"), load_metadata("tokyo"), load_metadata("oldpop")
    assert (s.title, s.tags, s.category_id, s.privacy, s.youtube_playlist_id) == (
        "시니어 추억의 가요", ["트로트"], "24", "private", "PLsenior")
    assert (t.title, t.description, t.youtube_playlist_id, t.youtube_playlist_title) == (
        "Tokyo Chill LIVE", "첫 줄\n둘째 줄\n\n넷째 줄", "PLtokyo", "도쿄칠 LIVE")  # 줄바꿈 보존
    assert o.title == "Old Pop Lounge 24H" and o.youtube_playlist_id == "" and o.privacy == "unlisted"
    t.title = "바뀐 도쿄칠"
    save_metadata("tokyo", t)  # 한 채널만 바꿔도 다른 채널 값은 그대로
    assert load_metadata("default").title == "시니어 추억의 가요" and load_metadata("oldpop").title == "Old Pop Lounge 24H"
    assert load_metadata("tokyo").title == "바뀐 도쿄칠"


def test_broken_saved_values_do_not_crash_and_other_settings_untouched():
    app_settings.update_settings(cloud={"host": "x"}, live_channel_metadata={"default": {"title": 5, "tags": "x",
                                                                                         "category_id": "abc",
                                                                                         "privacy": "everyone"}})
    md = load_metadata("default")
    assert md == LiveMetadata()  # 깨진 칸은 기본값
    save_metadata("default", LiveMetadata(title="ok"))
    assert app_settings.load_settings()["cloud"] == {"host": "x"}  # 다른 설정은 건드리지 않음


# ---------------- C/D/E 제목 · 설명 · 태그 · 공개 상태 ----------------

def test_title_validation_and_counts():
    for bad in ("", "   ", "x" * 101, "a <b> c"):
        with pytest.raises(MetadataError):
            save_metadata("default", LiveMetadata(title=bad))
    assert not has_saved_metadata("default")  # 막힌 값은 저장되지 않음
    assert title_count_text("  abc ") == "3 / 100자"
    ok = save_metadata("default", LiveMetadata(title="  x" * 1 + "y" * 98))
    assert len(ok.title) == 99
    with pytest.raises(MetadataError):
        save_metadata("default", LiveMetadata(title="t", description="d" * 5001))


def test_tags_normalize_dedupe_keep_order():
    assert normalize_tags("Tokyo Chill, R&B, Playlist") == ["Tokyo Chill", "R&B", "Playlist"]
    assert normalize_tags("Tokyo Chill\nR&B\n\n , r&b ,Playlist,tokyo chill,  ") == ["Tokyo Chill", "R&B", "Playlist"]
    assert normalize_tags("") == []
    md = save_metadata("default", LiveMetadata(title="t", tags=["  a ", "", "A", "b"]))
    assert md.tags == ["a", "b"] and load_metadata("default").tags == ["a", "b"]
    with pytest.raises(MetadataError):
        normalize_tags(",".join(f"tag{i:03d}xxxxx" for i in range(60)))  # 전체 500자 초과


def test_privacy_mapping_same_as_scheduled_live():
    from app.youtube_metadata import PRIVACY_LABELS
    assert [privacy_label(v) for v in ("public", "unlisted", "private")] == ["공개", "일부공개", "비공개"]
    assert [privacy_value(PRIVACY_LABELS[v]) for v in ("public", "unlisted", "private")] == ["public", "unlisted", "private"]
    assert privacy_value("public") == "public" and privacy_value("??") == "unlisted"
    with pytest.raises(MetadataError):
        LiveMetadata(title="t", privacy="everyone").validate()


# ---------------- F 카테고리 ----------------

def test_categories_fetch_cache_and_fallback(fake):
    assert dict(cached_categories())["10"] == "음악"  # 받기 전: 기본 목록
    fake.categories = [("10", "음악"), ("20", "게임"), ("24", "엔터테인먼트")]
    got, err = fetch_categories(api_for(fake))
    assert not err and ("20", "게임") in got
    assert ("20", "게임") in cached_categories()  # 저장됨 → 다음 실행에서 네트워크 없이
    assert app_settings.load_settings()[CATEGORY_CACHE_KEY]["KR"]["items"]
    fake.fail.append(("videoCategories", 403, "forbidden"))
    got2, err2 = fetch_categories(api_for(fake))
    assert err2 and got2 == got  # 실패: 저장된 목록 그대로
    assert category_label("20", got) == "게임" and category_label("99", got) == "카테고리 99 (저장된 값)"
    assert category_id_for("게임", got, "10") == "20"
    assert category_id_for("모르는 이름", got, "99") == "99"  # 저장된 ID를 임의로 바꾸지 않음


# ---------------- G 재생목록 목록 ----------------

def test_playlists_fetch_mine_only(fake):
    mine = fake.add_playlist("도쿄칠 LIVE", fake.channel["id"])
    fake.add_playlist("Tokyo Chill Story", fake.channel["id"])
    fake.add_playlist("남의 재생목록", "UCother")
    pls = api_for(fake).list_playlists()
    ch = playlist_choices(pls)
    assert ch[0] == ("", NO_PLAYLIST) and [t for _, t in ch[1:]] == ["도쿄칠 LIVE", "Tokyo Chill Story"]
    assert (mine, "도쿄칠 LIVE") in ch
    kept = playlist_choices(pls, "PLgone", "예전 목록")
    assert kept[-1] == ("PLgone", "예전 목록 (저장됨)")  # 저장된 값은 몰래 지우지 않음


# ---------------- I/J/K/L/M 적용 ----------------

def make_broadcast(fake, api, title="Tokyo Chill Live #03"):
    return api.insert_broadcast(title=title, description="", privacy="unlisted").id


def test_apply_all_steps_to_selected_broadcast_only(fake, tmp_path):
    api = api_for(fake)
    target = make_broadcast(fake, api)
    other = make_broadcast(fake, api, "다른 방송")
    before_other = json.dumps(fake.broadcasts[other]["snippet"], sort_keys=True)
    pl = fake.add_playlist("도쿄칠 LIVE", fake.channel["id"])
    md = md_tokyo(thumbnail_path=png(tmp_path / "t.png"), youtube_playlist_id=pl, category_id="24")
    r = apply_metadata_steps(api, target, md, channel_id=fake.channel["id"])
    assert r.ok and set(r.steps) == {STEP_TITLE, STEP_PRIVACY, STEP_TAGS, STEP_CATEGORY, STEP_THUMBNAIL, STEP_PLAYLIST}
    b = fake.broadcasts[target]
    assert (b["snippet"]["title"], b["snippet"]["description"]) == ("Tokyo Chill LIVE", "첫 줄\n둘째 줄\n\n넷째 줄")
    assert b["snippet"]["tags"] == ["Tokyo Chill", "R&B"] and b["snippet"]["categoryId"] == "24"
    assert b["status"]["privacyStatus"] == "public" and b["status"]["lifeCycleStatus"] == "created"
    assert fake.thumbnails[target].startswith(b"\x89PNG") and fake.playlists[pl]["items"] == [target]
    assert json.dumps(fake.broadcasts[other]["snippet"], sort_keys=True) == before_other  # 다른 방송은 그대로
    assert all(c[3]["id"] == target for c in fake.calls if c[0] == "PUT" and c[1] == "videos")
    assert "✓ YouTube 재생목록" in r.summary_lines()


def test_duplicate_playlist_add_prevented(fake):
    api = api_for(fake)
    bid = make_broadcast(fake, api)
    pl = fake.add_playlist("도쿄칠 LIVE", fake.channel["id"])
    md = md_tokyo(youtube_playlist_id=pl)
    apply_metadata_steps(api, bid, md, steps=[STEP_PLAYLIST], channel_id=fake.channel["id"])
    inserts = fake.ops().count(("POST", "playlistItems"))
    r2 = apply_metadata_steps(api, bid, md, steps=[STEP_PLAYLIST], channel_id=fake.channel["id"])
    assert r2.steps[STEP_PLAYLIST] is True and r2.playlist_state == "already"
    assert fake.ops().count(("POST", "playlistItems")) == inserts == 1 and fake.playlists[pl]["items"] == [bid]
    assert "이미 들어 있음" in "\n".join(r2.summary_lines())


def test_playlist_of_other_account_is_blocked(fake):
    api = api_for(fake)
    bid = make_broadcast(fake, api)
    foreign = fake.add_playlist("남의 재생목록", "UCother")
    r = apply_metadata_steps(api, bid, md_tokyo(youtube_playlist_id=foreign), steps=[STEP_PLAYLIST],
                             channel_id=fake.channel["id"])
    assert r.steps[STEP_PLAYLIST] is False and fake.playlists[foreign]["items"] == []
    assert ("POST", "playlistItems") not in fake.ops()


# ---------------- N/O 부분 실패 · 실패 항목만 다시 ----------------

def test_partial_failure_keeps_successes_and_retry_only_failed(fake, tmp_path):
    api = api_for(fake)
    bid = make_broadcast(fake, api)
    pl = fake.add_playlist("도쿄칠 LIVE", fake.channel["id"])
    md = md_tokyo(thumbnail_path=png(tmp_path / "t.png"), youtube_playlist_id=pl)
    fake.fail.append(("upload/thumbnails", 500, "backendError"))
    fake.fail.extend([("upload/thumbnails", 500, "backendError")] * 4)  # 재시도까지 모두 실패
    r = apply_metadata_steps(api, bid, md, channel_id=fake.channel["id"])
    assert r.failed_steps() == [STEP_THUMBNAIL]
    lines = r.summary_lines()
    assert lines[0] == "✓ 제목/설명" and any(x.startswith("✗ 썸네일") for x in lines) and "✓ YouTube 재생목록" in lines
    assert bid in fake.broadcasts and fake.playlists[pl]["items"] == [bid]  # 방송/성공 항목은 그대로
    fake.fail.clear()
    n_snippet = fake.ops().count(("PUT", "videos"))
    n_pl = fake.ops().count(("POST", "playlistItems"))
    apply_metadata_steps(api, bid, md, steps=r.failed_steps(), result=r, channel_id=fake.channel["id"])
    assert r.ok and r.steps[STEP_THUMBNAIL] is True and bid in fake.thumbnails
    assert fake.ops().count(("PUT", "videos")) == n_snippet and fake.ops().count(("POST", "playlistItems")) == n_pl


def test_missing_thumbnail_is_only_thumbnail_failure(fake, tmp_path):
    api = api_for(fake)
    bid = make_broadcast(fake, api)
    p = png(tmp_path / "gone.png")
    md = md_tokyo(thumbnail_path=p)
    (tmp_path / "gone.png").unlink()
    r = apply_metadata_steps(api, bid, md)
    assert r.failed_steps() == [STEP_THUMBNAIL] and "찾을 수 없습니다" in r.errors[STEP_THUMBNAIL]
    assert fake.broadcasts[bid]["snippet"]["title"] == "Tokyo Chill LIVE"
    bad = tmp_path / "x.gif"
    bad.write_bytes(b"GIF89a")
    with pytest.raises(MetadataError):
        save_metadata("tokyo", md_tokyo(thumbnail_path=str(bad)))  # 저장할 때는 분명한 경고


def test_snippet_failure_reports_each_item(fake):
    api = api_for(fake)
    bid = make_broadcast(fake, api)
    fake.fail.append(("videos", 403, "forbidden"))  # 첫 videos 호출 (snippet 읽기) 실패
    r = apply_metadata_steps(api, bid, md_tokyo())
    assert set(r.failed_steps()) == {STEP_TITLE, STEP_TAGS, STEP_CATEGORY} and r.steps[STEP_PRIVACY] is True


# ---------------- R API 자동 세션 ----------------

def test_api_auto_session_applies_metadata_after_create(fake, tmp_path):
    from app.youtube_session import BroadcastTemplate, YouTubeRolloverManager
    from tests.youtube_fakes import FakeClock
    api = api_for(fake)
    stream = api.ensure_reusable_stream(None)
    pl = fake.add_playlist("도쿄칠 LIVE", fake.channel["id"])
    md = md_tokyo(thumbnail_path=png(tmp_path / "t.png"), youtube_playlist_id=pl).validate()
    clock = FakeClock()
    m = YouTubeRolloverManager(api, stream_id=stream.id, template=BroadcastTemplate(md.title, md.description, md.privacy),
                               clock=clock, sleep=clock.sleep, session_seconds=3600,
                               after_create=lambda a, bid: apply_after_create(a, bid, md, channel_id=fake.channel["id"]))
    bid = m.go_live_first()
    b = fake.broadcasts[bid]
    ins = next(c[3] for c in fake.calls if c[:2] == ("POST", "liveBroadcasts"))
    assert (ins["snippet"]["title"], ins["snippet"]["description"], ins["status"]["privacyStatus"]) == (
        "Tokyo Chill LIVE", "첫 줄\n둘째 줄\n\n넷째 줄", "public")  # 생성 때 제목/설명/공개 상태
    assert b["snippet"]["tags"] == ["Tokyo Chill", "R&B"] and b["snippet"]["title"] == "Tokyo Chill LIVE"
    assert fake.thumbnails[bid] and fake.playlists[pl]["items"] == [bid] and fake.status_of(bid) == "live"
    r = m.metadata_result
    assert r.ok and r.steps[STEP_TITLE] is True and r.steps[STEP_PLAYLIST] is True
    ops = fake.ops()
    assert ops.index(("POST", "liveBroadcasts/bind")) < ops.index(("PUT", "videos"))  # 생성 → bind → 메타데이터


def test_api_auto_session_optional_failure_does_not_block_or_delete(fake):
    from app.youtube_session import BroadcastTemplate, YouTubeRolloverManager
    from tests.youtube_fakes import FakeClock
    api = api_for(fake)
    stream = api.ensure_reusable_stream(None)
    md = md_tokyo(youtube_playlist_id="PLmissing").validate()
    clock = FakeClock()
    m = YouTubeRolloverManager(api, stream_id=stream.id, template=BroadcastTemplate(md.title, md.description, md.privacy),
                               clock=clock, sleep=clock.sleep, session_seconds=3600,
                               after_create=lambda a, bid: apply_after_create(a, bid, md, channel_id=fake.channel["id"]))
    bid = m.go_live_first()
    assert fake.status_of(bid) == "live" and ("DELETE", "liveBroadcasts") not in fake.ops()
    assert m.metadata_result.failed_steps() == [STEP_PLAYLIST]

    def boom(a, b):
        raise RuntimeError("x")
    m2 = YouTubeRolloverManager(api, stream_id=stream.id, template=BroadcastTemplate("t2"), clock=clock,
                                sleep=clock.sleep, session_seconds=3600, after_create=boom)
    assert m2.go_live_first() and m2.metadata_warnings  # hook 예외도 시작을 막지 않음


def test_api_auto_session_core_failure_blocks(fake):
    from app.youtube_api import YouTubeApiError
    from app.youtube_session import BroadcastTemplate, YouTubeRolloverManager
    api = api_for(fake)
    stream = api.ensure_reusable_stream(None)
    called = []
    m = YouTubeRolloverManager(api, stream_id=stream.id, template=BroadcastTemplate("t"), session_seconds=3600,
                               after_create=lambda a, b: called.append(b))
    fake.fail.append(("liveBroadcasts/bind", 404, "liveStreamNotFound"))
    with pytest.raises(YouTubeApiError):
        m.go_live_first()
    assert not called  # 생성/bind 실패: 시작 차단, 방송 정보도 적용하지 않음


# ---------------- Q 적용 대상 목록 · S 계정 격리 ----------------

def test_target_broadcasts_listed_not_guessed(fake):
    api = api_for(fake)
    up = make_broadcast(fake, api, "예정 방송")
    live = make_broadcast(fake, api, "진행 중 방송")
    fake.broadcasts[live]["status"]["lifeCycleStatus"] = "live"
    done = make_broadcast(fake, api, "끝난 방송")
    fake.broadcasts[done]["status"]["lifeCycleStatus"] = "complete"
    n = len(writes(fake))
    got = list_target_broadcasts(api)
    assert [b.id for b in got] == [live, up] and len(writes(fake)) == n  # 목록만: 쓰기 0
    assert "LIVE 중" in broadcast_label(got[0]) and "예정" in broadcast_label(got[1])


def test_account_isolation_lists_only_own_broadcasts(fake):
    tokyo = {"id": "UCtokyo000001", "title": "Tokyo Chill"}
    fake.token_channels["tok-tokyo"] = tokyo
    fake.valid_tokens.add("tok-tokyo")
    senior_api, tokyo_api = api_for(fake), api_for(fake, "tok-tokyo")
    s_b = make_broadcast(fake, senior_api, "시니어 방송")
    t_b = make_broadcast(fake, tokyo_api, "도쿄칠 방송")
    assert [b.id for b in list_target_broadcasts(tokyo_api)] == [t_b]
    assert [b.id for b in list_target_broadcasts(senior_api)] == [s_b]
    from app.youtube_accounts import ChannelMismatchError, verify_channel
    with pytest.raises(ChannelMismatchError):
        verify_channel(tokyo_api, fake.channel["id"])  # 시니어 채널 ID를 기대 → 도쿄칠 token이면 차단
    assert verify_channel(tokyo_api, tokyo["id"]).title == "Tokyo Chill"


# ---------------- T 비밀 노출 없음 ----------------

def test_no_secrets_in_saved_metadata_results_or_texts(fake, tmp_path):
    api = api_for(fake)
    bid = make_broadcast(fake, api)
    md = md_tokyo(thumbnail_path=str(tmp_path / "none.png"), youtube_playlist_id="PLx")
    fake.fail.append(("videos", 401, "authError"))
    r = apply_metadata_steps(api, bid, md, channel_id=fake.channel["id"])
    save_metadata("tokyo", md_tokyo())
    blob = "\n".join(r.summary_lines() + change_lines(md) + [repr(r), repr(md),
                                                             app_settings.settings_file().read_text(encoding="utf-8")])
    for secret in (FAKE_ACCESS, FAKE_REFRESH, FAKE_STREAM_NAME, "Bearer"):
        assert secret not in blob
    with pytest.raises(ValueError):
        import app.youtube_metadata_control as c
        orig = LiveMetadata.to_dict
        try:
            LiveMetadata.to_dict = lambda self: {**orig(self), "stream_key": "x"}
            c.save_metadata("tokyo", md_tokyo())
        finally:
            LiveMetadata.to_dict = orig


def test_requested_steps_and_change_lines():
    md = md_tokyo()
    assert requested_steps(md) == [STEP_TITLE, STEP_PRIVACY, STEP_TAGS, STEP_CATEGORY]
    md2 = md_tokyo(thumbnail_path="a.png", youtube_playlist_id="PL1", youtube_playlist_title="도쿄칠 LIVE")
    assert requested_steps(md2)[-2:] == [STEP_THUMBNAIL, STEP_PLAYLIST]
    lines = change_lines(md2)
    assert lines[0] == "제목: Tokyo Chill LIVE" and "YouTube 재생목록: 도쿄칠 LIVE" in lines and lines[-1] == "공개 상태: 공개"
