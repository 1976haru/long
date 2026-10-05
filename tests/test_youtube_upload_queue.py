"""Gate F/G: 다채널 예약 업로드 대기열 (KR→JP→KR, 채널 불일치 차단, 재실행 이어 올리기, 썸네일 실패, API 제한,
세션 URL 비밀 보호) + settings 동시 저장. fake 서버만 사용, 실제 YouTube 접속 없음."""
import json
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import settings
from app.settings import load_settings, save_settings, update_settings
from app.youtube_accounts import ChannelProfile, ProfileStore, new_profile_id
from app.youtube_api import YouTubeApiClient
from app.youtube_upload import ResumableUploader, http_transport
from app.youtube_upload_queue import (
    API_REVIEW_REQUIRED, BLOCKED, COMPLETE, PARTIAL, PAUSED, PENDING, SETTINGS_KEY, QueueError, UploadQueue,
)
from youtube_fakes import FakeClock, FakeYouTube

KB256 = 256 * 1024
KR = {"id": "UCkr000000000000000000KR", "title": "한국 시니어"}
JP = {"id": "UCjp000000000000000000JP", "title": "CHILI LAB"}
DPAPI = pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")


def jpeg(path, w=1280, h=720):
    path.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
                     + b"\xff\xc0\x00\x11\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x03" + b"\x00" * 9 + b"\xff\xd9")
    return path


def png(path, w=1280, h=720):
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x02" + b"\x00" * 20)
    return path


@pytest.fixture
def fake():
    f = FakeYouTube()
    yield f
    f.close()


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "LONG_FINAL.mp4"
    p.write_bytes(bytes(range(256)) * (4 * 1024 + 7))
    return p


@pytest.fixture
def profiles():
    ps = ProfileStore()
    kr = ps.add(ChannelProfile(new_profile_id(), "🇰🇷 한국 시니어", channel_id=KR["id"], language="ko", timezone="Asia/Seoul"))
    jp = ps.add(ChannelProfile(new_profile_id(), "🇯🇵 CHILI LAB", channel_id=JP["id"], language="ja", timezone="Asia/Tokyo"))
    return ps, kr, jp


class Env:
    """프로필 → (실제로 로그인된 채널, access token). 작업마다 새 api client를 만든다."""

    def __init__(self, fake, ps, kr, jp):
        self.fake, self.clock = fake, FakeClock()
        self.accounts = {kr.profile_id: (KR, "tok-kr"), jp.profile_id: (JP, "tok-jp")}
        self.factory_calls = []
        self.spy = None

    def api_factory(self, profile, store):
        channel, token = self.accounts[profile.profile_id]
        self.factory_calls.append(profile.profile_id)
        self.fake.valid_tokens.add(token)
        self.fake.token_channels[token] = channel
        return YouTubeApiClient(lambda force_refresh=False: token, base_url=self.fake.api_base, sleep=lambda s: None)

    def uploader_factory(self, api, cancel=None):
        return ResumableUploader(api, transport=self.spy or http_transport, chunk_size=KB256, sleep=lambda s: None,
                                 cancel=cancel)

    def queue(self, ps):
        return UploadQueue(ps, api_factory=self.api_factory, uploader_factory=self.uploader_factory, clock=self.clock)

    def future(self, hours=24):
        return datetime.fromtimestamp(self.clock() + hours * 3600, timezone.utc)


@pytest.fixture
def env(fake, profiles):
    return Env(fake, *profiles)


def settings_text():
    return settings.settings_file().read_text(encoding="utf-8")


def assert_no_secrets(text):
    for s in ("upload_id", "tok-kr", "tok-jp", "Bearer", "refresh", "GOCSPX", "/upload/youtube"):
        assert s not in text


# ---------------- 다채널 순차 ----------------

def test_kr_jp_kr_queue_uses_each_profile_channel(env, profiles, fake, video):
    ps, kr, jp = profiles
    q = env.queue(ps)
    order = [kr, jp, kr]
    jobs = [q.add(q.make_job(profile_id=p.profile_id, video_path=str(video), title=f"영상 {i}",
                             publish_at=env.future(24 + i))) for i, p in enumerate(order)]
    q.run_pending()
    assert [j.status for j in q.jobs] == [COMPLETE] * 3
    assert env.factory_calls == [kr.profile_id, jp.profile_id, kr.profile_id]  # 작업마다 새 OAuth/api
    for job, p in zip(jobs, order):
        v = fake.videos[job.video_id]
        assert v["channel"] == p.channel_id and v["bytes"] == video.read_bytes()
        assert v["status"]["privacyStatus"] == "private" and v["status"]["publishAt"] == job.publish_at_utc
    assert len({j.video_id for j in jobs}) == 3
    assert jobs[1].timezone == "Asia/Tokyo" and jobs[0].timezone == "Asia/Seoul"
    assert_no_secrets(settings_text())
    assert "upload_id" not in repr(q) and all("upload_id" not in repr(j) for j in q.jobs)


@pytest.mark.parametrize("expected,actual", [("kr", JP), ("jp", KR)])
def test_wrong_channel_is_blocked_before_upload_and_queue_continues(env, profiles, fake, video, expected, actual):
    ps, kr, jp = profiles
    target = kr if expected == "kr" else jp
    other = jp if expected == "kr" else kr
    env.accounts[target.profile_id] = (actual, "tok-wrong")
    q = env.queue(ps)
    bad = q.add(q.make_job(profile_id=target.profile_id, video_path=str(video), title="차단", publish_at=env.future()))
    ok = q.add(q.make_job(profile_id=other.profile_id, video_path=str(video), title="정상", publish_at=env.future()))
    q.run_pending()
    assert bad.status == BLOCKED and target.channel_id in bad.error and actual["id"] in bad.error
    assert not bad.video_id and ok.status == COMPLETE
    assert all(s["channel"] == other.channel_id for s in fake.sessions.values())  # 차단된 작업은 세션도 만들지 않음


def test_profile_channel_changed_after_enqueue_blocks(env, profiles, video):
    ps, kr, jp = profiles
    q = env.queue(ps)
    job = q.add(q.make_job(profile_id=kr.profile_id, video_path=str(video), title="x", publish_at=env.future()))
    kr.channel_id = "UCother000000000000000XX"
    ps.save(kr)
    q.run_pending()
    assert job.status == BLOCKED and env.factory_calls == []


# ---------------- 예약 시간 ----------------

def test_schedule_rules_on_enqueue(env, profiles, video):
    ps, kr, jp = profiles
    q = env.queue(ps)
    with pytest.raises(QueueError):
        q.make_job(profile_id=kr.profile_id, video_path=str(video), title="x", publish_at=env.future(-1))
    with pytest.raises(QueueError):
        q.make_job(profile_id=kr.profile_id, video_path=str(video), title="x",
                   publish_at=env.future(1).replace(tzinfo=None))
    job = q.make_job(profile_id=jp.profile_id, video_path=str(video), title="x", privacy="public",
                     publish_at=env.future())
    assert job.publish_at_utc.endswith("Z")
    q.add(job)
    q.run_pending()
    assert job.status == COMPLETE  # 예약은 public 선택이어도 private + publishAt으로 올라간다


def test_queue_limit_and_part_file(env, profiles, video, tmp_path, monkeypatch):
    import app.youtube_upload_queue as m
    ps, kr, jp = profiles
    monkeypatch.setattr(m, "MAX_JOBS", 2)
    q = env.queue(ps)
    for _ in range(2):
        q.add(q.make_job(profile_id=kr.profile_id, video_path=str(video), title="x"))
    with pytest.raises(QueueError, match="최대"):
        q.add(q.make_job(profile_id=kr.profile_id, video_path=str(video), title="x"))
    part = tmp_path / "LONG.part.mp4"
    part.write_bytes(b"x")
    with pytest.raises(QueueError):
        q.make_job(profile_id=kr.profile_id, video_path=str(part), title="x")


# ---------------- 재실행 이어 올리기 ----------------

class CancelAfter:
    def __init__(self, q, n):
        self.q, self.n, self.puts = q, n, 0

    def __call__(self, method, url, headers, body, timeout):
        res = http_transport(method, url, headers, body, timeout)
        if method == "PUT":
            self.puts += 1
            if self.puts == self.n:
                self.q.cancel.set()
        return res


@DPAPI
def test_cancel_then_app_restart_resumes_same_session(env, profiles, fake, video):
    ps, kr, jp = profiles
    q = env.queue(ps)
    job = q.add(q.make_job(profile_id=kr.profile_id, video_path=str(video), title="이어 올리기", publish_at=env.future()))
    env.spy = CancelAfter(q, 2)
    q.run_pending()
    assert job.status == PAUSED and job.session_blob and not job.video_id
    assert len(fake.sessions) == 1 and 0 < len(next(iter(fake.sessions.values()))["data"]) < video.stat().st_size
    assert_no_secrets(settings_text())  # 세션 URL은 DPAPI 암호문으로만 저장
    # 업로드 도중 앱이 꺼진 것처럼: 상태가 UPLOADING인 채로 저장돼 있었다
    data = load_settings()
    data[SETTINGS_KEY][0]["status"] = "UPLOADING"
    save_settings(data)
    env.spy = None
    q2 = env.queue(ps)
    assert q2.jobs[0].status == PENDING and q2.jobs[0].session_blob == job.session_blob
    q2.run_pending()
    j2 = q2.jobs[0]
    assert j2.status == COMPLETE and len(fake.sessions) == 1  # 새 세션/중복 업로드 없음
    assert fake.videos[j2.video_id]["bytes"] == video.read_bytes() and j2.session_blob == ""


def test_expired_session_after_restart_starts_new_session(env, profiles, fake, video, monkeypatch):
    import app.youtube_upload_queue as m
    ps, kr, jp = profiles
    q = env.queue(ps)
    job = q.add(q.make_job(profile_id=kr.profile_id, video_path=str(video), title="만료", publish_at=env.future()))
    monkeypatch.setattr(m, "_unprotect", lambda blob, w: blob and fake.base + "/upload/youtube/v3/videos?uploadType=resumable&upload_id=gone")
    job.session_blob = "x"
    q.run_pending()
    assert job.status == COMPLETE and len(fake.sessions) == 1
    assert fake.videos[job.video_id]["bytes"] == video.read_bytes()


def test_video_changed_after_enqueue_is_not_uploaded(env, profiles, fake, video):
    ps, kr, jp = profiles
    q = env.queue(ps)
    job = q.add(q.make_job(profile_id=kr.profile_id, video_path=str(video), title="x"))
    video.write_bytes(b"different" * 10)
    q.run_pending()
    assert job.status == "FAILED" and fake.sessions == {}


# ---------------- 썸네일 ----------------

@pytest.mark.parametrize("name,maker", [("t.jpg", jpeg), ("t.jpeg", jpeg), ("t.png", png)])
def test_thumbnail_formats(env, profiles, fake, video, tmp_path, name, maker):
    ps, kr, jp = profiles
    thumb = maker(tmp_path / name)
    q = env.queue(ps)
    job = q.add(q.make_job(profile_id=jp.profile_id, video_path=str(video), title="x", thumbnail_path=str(thumb),
                           publish_at=env.future()))
    q.run_pending()
    assert job.status == COMPLETE and job.thumbnail_done is True
    assert fake.thumbnails[job.video_id] == thumb.read_bytes()


def test_thumbnail_failure_keeps_video_and_retry_only_thumbnail(env, profiles, fake, video, tmp_path):
    ps, kr, jp = profiles
    thumb = jpeg(tmp_path / "t.jpg")
    q = env.queue(ps)
    job = q.add(q.make_job(profile_id=kr.profile_id, video_path=str(video), title="x", thumbnail_path=str(thumb),
                           publish_at=env.future()))
    fake.fail.append(("upload/thumbnails", 400, "invalidImage"))
    q.run_pending()
    assert job.status == PARTIAL and job.thumbnail_done is False and job.video_id in fake.videos
    assert not any(m == "DELETE" for m, _ in fake.ops())  # 영상은 지우지 않는다
    vid = job.video_id
    q.retry_thumbnail(job.job_id)
    q.run_pending()
    assert job.status == COMPLETE and job.video_id == vid and len(fake.sessions) == 1
    assert fake.thumbnails[vid] == thumb.read_bytes()


# ---------------- API 프로젝트 제한 ----------------

def test_private_only_api_project_needs_review_and_never_reuploads(env, profiles, fake, video):
    ps, kr, jp = profiles
    fake.private_only = True
    q = env.queue(ps)
    job = q.add(q.make_job(profile_id=kr.profile_id, video_path=str(video), title="x", publish_at=env.future()))
    nxt = q.add(q.make_job(profile_id=jp.profile_id, video_path=str(video), title="y", publish_at=env.future()))
    q.run_pending()
    assert job.status == API_REVIEW_REQUIRED and job.video_id and nxt.status == API_REVIEW_REQUIRED
    assert len(fake.sessions) == 2
    q.retry(job.job_id)
    q.run_pending()
    assert job.status == API_REVIEW_REQUIRED and len(fake.sessions) == 2 and len(fake.videos) == 2  # 다시 올리지 않음


# ---------------- 보안: 실패 경로 ----------------

def test_failure_messages_have_no_session_url_or_token(env, profiles, fake, video):
    ps, kr, jp = profiles
    q = env.queue(ps)
    job = q.add(q.make_job(profile_id=kr.profile_id, video_path=str(video), title="x", publish_at=env.future()))
    fake.fail.append(("upload/videos", 400, "badRequest"))
    q.run_pending()
    assert job.status == "FAILED"
    events = []
    while not q.events.empty():
        events.append(q.events.get())
    for text in (job.error, repr(job), repr(q), json.dumps(events, ensure_ascii=False), settings_text()):
        assert_no_secrets(text)


# ---------------- settings ----------------

def test_settings_atomic_save_keeps_old_file_on_error(monkeypatch):
    save_settings({"queue": [{"a": 1}], "youtube": {"stream_mode": "auto"}})
    before = settings_text()
    with pytest.raises(TypeError):
        save_settings({"bad": object()})
    assert settings_text() == before

    def boom(src, dst):
        raise OSError("disk")
    monkeypatch.setattr(settings.os, "replace", boom)
    with pytest.raises(OSError):
        save_settings({"x": 1})
    assert settings_text() == before
    assert [p.name for p in settings.settings_dir().iterdir()] == ["settings.json"]  # 임시 파일 남지 않음


def test_concurrent_save_and_load_lose_nothing():
    from app.youtube_config import save_youtube_settings
    save_settings({"queue": [{"name": "기존 영상 작업"}], "prevent_sleep": True})
    errors = []

    def writer(i):
        try:
            for k in range(15):
                update_settings(**{f"w{i}": k})
                save_youtube_settings(**{f"live{i}": k})
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def reader():
        try:
            for _ in range(60):
                assert isinstance(load_settings(), dict) and load_settings().get("queue")
        except Exception as e:  # pragma: no cover
            errors.append(e)
    ts = [threading.Thread(target=writer, args=(i,)) for i in range(4)] + [threading.Thread(target=reader)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    data = load_settings()
    assert errors == []
    assert all(data[f"w{i}"] == 14 and data["youtube"][f"live{i}"] == 14 for i in range(4))
    assert data["queue"] == [{"name": "기존 영상 작업"}] and data["prevent_sleep"] is True


def test_upload_queue_and_profiles_keep_video_queue_and_live_settings(env, profiles, video):
    from app.youtube_config import load_youtube_settings, save_youtube_settings
    ps, kr, jp = profiles
    update_settings(queue=[{"name": "기존 영상 작업"}], continue_on_error=True)
    save_youtube_settings(stream_mode="auto", channel_id="UClive")
    q = env.queue(ps)
    q.add(q.make_job(profile_id=kr.profile_id, video_path=str(video), title="x"))
    q.run_pending()
    ps.save(jp)
    data = load_settings()
    assert data["queue"] == [{"name": "기존 영상 작업"}] and data["continue_on_error"] is True
    assert load_youtube_settings()["stream_mode"] == "auto" and load_youtube_settings()["channel_id"] == "UClive"
    assert len(data[SETTINGS_KEY]) == 1 and len(data["channel_profiles"]) == 2
