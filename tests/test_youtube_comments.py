"""Studio v1.2 댓글 자동화: 첫 댓글(예약 공개 후), 채널 전체 새 댓글 확인, 검토/안전형 자동답글, 중복/스팸 방지,
오류 분리, 사용량, 댓글 관리 UI, 예약 업로드 연동. fake 서버만 사용 — 실제 YouTube/OCI 접속 없음."""
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.youtube_accounts import ChannelProfile, ProfileStore, new_profile_id
from app.youtube_api import YouTubeApiClient, YouTubeApiError
from app.youtube_comments import (
    C_MANUAL, C_NEW, C_REPLIED, C_REVIEW, C_SELF, COMMENTS_DISABLED, DEFAULT_REPLIES, E_AUTH,
    E_INSUFFICIENT_PERMISSION, E_NETWORK, E_RATE_LIMITED, FAILED, POSTED, POSTING, READY, REAUTH_MESSAGE,
    REPLY_AUTO, WAITING_PRIVACY_CHANGE, WAITING_PUBLIC, CommentService, CommentStore, assess_comment, choose_reply,
    classify_error, render_first_comment, template_warnings,
)
from app.youtube_upload_queue import COMPLETE, UploadJob
from app.youtube_usage import today_usage
from test_youtube_upload_queue import JP, KR, Env
from youtube_fakes import FakeYouTube

KR_FIRST = "오늘도 함께해 주셔서 감사합니다.\n가장 마음에 드는 곡이 있다면 댓글로 남겨주세요."
JP_FIRST = "今日も聴きに来てくださってありがとうございます。\nお気に入りの曲があれば、ぜひコメントで教えてください。"


@pytest.fixture
def fake():
    f = FakeYouTube()
    yield f
    f.close()


@pytest.fixture
def world(fake):
    ps = ProfileStore()
    kr = ps.add(ChannelProfile(new_profile_id(), "🇰🇷 한국 시니어", channel_id=KR["id"], channel_title=KR["title"],
                               language="ko", timezone="Asia/Seoul"))
    jp = ps.add(ChannelProfile(new_profile_id(), "🇯🇵 CHILI LAB", channel_id=JP["id"], channel_title=JP["title"],
                               language="ja", timezone="Asia/Tokyo"))
    env = Env(fake, ps, kr, jp)
    fake.comment_clock = env.clock
    jobs: list = []
    svc = CommentService(ps, api_factory=env.api_factory, jobs=lambda: jobs, clock=env.clock, sleep=env.clock.sleep,
                         connected=lambda p: True)
    return env, ps, kr, jp, jobs, svc


def video(fake, profile, privacy="private", publish_at=None):
    vid = fake._id("vid")
    st = {"privacyStatus": privacy}
    if publish_at:
        st["publishAt"] = publish_at
    fake.videos[vid] = {"id": vid, "snippet": {"title": "v"}, "status": st, "channel": profile.channel_id, "bytes": b""}
    return vid


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def job(vid, profile, *, privacy="private", publish_at="", text=KR_FIRST, title="가을 샹송"):
    return UploadJob(job_id=os.urandom(6).hex(), profile_id=profile.profile_id, channel_id=profile.channel_id,
                     profile_alias=profile.alias, video_path="x.mp4", title=title, privacy=privacy,
                     publish_at_utc=publish_at, status=COMPLETE, video_id=vid, first_comment=text)


def threads_on(fake, vid):
    return [t for t in fake.threads.values() if t["video"] == vid]


def ops(fake, name):
    return sum(1 for m, op, _, _ in fake.calls if op == name)


# ================= API =================

def test_top_level_comment_insert_and_channel_list(world, fake):
    env, ps, kr, jp, jobs, svc = world
    vid = video(fake, kr, "public")
    api = env.api_factory(kr, ps)
    th = api.insert_top_level_comment(kr.channel_id, vid, KR_FIRST)
    assert th.video_id == vid and th.top.text == KR_FIRST and th.top.author_channel_id == KR["id"]
    got, token = api.list_channel_comment_threads(KR["id"])
    assert [t.id for t in got] == [th.id] and token == ""
    assert api.calls == ["commentThreads.insert", "commentThreads.list"]
    q = [c[2] for c in fake.calls if c[1] == "commentThreads" and c[0] == "GET"][0]
    assert q["allThreadsRelatedToChannelId"] == KR["id"] and q["order"] == "time" and q["moderationStatus"] == "published"
    assert q["part"] == "snippet,replies"


def test_render_first_comment_variables():
    from zoneinfo import ZoneInfo
    t = datetime(2026, 10, 10, 19, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    out = render_first_comment("{title}｜{channel} {date} {series} EP{episode} ({filename})", title="秋の夜", channel="CHILI LAB",
                               local_start=t, series="Tokyo night", episode="3", filename="女_003", language="ja")
    assert out == "秋の夜｜CHILI LAB 2026.10.10 Tokyo night EP3 (女_003)"


# ================= 첫 댓글 =================

def test_scheduled_private_waits_then_comments_after_public(world, fake):
    env, ps, kr, jp, jobs, svc = world
    pub = env.clock() + 86400
    vid = video(fake, kr, "private", iso(pub))
    jobs.append(job(vid, kr, publish_at=iso(pub)))
    assert svc.sync_tasks() == 1
    t = svc.store.tasks()[0]
    assert t.status == WAITING_PUBLIC and t.next_at == pub - 120
    before = len(fake.calls)
    env.clock.t = pub - 3600
    svc.process_tasks()
    assert len(fake.calls) == before  # 공개 2분 전까지는 YouTube에 묻지 않는다
    env.clock.t = pub - 60
    svc.process_tasks()
    assert svc.store.tasks()[0].status == WAITING_PUBLIC and threads_on(fake, vid) == []  # 아직 비공개 → 댓글 없음
    assert svc.store.tasks()[0].next_at == pub + 15  # 공개 시각 직후 다시 확인
    env.clock.t = pub + 20
    fake.set_privacy(vid, "public")
    svc.process_tasks()
    t = svc.store.tasks()[0]
    assert t.status == READY and threads_on(fake, vid) == []  # 공개 확인 후 바로 쓰지 않고 잠시 기다림
    env.clock.t += 61
    svc.process_tasks()
    t = svc.store.tasks()[0]
    assert t.status == POSTED and t.first_comment_id
    [th] = threads_on(fake, vid)
    assert th["top"]["snippet"]["textOriginal"] == KR_FIRST and th["top"]["snippet"]["authorChannelId"]["value"] == KR["id"]
    assert t.label == "첫 댓글 등록 완료"


@pytest.mark.parametrize("privacy", ["unlisted", "public"])
def test_immediate_unlisted_or_public_comment(world, fake, privacy):
    env, ps, kr, jp, jobs, svc = world
    vid = video(fake, jp, privacy)
    jobs.append(job(vid, jp, privacy=privacy, text=JP_FIRST))
    svc.sync_tasks()
    assert svc.store.tasks()[0].status == READY
    svc.process_tasks()
    assert threads_on(fake, vid) == []
    env.clock.t += 61
    svc.process_tasks()
    assert svc.store.tasks()[0].status == POSTED and len(threads_on(fake, vid)) == 1
    assert threads_on(fake, vid)[0]["top"]["snippet"]["authorChannelId"]["value"] == JP["id"]


def test_private_without_schedule_waits_without_retry_loop(world, fake):
    env, ps, kr, jp, jobs, svc = world
    vid = video(fake, kr, "private")
    jobs.append(job(vid, kr, privacy="private"))
    svc.sync_tasks()
    assert svc.store.tasks()[0].status == WAITING_PRIVACY_CHANGE
    before = len(fake.calls)
    for _ in range(5):
        env.clock.t += 3600
        svc.process_tasks()
    assert len(fake.calls) == before  # 자동 polling 없음
    svc.process_tasks(force=True)  # 앱 시작/지금 확인 때만 1회 확인
    t = svc.store.tasks()[0]
    assert t.status == WAITING_PRIVACY_CHANGE and t.attempts == 0 and threads_on(fake, vid) == []
    fake.set_privacy(vid, "unlisted")
    svc.process_tasks(force=True)
    env.clock.t += 61
    svc.process_tasks()
    assert svc.store.tasks()[0].status == POSTED


def test_comments_disabled_stops(world, fake):
    env, ps, kr, jp, jobs, svc = world
    vid = video(fake, kr, "public")
    fake.comments_disabled.add(vid)
    jobs.append(job(vid, kr, privacy="public"))
    svc.sync_tasks()
    env.clock.t += 61
    svc.process_tasks()
    assert svc.store.tasks()[0].status == COMMENTS_DISABLED
    before = len(fake.calls)
    env.clock.t += 9999
    svc.process_tasks(force=True)
    assert len(fake.calls) == before  # 끝난 작업은 다시 시도하지 않음


def test_duplicate_first_comment_prevention(world, fake):
    env, ps, kr, jp, jobs, svc = world
    vid = video(fake, kr, "public")
    j = job(vid, kr, privacy="public")
    jobs.append(j)
    assert svc.sync_tasks() == 1 and svc.sync_tasks() == 0  # 작업당 1개
    env.clock.t += 61
    svc.process_tasks()
    env.clock.t += 999
    svc.process_tasks(force=True)
    assert len(threads_on(fake, vid)) == 1
    # 등록 직후 꺼져서 POSTING으로 남은 작업 → 다시 켜면 이미 달린 댓글을 찾아 POSTED (다시 쓰지 않음)
    vid2 = video(fake, kr, "public")
    j2 = job(vid2, kr, privacy="public")
    jobs.append(j2)
    svc.sync_tasks()
    api = env.api_factory(kr, ps)
    api.insert_top_level_comment(kr.channel_id, vid2, j2.first_comment)
    t = svc.store.task(j2.job_id)
    t.status = POSTING
    svc.store.save_task(t)
    CommentService(ps, api_factory=env.api_factory, jobs=lambda: jobs, clock=env.clock,
                   connected=lambda p: True).process_tasks()
    t = svc.store.task(j2.job_id)
    assert t.status == POSTED and t.first_comment_id and len(threads_on(fake, vid2)) == 1


def test_restart_catch_up(world, fake):
    env, ps, kr, jp, jobs, svc = world
    pub = env.clock() + 3600
    vid = video(fake, jp, "private", iso(pub))
    jobs.append(job(vid, jp, publish_at=iso(pub), text=JP_FIRST))
    svc.sync_tasks()  # 앱 종료 (예약 시각에 꺼져 있었음)
    env.clock.t = pub + 5 * 3600
    fake.set_privacy(vid, "public")
    svc2 = CommentService(ps, api_factory=env.api_factory, jobs=lambda: jobs, clock=env.clock, sleep=env.clock.sleep,
                          connected=lambda p: True)
    svc2.run_once(catch_up=True)
    assert svc2.store.tasks()[0].status == READY
    env.clock.t += 61
    svc2.run_once()
    assert svc2.store.tasks()[0].status == POSTED and len(threads_on(fake, vid)) == 1


def test_wrong_channel_task_is_not_posted(world, fake):
    env, ps, kr, jp, jobs, svc = world
    vid = video(fake, kr, "public")
    jobs.append(job(vid, kr, privacy="public"))
    svc.sync_tasks()
    env.accounts[kr.profile_id] = (JP, "tok-jp-in-kr")  # 한국 프로필 연결이 일본 채널 계정
    env.clock.t += 61
    svc.process_tasks()
    t = svc.store.tasks()[0]
    assert t.status == FAILED and threads_on(fake, vid) == []


# ================= 오류 분리 =================

def test_permission_vs_auth_errors_are_separate(world, fake):
    env, ps, kr, jp, jobs, svc = world
    vid = video(fake, kr, "public")
    jobs.append(job(vid, kr, privacy="public"))
    svc.sync_tasks()
    env.clock.t += 61
    fake.fail.append(("commentThreads", 403, "insufficientPermissions"))
    svc.process_tasks()
    t = svc.store.tasks()[0]
    assert t.status == FAILED and t.error_kind == E_INSUFFICIENT_PERMISSION and t.error == REAUTH_MESSAGE
    assert svc.store.settings_for(kr).needs_reauth
    e401 = YouTubeApiError("x", kind="auth", status=401)
    e403 = YouTubeApiError("x", kind="config", reason="insufficientPermissions", status=403)
    assert classify_error(e401) == E_AUTH and classify_error(e403) == E_INSUFFICIENT_PERMISSION
    assert classify_error(YouTubeApiError("x", kind="config", reason="commentsDisabled", status=403)) == COMMENTS_DISABLED
    assert classify_error(YouTubeApiError("x", kind="config", reason="quotaExceeded", status=403)) == "QUOTA_EXCEEDED"
    assert classify_error(YouTubeApiError("x", kind="not_found", reason="videoNotFound", status=404)) == "VIDEO_NOT_FOUND"


def test_401_expired_auth(world, fake):
    env, ps, kr, jp, jobs, svc = world
    vid = video(fake, kr, "public")
    jobs.append(job(vid, kr, privacy="public"))
    svc.sync_tasks()
    env.clock.t += 61
    orig = env.api_factory

    def expired(profile, store):
        api = orig(profile, store)
        fake.valid_tokens.discard("tok-kr")
        return api
    svc.api_factory = expired
    svc.process_tasks()
    t = svc.store.tasks()[0]
    assert t.status == FAILED and t.error_kind == E_AUTH and not svc.store.settings_for(kr).needs_reauth
    fake.valid_tokens.add("tok-kr")


def test_429_retry_inside_client_then_backoff(world, fake):
    env, ps, kr, jp, jobs, svc = world
    vid = video(fake, kr, "public")
    jobs.append(job(vid, kr, privacy="public"))
    svc.sync_tasks()
    env.clock.t += 61
    fake.fail.append(("commentThreads", 429, "rateLimitExceeded"))
    svc.process_tasks()
    assert svc.store.tasks()[0].status == POSTED  # 1번 429 → 자동 재시도 성공
    vid2 = video(fake, kr, "public")
    jobs.append(job(vid2, kr, privacy="public"))
    svc.sync_tasks()
    env.clock.t += 61
    for _ in range(6):
        fake.fail.append(("commentThreads", 429, "rateLimitExceeded"))
    svc.process_tasks()
    t = [x for x in svc.store.tasks() if x.video_id == vid2][0]
    assert t.status == READY and t.error_kind == E_RATE_LIMITED and t.next_at >= env.clock() + 300
    fake.fail.clear()
    env.clock.t = t.next_at + 1
    svc.process_tasks()
    assert [x for x in svc.store.tasks() if x.video_id == vid2][0].status == POSTED


def test_network_error_retries(world, fake, monkeypatch):
    env, ps, kr, jp, jobs, svc = world
    vid = video(fake, kr, "public")
    jobs.append(job(vid, kr, privacy="public"))
    svc.sync_tasks()
    env.clock.t += 61
    orig = env.api_factory
    state = {"n": 0}

    def flaky(profile, store):
        api = orig(profile, store)
        real = api.insert_top_level_comment

        def once(*a, **k):
            state["n"] += 1
            if state["n"] == 1:
                raise YouTubeApiError("네트워크", kind="transient", reason="network")
            return real(*a, **k)
        api.insert_top_level_comment = once
        return api
    svc.api_factory = flaky
    svc.process_tasks()
    t = svc.store.tasks()[0]
    assert t.status == READY and t.error_kind == E_NETWORK and t.attempts == 1 and t.next_at == env.clock() + 120
    env.clock.t += 121
    svc.process_tasks()
    assert svc.store.tasks()[0].status == POSTED and len(threads_on(fake, vid)) == 1


# ================= 새 댓글 확인 / 자동답글 =================

def enable(svc, profile, **kw):
    cs = svc.store.settings_for(profile)
    for k, v in kw.items():
        setattr(cs, k, v)
    svc.store.save_settings(cs)
    return cs


def test_channel_wide_polling_kr_jp_isolation_and_self(world, fake):
    env, ps, kr, jp, jobs, svc = world
    enable(svc, kr)
    enable(svc, jp)
    a, b = video(fake, kr, "public"), video(fake, kr, "public")
    c = video(fake, jp, "public")
    fake.add_viewer_comment(a, "정말 힐링되네요 감사합니다")
    fake.add_viewer_comment(b, "이 곡 제목이 뭔가요?")
    fake.add_viewer_comment(c, "素敵な曲ですね、ありがとう")
    env.api_factory(kr, ps).insert_top_level_comment(kr.channel_id, a, KR_FIRST)  # 내 첫 댓글
    before = ops(fake, "commentThreads")
    res = svc.poll_all()
    assert ops(fake, "commentThreads") - before == 2  # 채널마다 1번 (영상마다 조회하지 않음)
    kr_recs = {r.text: r for r in svc.store.records(kr.profile_id)}
    jp_recs = {r.text: r for r in svc.store.records(jp.profile_id)}
    assert set(jp_recs) == {"素敵な曲ですね、ありがとう"} and "素敵な曲ですね、ありがとう" not in kr_recs
    assert kr_recs["정말 힐링되네요 감사합니다"].status == C_NEW
    assert kr_recs["이 곡 제목이 뭔가요?"].status == C_REVIEW and "질문" in kr_recs["이 곡 제목이 뭔가요?"].reason
    assert kr_recs[KR_FIRST].status == C_SELF
    assert res[kr.profile_id].new == 2 and svc.counts(kr.profile_id)["new"] == 1
    # 두 번째 확인: 이미 본 댓글에서 멈춤, 새 댓글만
    env.clock.t += 600
    fake.add_viewer_comment(a, "최고예요")
    svc.poll_profile(kr)
    assert len([r for r in svc.store.records(kr.profile_id)]) == 4


def test_long_offline_paginates_until_last_seen_with_limit(world, fake):
    env, ps, kr, jp, jobs, svc = world
    enable(svc, kr)
    a = video(fake, kr, "public")
    fake.add_viewer_comment(a, "첫 댓글 감사")
    svc.poll_profile(kr)
    env.clock.t += 60
    for i in range(130):  # 오래 꺼져 있던 사이 130개
        env.clock.t += 1
        fake.add_viewer_comment(a, f"좋아요 {i}")
    res = svc.poll_profile(kr)
    assert res.pages == 3 and len(svc.store.records(kr.profile_id)) == 131
    for i in range(700):
        env.clock.t += 1
        fake.add_viewer_comment(a, f"좋네 {i}")
    res = svc.poll_profile(kr, max_pages=10)
    assert res.pages == 10  # 무한 pagination 금지


def test_manual_owner_reply_detected(world, fake):
    env, ps, kr, jp, jobs, svc = world
    enable(svc, kr)
    a = video(fake, kr, "public")
    t1 = fake.add_viewer_comment(a, "감사합니다 좋아요")
    fake.add_reply(t1, "고맙습니다!", author_channel=KR["id"])  # YouTube Studio에서 직접 답글
    t2 = fake.add_viewer_comment(a, "잘 들었어요 감사")
    for i in range(6):
        fake.add_reply(t2, f"동감 {i}", author_channel=f"UCother{i}")
    fake.add_reply(t2, "감사합니다 :)", author_channel=KR["id"])  # 6개 뒤 → part=replies 목록에는 안 보임
    svc.poll_profile(kr)
    recs = {r.comment_id: r for r in svc.store.records(kr.profile_id)}
    assert recs[t1].status == C_MANUAL and recs[t2].status == C_NEW
    with pytest.raises(ValueError, match="직접 답글"):
        svc.reply(t2, "감사합니다")  # comments.list(parentId)로 확인 → 중복 답글 안 함
    assert svc.store.record(t2).status == C_MANUAL
    assert all(r["snippet"]["textOriginal"] != "감사합니다" for r in fake.replies_to(t2))


def test_assess_rules():
    assert assess_comment("정말 좋아요 감사합니다")[0]
    assert assess_comment("素敵な曲、ありがとう")[0]
    for text, reason in (("이 곡 제목 알려주세요?", "질문"), ("曲名は？", "질문"), ("좋아요 https://spam.example", "링크"),
                         ("좋아요 " + "가" * 100, "긴"), ("", "빈"), ("음", "어려움"), ("구독하고 갑니다 좋아요", "확인")):
        safe, why = assess_comment(text)
        assert not safe and reason in why, (text, why)
    assert not assess_comment("좋아요 감사", exclude_keywords=["감사"])[0]
    assert not assess_comment("좋아요", moderation="heldForReview")[0]


def test_safe_auto_reply_cap_interval_rotation_and_no_duplicates(world, fake):
    env, ps, kr, jp, jobs, svc = world
    enable(svc, kr, reply_mode=REPLY_AUTO, daily_cap=10)
    a = video(fake, kr, "public")
    svc.poll_profile(kr)  # 자동답글 시작 시점 (이전 댓글에는 몰아서 답하지 않음)
    env.clock.t += 60
    q = fake.add_viewer_comment(a, "이 곡 제목이 뭔가요?")
    url = fake.add_viewer_comment(a, "좋아요 www.spam.com")
    safe = []
    for i in range(12):
        env.clock.t += 1
        safe.append(fake.add_viewer_comment(a, f"정말 좋아요 감사합니다 {i}"))
    t0 = env.clock()
    res = svc.poll_profile(kr)
    assert res.auto_replied == 10  # 하루 최대 10
    recs = {r.comment_id: r for r in svc.store.records(kr.profile_id)}
    assert recs[q].status == C_REVIEW and recs[url].status == C_REVIEW and not fake.replies_to(q)
    replied = sorted((r for r in recs.values() if r.status == C_REPLIED), key=lambda r: r.replied_at)
    assert len(replied) == 10 and all(r.auto for r in replied)
    gaps = [b.replied_at - a.replied_at for a, b in zip(replied, replied[1:])]
    assert all(g >= 60 for g in gaps) and env.clock() - t0 >= 9 * 60  # 답글 사이 60초
    texts = [r.reply_text for r in replied]
    assert all(x != y for x, y in zip(texts, texts[1:])) and len(set(texts)) >= 3  # 같은 문장 연속 금지
    assert set(texts) <= set(DEFAULT_REPLIES["ko"])
    # 다음 확인: 남은 2개는 한도 때문에 대기, 이미 답한 댓글에는 다시 안 씀
    env.clock.t += 600
    svc.poll_profile(kr)
    assert sum(len(fake.replies_to(t)) for t in safe) == 10
    with pytest.raises(ValueError, match="이미"):
        svc.reply(replied[0].comment_id, "또 감사합니다")
    env.clock.t += 86400
    svc.poll_profile(kr)
    assert sum(len(fake.replies_to(t)) for t in safe) == 12
    u = today_usage(env.clock)
    assert u["replies"] >= 2 and u["comment_reads"] >= 1


def test_reply_templates_rotation_and_warnings():
    tpl = DEFAULT_REPLIES["ja"]
    assert len(tpl) >= 5 and choose_reply("c1", tpl) == choose_reply("c1", tpl)  # 재현 가능
    picked = choose_reply("c1", tpl)
    assert choose_reply("c1", tpl, last_text=picked) != picked
    assert template_warnings(DEFAULT_REPLIES["ko"]) == []
    w = template_warnings(["구독 부탁드려요", "구독 부탁드려요"])
    assert any("5개" in x for x in w) and any("같은" in x for x in w) and any("홍보" in x for x in w)
    for lang in ("ko", "ja", "en"):
        assert not any(p in t for t in DEFAULT_REPLIES[lang] for p in ("구독", "좋아요 눌러", "subscribe", "チャンネル登録"))


def test_review_mode_never_auto_replies_and_quota_counter(world, fake):
    env, ps, kr, jp, jobs, svc = world
    enable(svc, kr)  # 기본: 검토 후 답글
    a = video(fake, kr, "public")
    svc.poll_profile(kr)
    env.clock.t += 60
    tid = fake.add_viewer_comment(a, "정말 좋아요 감사합니다")
    svc.poll_profile(kr)
    assert fake.replies_to(tid) == [] and svc.store.record(tid).status == C_NEW
    rec = svc.reply(tid, svc.store.record(tid).recommended)
    assert rec.status == C_REPLIED and not rec.auto and len(fake.replies_to(tid)) == 1
    u = today_usage(env.clock)
    assert u["comment_reads"] == 2 and u["replies"] == 1 and u["comment_units"] >= 52


# ================= OAuth 호환 =================

def test_connect_profile_scope_default_and_comment_scopes(fake):
    import threading
    import urllib.parse
    import urllib.request
    from app.youtube_accounts import connect_profile
    from app.youtube_oauth import COMMENT_SCOPES, YOUTUBE_SCOPE, OAuthClient, has_comment_scope
    seen = []

    def browser(url):
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
        seen.append(q["scope"])
        threading.Thread(target=lambda: urllib.request.urlopen(
            q["redirect_uri"] + "/?" + urllib.parse.urlencode({"code": "c", "state": q["state"]}), timeout=10).read(),
            daemon=True).start()
    from pathlib import Path
    from app.youtube_oauth import YouTubeAuthStore
    ps = ProfileStore(store_factory=lambda pid: YouTubeAuthStore(Path("unused"), is_windows=False))  # 메모리만
    p = ps.add(ChannelProfile(new_profile_id(), "🇰🇷 한국 시니어"))
    client = OAuthClient("cid", "GOCSPX-fake", token_uri=fake.token_uri)
    connect_profile(ps, p, "x.json", open_browser=browser, client=client, api_base=fake.api_base, timeout=10)
    connect_profile(ps, ps.get(p.profile_id), "x.json", open_browser=browser, client=client, api_base=fake.api_base,
                    timeout=10, scope=COMMENT_SCOPES)
    assert seen[0] == YOUTUBE_SCOPE  # 기본: 기존 scope 그대로 (기존 사용자 재인증 강요 없음)
    assert seen[1].split() == [YOUTUBE_SCOPE, "https://www.googleapis.com/auth/youtube.force-ssl"]
    assert has_comment_scope({"scope": COMMENT_SCOPES}) is True and has_comment_scope({"scope": YOUTUBE_SCOPE}) is False
    assert has_comment_scope({}) is None


def test_channel_manager_requests_comment_scope_only_after_permission_error(root, world, tmp_path):
    import json
    from app.youtube_channels_ui import ChannelManagerWindow
    from app.youtube_oauth import COMMENT_SCOPES
    env, ps, kr, jp, jobs, svc = world
    cf = tmp_path / "client_secret.json"
    cf.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "GOCSPX-fake",
                                            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                                            "token_uri": "https://oauth2.googleapis.com/token"}}))
    kr.client_file = str(cf)
    ps.save(kr)
    calls = []

    def fake_connect(profiles, p, path, open_browser, **kw):
        calls.append(kw)
        return profiles.save(p)
    cm = ChannelManagerWindow(root, profiles=ps, connect=fake_connect, open_browser=lambda u: None)
    cm.tree.selection_set(kr.profile_id)
    cm._on_select()
    assert not cm.comment_scope.get()
    cm.start_connect()
    assert pump(root, lambda: not cm.busy and calls)
    assert calls[-1] == {}  # 보통 재연결: 기존 scope
    enable(svc, kr, needs_reauth=True)  # 실제 API에서 댓글 권한 부족 확인됨
    cm.selected_id = ""
    cm.tree.selection_set(kr.profile_id)
    cm._on_select()
    assert cm.comment_scope.get() and "댓글 권한" in cm.message.get()
    cm.start_connect()
    assert pump(root, lambda: not cm.busy and len(calls) == 2)
    assert calls[-1] == {"scope": COMMENT_SCOPES} and not svc.store.settings_for(kr).needs_reauth
    cm.destroy()


# ================= UI / 업로드 연동 =================

@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    r.withdraw()
    yield r
    r.destroy()


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from tkinter import messagebox
    shown = []
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(messagebox, name, lambda *a, **k: shown.append(a))
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    return shown


def pump(root, cond, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.03)
    return cond()


def test_comment_manager_ui(root, world, fake):
    from app.youtube_comments_ui import OFFLINE_NOTE, CommentManagerWindow
    env, ps, kr, jp, jobs, svc = world
    a = video(fake, jp, "public")
    w = CommentManagerWindow(root, service=svc, profile_id=jp.profile_id)

    def texts(widget):
        out = [str(widget.cget("text"))] if "text" in widget.keys() else []
        for c in widget.winfo_children():
            out += texts(c)
        return out
    assert w.reply_mode.get() == "검토 후 답글" and OFFLINE_NOTE in texts(w)  # 꺼져 있으면 댓글 불가 안내
    w.check_now()
    assert pump(root, lambda: "새 댓글" in w.message.get() and not w.busy)
    env.clock.t += 60
    t_ok = fake.add_viewer_comment(a, "素敵な曲ですね、ありがとう")
    t_q = fake.add_viewer_comment(a, "この曲の名前は？")
    w.check_now()
    assert pump(root, lambda: "새 댓글 2개" in w.message.get())
    assert w.tree.set(t_ok, "state") == "새 댓글" and w.tree.set(t_q, "state").startswith("검토 필요")
    assert w.tree.set(t_ok, "reply").startswith("추천: ") and "검토 필요 1" in w.counts_text.get()
    w.tree.selection_set(t_ok)
    w.reply_selected("ありがとうございます!")
    assert pump(root, lambda: "답글을 달았습니다" in w.message.get())
    assert w.tree.set(t_ok, "state") == "답글 완료" and fake.replies_to(t_ok)[0]["snippet"]["textOriginal"] == "ありがとうございます!"
    w.tree.selection_set(t_q)
    w.mark_selected("EXCLUDED")
    assert not w.tree.exists(t_q)
    w.show_closed.set(True)
    w.refresh()
    assert w.tree.set(t_q, "state") == "자동답글 제외"
    w.tree.selection_set(t_ok)
    d = w.show_full()
    assert d.winfo_exists()
    d.destroy()
    w.reply_mode.set("자동답글 (안전형)")
    w.save_settings()
    assert svc.store.settings_for(jp).reply_mode == REPLY_AUTO
    assert "댓글 조회" in w.usage.get() and "Google Cloud 실제 quota 아님" in w.usage.get()
    w.destroy()


def test_batch_ten_videos_preview_and_ten_tasks(root, world, fake, tmp_path):
    from app.youtube_upload_ui import MultiChannelUploadWindow
    env, ps, kr, jp, jobs, svc = world
    q = env.queue(ps)
    svc.jobs = q.snapshot
    w = MultiChannelUploadWindow(root, upload_queue=q, clock=env.clock, comments=svc)
    w.select_profile(jp.profile_id)
    d = tmp_path / "ten"
    d.mkdir()
    for i in range(1, 11):
        (d / f"{i:03d}.mp4").write_bytes(b"x" * 3000)
    assert w.add_folder(str(d)) == 10
    w.toggle_detail()
    w.cb_first_comment.current(0)
    w._use_first_comment_preset()
    assert w.first_comment_on.get() and "ありがとう" in w.txt_first_comment.get("1.0", "end")
    from zoneinfo import ZoneInfo
    day = datetime.fromtimestamp(env.clock(), timezone.utc).astimezone(ZoneInfo("Asia/Tokyo")).date() + timedelta(days=1)
    w.pub_date.set(day.isoformat())
    dlg = w.preview()
    assert pump(root, lambda: dlg.verify_state == "ok")
    assert dlg.plan.first_comment_text == "10/10 자동등록 예정 (공개 후)"
    jobs10 = dlg.confirm()
    assert len(jobs10) == 10 and all(j.first_comment.startswith("今日も") for j in jobs10)
    assert w.tree.set(jobs10[0].job_id, "comment") == "업로드 후 공개되면 자동등록"
    q.run_pending()
    assert all(j.status == COMPLETE for j in q.snapshot())
    assert svc.sync_tasks() == 10 and svc.sync_tasks() == 0
    assert all(t.status == WAITING_PUBLIC for t in svc.store.tasks())
    w.refresh_jobs()
    assert w.tree.set(jobs10[0].job_id, "comment") == "공개 후 자동등록 대기"  # '댓글 완료'를 미리 표시하지 않음
    w.tree.selection_set(jobs10[0].job_id)
    w._on_select()
    assert "영상 예약 완료" in w.detail.get() and "공개 예정" in w.detail.get() and "첫 댓글: 공개 후 자동등록 대기" in w.detail.get()
    assert not any(fake.threads)
    cw = w.open_comments()
    assert cw.winfo_exists() and len(cw.task_tree.get_children()) == 10
    w.destroy()
