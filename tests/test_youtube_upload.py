"""Gate E/H: resumable upload (fake HTTP 서버) + 예약 공개(publishAt) 검증. 실제 YouTube 접속 없음."""
import threading
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import pytest

from app.youtube_api import YouTubeApiClient, YouTubeApiError
from app.youtube_metadata import BroadcastMetadata
from app.youtube_schedule import local_to_utc
from app.youtube_upload import (
    API_RESTRICTED_MESSAGE, CHUNK_SIZE, ApiRestrictedError, ResumableUploader, SessionExpired, UploadCancelled,
    build_video_body, http_transport, utc_iso, validate_publish_at, validate_video_file, verify_publish_at,
)
from youtube_fakes import FAKE_ACCESS, FakeYouTube

KB256 = 256 * 1024
NOW = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def fake():
    f = FakeYouTube()
    yield f
    f.close()


@pytest.fixture
def api(fake):
    return YouTubeApiClient(lambda force_refresh=False: FAKE_ACCESS, base_url=fake.api_base, sleep=lambda s: None)


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "LONG_FINAL.mp4"
    p.write_bytes(bytes(range(256)) * (4 * 1024 + 7))  # 약 1.0MB, 256KB 배수 아님
    return p


class Spy:
    """transport를 감싸 요청 크기/횟수를 기록하고, 원하는 순서에서 네트워크 끊김을 흉내낸다."""

    def __init__(self, drop_at=(), lose_response_at=()):
        self.bodies, self.methods, self.n = [], [], 0
        self.drop_at, self.lose_response_at = set(drop_at), set(lose_response_at)

    def __call__(self, method, url, headers, body, timeout):
        self.n += 1
        self.methods.append((method, headers.get("Content-Range", "")))
        self.bodies.append(len(body or b""))
        if self.n in self.drop_at:
            raise ConnectionError("ConnectionResetError")  # 요청이 서버에 닿지 않음
        res = http_transport(method, url, headers, body, timeout)
        if self.n in self.lose_response_at:
            raise ConnectionError("TimeoutError")  # 서버는 받았지만 응답을 못 받음
        return res


def body_for(md=None, publish_at=None):
    return build_video_body(md or BroadcastMetadata(title="장시간 플레이리스트", tags=["샹송"], default_language="ko"),
                            publish_at)


def uploader(api, spy=None, **kw):
    return ResumableUploader(api, transport=spy or http_transport, chunk_size=KB256, sleep=lambda s: None, **kw)


def uploaded(fake, vid):
    return fake.videos[vid]["bytes"]


# ---------------- 기본 ----------------

def test_chunk_size_constant():
    assert CHUNK_SIZE == 8 * 1024 * 1024 and CHUNK_SIZE % KB256 == 0
    with pytest.raises(ValueError):
        ResumableUploader(None, chunk_size=100_000)


def test_normal_upload_in_256kb_chunks_without_reading_whole_file(api, fake, video, monkeypatch):
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("영상 전체를 메모리로 읽으면 안 됨"))
    spy = Spy()
    sessions, progress = [], []
    res = uploader(api, spy).upload(video, body_for(), on_session=sessions.append, on_progress=progress.append)
    with open(video, "rb") as f:
        assert uploaded(fake, res["id"]) == f.read()
    assert max(spy.bodies[1:]) <= KB256  # 첫 요청은 metadata JSON
    puts = [r for m, r in spy.methods if m == "PUT"]
    assert len(puts) == -(-video.stat().st_size // KB256)  # 308로 이어가며 chunk 수만큼
    assert len(sessions) == 1 and progress[-1].fraction == 1.0
    assert fake.ops().count(("POST", "upload/videos")) == 1
    v = fake.videos[res["id"]]
    assert v["snippet"]["tags"] == ["샹송"] and v["snippet"]["defaultLanguage"] == "ko"
    assert spy.methods[0][0] == "POST"


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_http_errors_resume_from_server_offset(api, fake, video, status):
    fake.upload_fail.extend([("chunk", status, False), ("chunk", status, True)])  # 두 번째는 서버가 받은 뒤 오류
    res = uploader(api).upload(video, body_for())
    assert uploaded(fake, res["id"]) == video.read_bytes()
    assert ("PUT", "upload/videos") in fake.ops()
    assert fake.ops().count(("POST", "upload/videos")) == 1  # 세션 1개로 끝까지


def test_network_disconnect_then_offset_query(api, fake, video):
    spy = Spy(drop_at={3}, lose_response_at={5})
    res = uploader(api, spy).upload(video, body_for())
    assert uploaded(fake, res["id"]) == video.read_bytes()
    assert any(r.startswith("bytes */") for _, r in spy.methods)  # 받은 위치 확인 요청


def test_lost_response_on_last_chunk_uses_finished_video(api, fake, video):
    n_chunks = -(-video.stat().st_size // KB256)
    spy = Spy(lose_response_at={1 + n_chunks})  # 마지막 chunk: 서버는 완료, 응답만 유실
    res = uploader(api, spy).upload(video, body_for())
    assert len(fake.videos) == 1 and res["id"] in fake.videos


def test_gives_up_after_too_many_failures(api, fake, video):
    fake.upload_fail.extend([("chunk", 503, False)] * 20)
    with pytest.raises(YouTubeApiError) as ei:
        uploader(api, max_failures=3).upload(video, body_for())
    assert ei.value.retryable


def test_cancel_then_resume_same_session(api, fake, video):
    cancel = threading.Event()
    sessions = []

    def prog(p):
        if p.sent >= 2 * KB256:
            cancel.set()
    with pytest.raises(UploadCancelled):
        uploader(api, cancel=cancel).upload(video, body_for(), on_session=sessions.append, on_progress=prog)
    assert not fake.videos
    res = uploader(api).upload(video, body_for(), session_url=sessions[0])  # '재실행' 후 같은 세션
    assert uploaded(fake, res["id"]) == video.read_bytes()
    assert fake.ops().count(("POST", "upload/videos")) == 1


def test_expired_session_creates_new_session(api, fake, video):
    cancel = threading.Event()
    sessions = []
    with pytest.raises(UploadCancelled):
        uploader(api, cancel=cancel).upload(video, body_for(), on_session=sessions.append,
                                            on_progress=lambda p: p.sent and cancel.set())
    fake.sessions.clear()  # 서버에서 세션 만료
    new = []
    res = uploader(api).upload(video, body_for(), session_url=sessions[0], on_session=new.append)
    assert new and new[0] != sessions[0]
    assert uploaded(fake, res["id"]) == video.read_bytes()


def test_session_expired_mid_upload_raises(api, fake, video):
    def prog(p):
        if p.sent:
            fake.expire_sessions = True
    with pytest.raises(SessionExpired):
        uploader(api).upload(video, body_for(), on_progress=prog)


def test_session_url_never_in_errors_or_repr(api, fake, video):
    sessions = []
    cancel = threading.Event()
    with pytest.raises(UploadCancelled) as ei:
        uploader(api, cancel=cancel).upload(video, body_for(), on_session=sessions.append,
                                            on_progress=lambda p: cancel.set())
    url = sessions[0]
    up = uploader(api)
    assert "upload_id" not in repr(up) and "upload_id" not in str(ei.value)
    fake.fail.append(("upload/videos", 400, "badRequest"))
    with pytest.raises(YouTubeApiError) as e2:
        up.upload(video, body_for(), session_url=url)
    assert "upload_id" not in str(e2.value) and FAKE_ACCESS not in str(e2.value)


def test_foreign_session_url_is_refused_before_sending_token(api, video):
    calls = []
    up = ResumableUploader(api, transport=lambda *a: calls.append(a) or (308, {}, b""), chunk_size=KB256)
    with pytest.raises(YouTubeApiError, match="세션 주소"):
        up.upload(video, body_for(), session_url="https://evil.example.com/upload/youtube/v3/videos?upload_id=x")
    assert calls == []


def test_video_file_rules(tmp_path):
    with pytest.raises(YouTubeApiError):
        validate_video_file(tmp_path / "none.mp4")
    (tmp_path / "x.part.mp4").write_bytes(b"1")
    with pytest.raises(YouTubeApiError, match="part"):
        validate_video_file(tmp_path / "x.part.mp4")
    (tmp_path / "e.mp4").write_bytes(b"")
    with pytest.raises(YouTubeApiError):
        validate_video_file(tmp_path / "e.mp4")
    (tmp_path / "a.txt").write_bytes(b"1")
    with pytest.raises(YouTubeApiError):
        validate_video_file(tmp_path / "a.txt")


# ---------------- publishAt ----------------

def test_publish_at_timezones_and_dst():
    seoul = local_to_utc(date(2026, 10, 10), dtime(19, 0), "Asia/Seoul")
    tokyo = local_to_utc(date(2026, 10, 10), dtime(19, 0), "Asia/Tokyo")
    ny_dst = local_to_utc(date(2026, 11, 1), dtime(1, 30), "America/New_York")  # 두 번 있는 시각 → 첫 번째(EDT)
    assert utc_iso(seoul) == "2026-10-10T10:00:00Z" and utc_iso(tokyo) == "2026-10-10T10:00:00Z"
    assert utc_iso(ny_dst) == "2026-11-01T05:30:00Z"
    b = body_for(BroadcastMetadata(title="T", privacy_status="public"), seoul)
    assert b["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False, "publishAt": "2026-10-10T10:00:00Z"}
    assert body_for(BroadcastMetadata(title="T", privacy_status="unlisted"), None)["status"]["privacyStatus"] == "unlisted"


def test_publish_at_rejects_past_near_and_naive():
    validate_publish_at(NOW + timedelta(hours=1), NOW)
    validate_publish_at(None, NOW)
    for bad in (NOW - timedelta(minutes=1), NOW + timedelta(minutes=2)):
        with pytest.raises(YouTubeApiError):
            validate_publish_at(bad, NOW)
    with pytest.raises(YouTubeApiError, match="시간대"):
        validate_publish_at(datetime(2030, 1, 1), NOW)
    with pytest.raises(ValueError):
        utc_iso(datetime(2030, 1, 1))


def upload_scheduled(api, video, when):
    return uploader(api).upload(video, body_for(publish_at=when))["id"]


def test_verify_publish_at_after_upload(api, fake, video):
    when = datetime(2030, 1, 1, 10, tzinfo=timezone.utc)
    vid = upload_scheduled(api, video, when)
    st = verify_publish_at(api, vid, publish_at=when, privacy="public", made_for_kids=False)
    assert st["privacyStatus"] == "private" and st["publishAt"] == "2030-01-01T10:00:00Z"
    assert ("GET", "videos") in fake.ops()  # videos.list로 실제 상태 확인


def test_verify_publish_at_fixes_once(api, fake, video):
    when = datetime(2030, 1, 1, 10, tzinfo=timezone.utc)
    fake.publish_override = "2030-01-02T10:00:00Z"
    vid = upload_scheduled(api, video, when)
    verify_publish_at(api, vid, publish_at=when, privacy="public", made_for_kids=False)
    assert fake.videos[vid]["status"]["publishAt"] == "2030-01-01T10:00:00Z"
    assert ("PUT", "videos") in fake.ops()


def test_verify_publish_at_mismatch_is_not_complete(api, fake, video, monkeypatch):
    when = datetime(2030, 1, 1, 10, tzinfo=timezone.utc)
    fake.publish_override = "2030-01-02T10:00:00Z"
    vid = upload_scheduled(api, video, when)
    monkeypatch.setattr(api, "update_video_status", lambda *a, **k: {})  # 수정이 반영되지 않음
    with pytest.raises(YouTubeApiError) as ei:
        verify_publish_at(api, vid, publish_at=when, privacy="public", made_for_kids=False)
    assert ei.value.reason == "publishAtMismatch"


def test_api_project_private_only_detected(api, fake, video):
    fake.private_only = True
    when = datetime(2030, 1, 1, 10, tzinfo=timezone.utc)
    vid = upload_scheduled(api, video, when)
    with pytest.raises(ApiRestrictedError) as ei:
        verify_publish_at(api, vid, publish_at=when, privacy="public", made_for_kids=False)
    assert "영상 업로드는 완료됐지만" in str(ei.value) and "예약 공개가 적용되지 않았습니다" in str(ei.value)
    assert str(ei.value) == API_RESTRICTED_MESSAGE
    # 처음부터 비공개를 원했으면 제한이 있어도 정상
    vid2 = uploader(api).upload(video, body_for(BroadcastMetadata(title="T", privacy_status="private")))["id"]
    assert verify_publish_at(api, vid2, publish_at=None, privacy="private", made_for_kids=False)
    assert fake.ops().count(("POST", "upload/videos")) == 2
