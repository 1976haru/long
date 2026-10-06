from __future__ import annotations

import time

import pytest

from app.jp_youtube_workflow import (DuplicateCommentError, ExternalJapaneseCommentService,
    JapaneseCommentHistory, TranslationCache, parse_youtube_video_id)
from app.youtube_accounts import ChannelMismatchError, ChannelProfile, ProfileStore, new_profile_id
from app.youtube_api import QUOTA_COSTS, YouTubeApiClient
from youtube_fakes import FakeYouTube

JP = {"id": "UCjp000000000000000000JP", "title": "CHILI LAB"}
KR = {"id": "UCkr000000000000000000KR", "title": "한국 시니어"}
VID = "AbCdEf12345"


@pytest.mark.parametrize("url", [
    f"https://www.youtube.com/watch?v={VID}", f"https://youtu.be/{VID}",
    f"https://youtube.com/shorts/{VID}", f"https://m.youtube.com/watch?v={VID}&feature=share"])
def test_parse_supported_youtube_urls(url):
    assert parse_youtube_video_id(url) == VID


@pytest.mark.parametrize("url", ["", "http://youtu.be/AbCdEf12345", "https://evil.test/watch?v=AbCdEf12345",
                                  "https://youtube.com/watch?v=short", "https://youtube.com/results?x=1"])
def test_parse_rejects_unsafe_urls(url):
    with pytest.raises(ValueError): parse_youtube_video_id(url)


@pytest.fixture
def workflow(tmp_path):
    fake = FakeYouTube(); ps = ProfileStore()
    jp = ps.add(ChannelProfile(new_profile_id(), "🇯🇵 CHILI LAB", channel_id=JP["id"], channel_title=JP["title"],
                               language="ja", timezone="Asia/Tokyo"))
    kr = ps.add(ChannelProfile(new_profile_id(), "🇰🇷 한국", channel_id=KR["id"], channel_title=KR["title"],
                               language="ko", timezone="Asia/Seoul"))
    fake.videos[VID] = {"id": VID, "snippet": {"title": "Night Mix", "description": "calm music",
        "channelTitle": "Tokyo Sounds", "channelId": "UCtarget00000000000001", "categoryId": "10"},
        "status": {"privacyStatus": "public"}, "channel": "UCtarget00000000000001"}
    accounts = {jp.profile_id: (JP, "tok-jp"), kr.profile_id: (KR, "tok-kr")}
    def factory(profile, store):
        channel, token = accounts[profile.profile_id]; fake.valid_tokens.add(token); fake.token_channels[token] = channel
        return YouTubeApiClient(lambda force_refresh=False: token, base_url=fake.api_base, sleep=lambda _: None)
    svc = ExternalJapaneseCommentService(ps, api_factory=factory,
        history=JapaneseCommentHistory(tmp_path / "history.json"), clock=lambda: 1000)
    yield fake, ps, jp, kr, accounts, svc
    fake.close()


def test_metadata_jp_default_manual_confirm_post_duplicate_delete(workflow):
    fake, ps, jp, kr, accounts, svc = workflow
    assert svc.japanese_profiles()[0].profile_id == jp.profile_id
    video = svc.video(jp.profile_id, f"https://youtu.be/{VID}")
    assert (video.title, video.channel_title) == ("Night Mix", "Tokyo Sounds")
    with pytest.raises(ValueError, match="최종 확인"):
        svc.post_confirmed(jp.profile_id, video, "夜に聴くと落ち着きます。")
    rec = svc.post_confirmed(jp.profile_id, video, "夜に聴くと落ち着きます。", confirmed=True)
    assert rec.status == "POSTED" and rec.comment_id and svc.history.daily_count(jp.profile_id, 1000) == 1
    with pytest.raises(DuplicateCommentError):
        svc.post_confirmed(jp.profile_id, video, "夜に聴くと落ち着きます。", confirmed=True)
    deleted = svc.delete_confirmed(jp.profile_id, rec.comment_id, confirmed=True)
    assert deleted.status == "DELETED" and svc.history.all()[0].status == "DELETED"
    assert "comments.delete" in QUOTA_COSTS


def test_wrong_profile_actual_channel_is_blocked(workflow):
    fake, ps, jp, kr, accounts, svc = workflow
    video = svc.video(jp.profile_id, f"https://www.youtube.com/watch?v={VID}")
    accounts[jp.profile_id] = (KR, "wrong-token")
    with pytest.raises(ChannelMismatchError):
        svc.post_confirmed(jp.profile_id, video, "落ち着く雰囲気ですね。", confirmed=True)
    assert not svc.history.all()


def test_failed_post_is_recorded(workflow):
    fake, ps, jp, kr, accounts, svc = workflow
    video = svc.video(jp.profile_id, f"https://youtu.be/{VID}")
    fake.comments_disabled.add(VID)
    with pytest.raises(Exception):
        svc.post_confirmed(jp.profile_id, video, "静かな雰囲気が好きです。", confirmed=True)
    assert svc.history.all()[0].status == "FAILED"


def test_translation_cache_invalidates_when_text_or_model_changes(tmp_path):
    cache = TranslationCache(tmp_path / "cache.json")
    cache.put("c1", "qwen3:8b", "いいね", "좋네요", "가벼운 칭찬")
    assert cache.get("c1", "qwen3:8b", "いいね")["translation_ko"] == "좋네요"
    assert cache.get("c1", "qwen3:8b", "違う") is None
    assert cache.get("c1", "qwen3:4b", "いいね") is None


def test_source_has_no_bulk_external_posting_api():
    import app.jp_youtube_workflow as module
    names = set(vars(module.ExternalJapaneseCommentService))
    assert not names.intersection({"post_many", "schedule", "search_and_post", "auto_post"})
