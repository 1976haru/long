"""TRUE 예약 LIVE 준비 Pipeline (app/scheduled_live.py) — 가짜 YouTube 서버 + 가짜 SSH 서버(실제 worker 관리 명령).
실제 OCI/YouTube 접속 없음. Stream Key가 job/로그/화면 문구에 남지 않는지 확인한다."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.live_ready import LiveReadyIssue, LiveReadyReport
from app.scheduled_live import (
    DURATION_PRESETS, build_job, cancel_reservation, duration_label, key_fingerprint, pending_cloud_reservations,
    prepare_cloud_schedule, retry_cloud_job, sync_cloud_states, verify_readback,
)
from app.settings import load_settings, update_settings
from app.youtube_api import YouTubeApiClient
from app.youtube_metadata import MetadataTemplate
from app.youtube_schedule import ReservationStore, ScheduleRule, create_reservation, occurrences, top_up
from cloud_fakes import make_client
from cloud_sched_fakes import SchedRemote, w
from youtube_fakes import FAKE_ACCESS, FAKE_STREAM_NAME, FakeYouTube

NOW = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)  # 2026-10-09 19:00 KST (예약 전날)


@pytest.fixture
def fake():
    f = FakeYouTube()
    yield f
    f.close()


@pytest.fixture
def api(fake):
    return YouTubeApiClient(lambda force_refresh=False: FAKE_ACCESS, base_url=fake.api_base, sleep=lambda s: None)


def report(path, *, duration=2716.0, keyframe=2.0, sample_rate=44100, fps=30.0):
    r = LiveReadyReport(path=Path(path), width=1920, height=1080, fps=fps, duration=duration, video_codec="h264",
                        audio_codec="aac", sample_rate=sample_rate, channels=2, keyframe_max=keyframe, keyframe_avg=keyframe)
    if keyframe > 4:
        r.issues.append(LiveReadyIssue("keyframe", f"Keyframe 간격 {keyframe:.0f}초 (4초 이하 필요, 2초 권장)."))
    return r


@pytest.fixture
def media(tmp_path):
    paths = []
    for n, name in enumerate(("girl 01_LIVE_READY.mp4", "man_001_LIVE_READY.mp4")):
        p = tmp_path / "videos" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(f"fake-mp4-{n}".encode() * 2000)
        paths.append(p)
    return paths, [report(paths[0], duration=2716.0), report(paths[1], duration=2693.0)]


@pytest.fixture
def remote(tmp_path):
    return SchedRemote(tmp_path / "server")


@pytest.fixture
def client(tmp_path, remote):
    return make_client(tmp_path, remote)


def store():
    return ReservationStore(load_settings, update_settings)


def rule(mode="ONCE", minutes=120):
    return ScheduleRule(mode=mode, start_date="2026-10-10", start_time_local="19:00", timezone="Asia/Seoul",
                        duration_minutes=minutes).validate()


def tpl(privacy="unlisted"):
    return MetadataTemplate(name="t", title_template="{date} Playlist LIVE #{session}", privacy_status=privacy).validate()


def run(api, client, media, *, r=None, rule_id="rule01", saved_playlist=None, events=None, on_playlist=None):
    paths, reports = media
    ev = events if events is not None else []
    return prepare_cloud_schedule(api=api, client=client, media_paths=paths, reports=reports, rule=r or rule(),
                                  template=tpl(), rule_id=rule_id, store=store(), now=NOW, channel="Old Pop Lounge",
                                  emit=lambda *a: ev.append(a), saved_playlist=saved_playlist, on_playlist=on_playlist)


def test_full_pipeline_creates_autostart_broadcast_and_cloud_job(api, client, remote, fake, media):
    events = []
    out = run(api, client, media, events=events)
    assert out.all_ready and out.made == 1 and out.ready == 1 and out.uploaded == 2 and out.scheduler_ok
    steps = [e[1] for e in events if e[0] == "step" and e[2] == "ok"]
    assert steps == ["analyze", "ready", "compat", "cloud", "disk", "upload", "verify", "manifest", "stream",
                     "youtube", "job", "readback"]
    # YouTube: 예약 Cloud LIVE만 autoStart/autoStop = true, 재사용 stream bind
    (b,) = fake.broadcasts.values()
    cd = b["body"]["contentDetails"]
    assert cd["enableAutoStart"] is True and cd["enableAutoStop"] is True
    assert b["contentDetails"]["boundStreamId"] and b["status"]["privacyStatus"] == "unlisted"
    assert b["snippet"]["scheduledStartTime"].startswith("2026-10-10T10:00:00")
    # Cloud job: UTC 시각, 서버 파일 이름 + SHA256만 (로컬 경로/Stream Key 없음)
    (spec_file,) = list(remote.jobs_dir.glob("*.json"))
    spec = json.loads(spec_file.read_text(encoding="utf-8"))
    assert spec["scheduled_at_utc"] == "2026-10-10T10:00:00Z" and spec["stop_at_utc"] == "2026-10-10T12:00:00Z"
    assert [p["name"] for p in spec["playlist"]][1] == "man_001_LIVE_READY.mp4"
    assert spec["playlist"][0]["name"].startswith("girl_01_LIVE_READY")  # 공백 → 안전한 서버 이름
    raw = spec_file.read_text(encoding="utf-8")
    assert FAKE_STREAM_NAME not in raw and "videos" not in raw and ":\\" not in raw
    assert spec["key_fingerprint"] == key_fingerprint(FAKE_STREAM_NAME)
    # Stream Key는 기존 보안 경로(stdin → 0600 stream.key)로만
    assert remote.key == FAKE_STREAM_NAME + "\n"
    assert all(FAKE_STREAM_NAME not in " ".join(c["args"]) for c in remote.calls)
    assert FAKE_STREAM_NAME not in "\n".join(client.detail)
    assert FAKE_STREAM_NAME not in json.dumps([list(map(str, e)) for e in events], ensure_ascii=False)
    # 로컬 예약 기록: Cloud READY, read-back 확인
    (rec,) = store().all()
    assert rec.execution == "cloud" and rec.cloud_state == "READY" and rec.cloud_job_id == spec["job_id"]
    assert rec.cloud_label.startswith("✓") and FAKE_STREAM_NAME not in json.dumps(load_settings(), ensure_ascii=False)
    listing = client.list_jobs()
    assert listing[0]["state"] == "PENDING" and listing[0]["media_count"] == 2 and listing[0]["manifest_ok"]


def test_existing_pc_live_defaults_unchanged(api, fake):
    api.insert_broadcast(title="수동 LIVE")
    occ = occurrences(rule(), NOW)[0]
    create_reservation(api, tpl().render(local_start=occ.local_start, session=1), occ)
    top_up(api, store(), rule_id="yt", rule=rule(mode="DAILY"), template=tpl(), now=NOW)
    assert len(fake.broadcasts) == 9
    for b in fake.broadcasts.values():
        assert b["body"]["contentDetails"]["enableAutoStart"] is False
        assert b["body"]["contentDetails"]["enableAutoStop"] is False


def test_cloud_unreachable_creates_no_youtube_reservation(api, client, remote, fake, media):
    remote.reachable = False
    out = run(api, client, media)
    assert out.failed_step == "cloud" and not out.all_ready and out.made == 0
    assert fake.broadcasts == {} and store().all() == []


def test_not_live_ready_or_old_worker_stops_before_upload(api, client, remote, fake, media):
    paths, reports = media
    bad = [reports[0], report(paths[1], keyframe=5.0)]
    out = run(api, client, (paths, bad))
    assert out.failed_step == "ready" and "LIVE READY" in out.errors[0] and remote.uploads() == 0
    mixed = [reports[0], report(paths[1], sample_rate=48000)]
    assert run(api, client, (paths, mixed)).failed_step == "compat"
    remote.worker_version = 2
    out = run(api, client, media)
    assert out.failed_step == "cloud" and "업데이트" in out.errors[0] and remote.uploads() == 0
    assert fake.broadcasts == {}


def test_partial_failure_keeps_youtube_and_retry_makes_ready(api, client, remote, fake, media):
    remote.fail_add = True
    out = run(api, client, media)
    assert out.made == 1 and out.ready == 0 and not out.all_ready and out.failed_step == "job"
    assert len(fake.broadcasts) == 1  # 이미 만든 YouTube 예약은 자동 삭제하지 않음
    (rec,) = store().all()
    assert rec.cloud_state == "PARTIAL" and "서버 저장 실패" in rec.cloud_error and rec.cloud_label.startswith("⚠")
    assert out.partial == [rec] or out.partial[0].broadcast_id == rec.broadcast_id
    remote.fail_add = False
    retry_cloud_job(rec, api=api, client=client, store=store(), now=NOW)
    (rec,) = store().all()
    assert rec.cloud_state == "READY" and rec.cloud_job_id and len(list(remote.jobs_dir.glob("*.json"))) == 1


def test_repeat_rule_one_job_per_occurrence_and_media_reused(api, client, remote, fake, media):
    saved = {}
    out = run(api, client, media, r=rule(mode="DAILY"), on_playlist=lambda pl: saved.setdefault("pl", pl))
    assert out.made == 7 and out.ready == 7 and out.all_ready  # 7일 rolling / 최대 7개
    assert len(list(remote.jobs_dir.glob("*.json"))) == 7 and remote.uploads() == 2
    assert all(r.cloud_state == "READY" for r in store().all())
    # 부족분 보충: 새 회차 없음 → 업로드도 없음 (SHA256 확인 후 재사용)
    before = remote.uploads()
    out2 = run(api, client, media, r=rule(mode="DAILY"), saved_playlist=saved["pl"])
    assert out2.made == 0 and not out2.failed_step and remote.uploads() == before and out2.reused == 2
    # 하나를 목록에서만 지운 뒤 보충 → Cloud에는 같은 채널·같은 시각 예약이 아직 있음 → 겹침 차단 (v4)
    # YouTube 예약을 만들기 전에 막으므로 유령 YouTube 예약/중복 Cloud job이 생기지 않는다
    first = store().all()[0]
    store().remove(first.broadcast_id)
    n_broadcasts = len(fake.broadcasts)
    out3 = run(api, client, media, r=rule(mode="DAILY"), saved_playlist=saved["pl"])
    assert out3.made == 0 and out3.failed_step == "youtube" and "겹치는" in out3.errors[0]
    assert len(fake.broadcasts) == n_broadcasts and len(list(remote.jobs_dir.glob("*.json"))) == 7
    # Cloud 예약을 취소한 뒤 보충 → YouTube 예약과 Cloud job이 함께 생김 (영상은 다시 보내지 않음)
    remote.store().request_cancel(first.cloud_job_id)
    out4 = run(api, client, media, r=rule(mode="DAILY"), saved_playlist=saved["pl"])
    assert out4.made == 1 and out4.ready == 1 and remote.uploads() == before
    assert len(list(remote.jobs_dir.glob("*.json"))) == 8


def test_cancel_cloud_only_then_with_youtube(api, client, remote, fake, media):
    run(api, client, media, r=rule(mode="DAILY"))
    recs = store().all()
    lines = cancel_reservation(recs[0], store=store(), client=client)
    assert "Cloud 자동 시작을 취소" in lines[0] and remote.store().cancel_requested(recs[0].cloud_job_id)
    assert recs[0].broadcast_id in fake.broadcasts  # YouTube 예약은 그대로
    assert store().all()[0].cloud_state == "CANCELLED"
    lines = cancel_reservation(recs[1], store=store(), client=client, api=api, delete_youtube=True)
    assert recs[1].broadcast_id not in fake.broadcasts and all(r.broadcast_id != recs[1].broadcast_id for r in store().all())
    assert remote.store().cancel_requested(recs[1].cloud_job_id) and "YouTube 예약도 삭제" in lines[-1]


def test_sync_reads_cloud_states_back(api, client, remote, media):
    run(api, client, media, r=rule(mode="DAILY"))
    recs = store().all()
    st = remote.store()
    st.write_state(recs[0].cloud_job_id, {"state": "COMPLETE"})
    st.write_state(recs[1].cloud_job_id, {"state": "MISSED", "message": "예약 시각이 5분 넘게 지나"})
    assert sync_cloud_states(store(), client) == 2
    by = {r.broadcast_id: r for r in store().all()}
    assert by[recs[0].broadcast_id].cloud_label == "완료" and "5분" in by[recs[1].broadcast_id].cloud_error
    later = datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc)
    assert len(pending_cloud_reservations(store(), later)) == 5


def test_readback_and_job_helpers():
    occ = occurrences(rule(), NOW)[0]
    job = build_job(job_id="sj_1", broadcast_id="b1", stream_id="s1", start_utc=occ.start_utc, end_utc=occ.end_utc,
                    playlist=[{"name": "a.mp4", "sha256": "0" * 64, "size": 10, "local": "D:\\x\\a.mp4"}],
                    ingest_url="rtmps://a.rtmps.youtube.com/live2", key_fp="0123456789abcdef", now=NOW)
    assert "local" not in json.dumps(job) and w.parse_job(job)["start"] == occ.start_utc.timestamp()
    good = {"job_id": "sj_1", "scheduled_at_utc": "2026-10-10T10:00:00Z", "media_count": 1, "media_ok": True,
            "manifest_ok": True, "state": "PENDING"}
    assert verify_readback([good], {"sj_1": ("2026-10-10T10:00:00Z", 1)}) == {}
    assert "시각" in verify_readback([{**good, "scheduled_at_utc": "2026-10-10T10:01:00Z"}],
                                   {"sj_1": ("2026-10-10T10:00:00Z", 1)})["sj_1"]
    assert "PENDING" in verify_readback([{**good, "state": "LIVE"}], {"sj_1": ("2026-10-10T10:00:00Z", 1)})["sj_1"]
    assert "찾을 수 없" in verify_readback([], {"sj_1": ("2026-10-10T10:00:00Z", 1)})["sj_1"]
    assert duration_label(710) == "11시간 50분" and duration_label(120) == "2시간" and duration_label(30) == "30분"
    assert dict(DURATION_PRESETS)["11시간 50분"] == 710 and key_fingerprint(" k ") == w.key_fingerprint("k")


def test_timezone_asia_seoul_to_utc():
    occ = occurrences(rule(), NOW)[0]
    assert occ.start_utc == datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)
    assert occ.local_start.strftime("%Y-%m-%d %H:%M %z") == "2026-10-10 19:00 +0900"
    assert (occ.end_utc - occ.start_utc).total_seconds() == 7200


def test_manual_cloud_live_with_other_key_blocks_key_change(api, client, remote, fake, media):
    remote.key_file.write_text("other-key-0000\n", encoding="utf-8")
    remote.active = True
    remote.config = json.dumps({"media": "x.mp4"})
    remote.state = "RUNNING"
    out = run(api, client, media)
    assert out.failed_step == "stream" and "다른 Stream Key" in out.errors[0] and fake.broadcasts == {}
    assert remote.key_file.read_text(encoding="utf-8") == "other-key-0000\n"
