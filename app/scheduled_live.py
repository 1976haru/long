"""TRUE 예약 LIVE (Cloud 자동 송출) — Tk와 무관한 준비 Pipeline.

오늘 설정 → PC 종료 → 예약 시각에 Cloud worker(long-live-scheduler.service)가 Playlist를 DIRECT COPY로 송출.
YouTube 방송은 enableAutoStart/enableAutoStop=True로 만들어 송출 시작/종료에 맞춰 YouTube가 직접 LIVE/종료한다
→ Cloud에 Google OAuth token을 두지 않는다.

준비 순서 (Cloud 준비가 성공한 뒤에만 YouTube 예약을 만든다 → 유령 예약 최소화):
 1 영상 검사 · 2 LIVE READY · 3 Playlist 호환 · 4 Cloud 연결/Worker v3 · 5 저장 공간 · 6 전송(SHA256 skip)
 7 SHA256 검증 · 8 manifest · 9 송출 스트림 확인 + Cloud Key/scheduler · 10 YouTube 예약(autoStart/Stop) + bind
 11 Cloud job 저장 · 12 Cloud에서 다시 읽어 확인 (job_id/시각/영상 수/manifest/PENDING)

Cloud에는 로컬 경로(D:\\...)를 저장하지 않는다: 서버 파일 이름 + SHA256 + 크기만.
Stream Key는 기존 방식(서버 /etc/long-live/stream.key 0600, ssh stdin)만 쓰고 job/로그/화면에는 지문도 표시하지 않는다.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from .live_playlist import entry_durations, validate_playlist
from .live_profile import YOUTUBE_RTMPS_INGEST, redact
from .youtube_metadata import MetadataTemplate
from .youtube_schedule import (
    ReservationRecord, ReservationStore, ScheduleOccurrence, ScheduleRule, create_reservation, plan_top_up,
)

EXECUTION_CLOUD = "cloud"
EXECUTION_YOUTUBE = "youtube"
LATE_GRACE_SECONDS = 300
DURATION_PRESETS = (("30분", 30), ("1시간", 60), ("2시간", 120), ("6시간", 360), ("11시간 50분", 710))
FIRST_TEST_DURATION = 120  # 첫 REAL 테스트 권장 (1:30 Playlist가 처음으로 돌아가는 것까지 확인)
DELAY_NOTICE = "예약 시각에 송출을 시작하며 YouTube LIVE 표시까지 약간의 지연이 있을 수 있습니다."
STEPS = (
    ("analyze", "영상 검사"), ("ready", "LIVE READY"), ("compat", "Playlist 호환"), ("cloud", "Cloud 연결"),
    ("disk", "Cloud 저장 공간"), ("upload", "영상 Cloud 전송"), ("verify", "파일 검증 (SHA256)"),
    ("manifest", "Playlist 정보 만들기"), ("stream", "송출 스트림 · 자동 시작 켜기"), ("youtube", "YouTube 예약"),
    ("job", "Cloud 자동 시작 등록"), ("readback", "최종 확인"),
)
STEP_LABELS = dict(STEPS)


class ScheduledLiveError(RuntimeError):
    """사용자에게 보여줄 한글 메시지 (secret 없음)."""

    def __init__(self, message: str, step: str = ""):
        super().__init__(message)
        self.step = step


def key_fingerprint(key: str) -> str:
    """cloud/long_live_worker.py key_fingerprint와 같은 계산 (SHA256 앞 16자리)."""
    return hashlib.sha256((key or "").strip().encode("utf-8")).hexdigest()[:16]


def duration_label(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return " ".join(x for x in (f"{h}시간" if h else "", f"{m}분" if m else "") if x) or "0분"


def new_job_id() -> str:
    return "sj_" + secrets.token_hex(8)


def utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value: str) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def build_job(*, job_id: str, broadcast_id: str, stream_id: str, start_utc: datetime, end_utc: datetime,
              playlist: Sequence[dict], ingest_url: str, key_fp: str, now: datetime,
              grace_seconds: int = LATE_GRACE_SECONDS) -> dict:
    """서버 worker parse_job()이 받는 예약 job (Stream Key 없음, 로컬 경로 없음)."""
    return {
        "schema": 1, "job_id": job_id, "broadcast_id": broadcast_id, "stream_id": stream_id,
        "scheduled_at_utc": utc_z(start_utc), "stop_at_utc": utc_z(end_utc),
        "playlist": [{"name": p["name"], "sha256": p["sha256"], "size": int(p["size"])} for p in playlist],
        "ingest_url": ingest_url, "ingest_mode": "copy", "key_fingerprint": key_fp,
        "grace_seconds": int(grace_seconds), "created_at": utc_z(now),
    }


def verify_readback(listing: Sequence[dict], expected: dict[str, tuple[str, int]]) -> dict[str, str]:
    """Cloud에서 다시 읽은 job 목록 확인. expected: job_id → (scheduled_at_utc, 영상 수). 문제 job_id → 이유."""
    by_id = {j.get("job_id"): j for j in listing if isinstance(j, dict)}
    problems = {}
    for jid, (when, count) in expected.items():
        j = by_id.get(jid)
        if j is None:
            problems[jid] = "Cloud에서 예약 작업을 찾을 수 없습니다."
        elif _epoch(j.get("scheduled_at_utc")) != _epoch(when):
            problems[jid] = "Cloud 예약 시각이 다릅니다."
        elif int(j.get("media_count") or 0) != count or not j.get("media_ok"):
            problems[jid] = "Cloud 영상 정보가 맞지 않습니다."
        elif not j.get("manifest_ok"):
            problems[jid] = "Cloud Playlist 정보(manifest)가 없습니다."
        elif j.get("state") != "PENDING" or j.get("cancel_requested"):
            problems[jid] = f"Cloud 예약 상태가 대기(PENDING)가 아닙니다 ({j.get('state')})."
    return problems


def media_problem(paths: Sequence[Path], reports: Sequence) -> tuple[str, str]:
    """(step, 메시지) — 영상 검사 → LIVE READY → Playlist 호환. 문제 없으면 ("", "")."""
    if not paths:
        return "analyze", "방송할 영상이 없습니다. LIVE Playlist에 영상을 넣거나 [영상 선택]을 누르세요."
    if len(reports) != len(paths) or any(r is None for r in reports):
        return "analyze", "영상 분석 중입니다. 잠시 후 다시 시도하세요."
    for n, (p, r) in enumerate(zip(paths, reports), 1):
        if not Path(p).is_file():
            return "analyze", f"{n}번 영상 파일을 찾을 수 없습니다: {Path(p).name}"
        blocking = [i.message for i in r.issues if i.blocking]
        if blocking:
            return "ready", (f"{n}번 영상이 LIVE READY가 아닙니다: {blocking[0]}\n"
                             "→ LIVE 창의 [문제 영상 모두 LIVE READY로 만들기]로 먼저 변환하세요.")
    v = validate_playlist(list(reports))
    if not v.ok:
        return "compat", v.first_error + "\n→ LIVE 창의 [문제 영상 모두 LIVE READY로 만들기]로 맞출 수 있습니다."
    return "", ""


@dataclass
class CloudScheduleOutcome:
    made: int = 0  # 만든 YouTube 예약 수
    ready: int = 0  # Cloud 자동 시작까지 확인된 수
    records: list[ReservationRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    failed_step: str = ""
    scheduler_ok: bool = False
    uploaded: int = 0
    reused: int = 0
    first_start_utc: str = ""
    duration_minutes: int = 0
    media_count: int = 0
    privacy: str = ""

    @property
    def partial(self) -> list[ReservationRecord]:
        return [r for r in self.records if r.cloud_state != "READY"]

    @property
    def all_ready(self) -> bool:
        """'PC를 꺼도 됩니다'는 모든 예약이 Cloud에서 다시 읽어 확인되고 scheduler가 켜져 있을 때만."""
        return self.made > 0 and self.ready == self.made and self.scheduler_ok and not self.failed_step


def _emit_noop(*_a):
    pass


def reuse_uploads(client, paths: Sequence[Path], saved: Sequence[dict]) -> list[dict] | None:
    """같은 Playlist 반복 예약: 로컬 파일(크기/수정시각)이 그대로고 서버 SHA256이 같으면 다시 해시/업로드하지 않는다."""
    if not saved or len(saved) != len(paths):
        return None
    out = []
    for p, s in zip(paths, saved):
        try:
            st = Path(p).stat()
        except OSError:
            return None
        if st.st_size != s.get("size") or st.st_mtime_ns != s.get("mtime_ns") or str(Path(p)) != s.get("local"):
            return None
        if client.remote_sha256(s["name"]) != s.get("sha256"):
            return None
        out.append(dict(s))
    return out


def prepare_media(client, paths: Sequence[Path], reports: Sequence, *, saved: Sequence[dict] | None = None,
                  emit: Callable = _emit_noop, cancel=None) -> tuple[list[dict], int, int]:
    """Cloud 전송 (기존 upload_many: SHA256 skip, .part 업로드, 서버 SHA256 검증) → (playlist, 새로 보냄, 생략)."""
    paths = [Path(p) for p in paths]
    durations = entry_durations(reports)
    reused = reuse_uploads(client, paths, saved or [])
    if reused is not None:
        emit("step", "disk", "ok", "같은 영상이 이미 Cloud에 있습니다")
        emit("step", "upload", "ok", f"이미 Cloud에 있음 {len(paths)}개 (다시 보내지 않음)")
        return reused, 0, len(paths)
    free = client._remote_free_bytes()
    emit("step", "disk", "ok", f"남은 공간 {free / 1024**3:.1f} GB" if free else "확인 완료")
    emit("step", "upload", "run", "")
    n = len(paths)

    def cb(i, n_, f, t):
        emit("progress", (i - 1 + f) / max(n_, 1), f"영상 {i} / {n_} 전송 · {t}")
    if n > 1:
        ups = client.upload_many(paths, progress_cb=cb, cancel=cancel)
    else:
        ups = [client.upload_media(paths[0], cancel=cancel, progress_cb=lambda f, t: cb(1, 1, f, t))]
    playlist = []
    for p, u, d in zip(paths, ups, durations):
        st = p.stat()
        playlist.append({"name": u.remote_name, "sha256": u.sha256, "size": st.st_size, "duration": round(d, 3),
                         "local": str(p), "mtime_ns": st.st_mtime_ns})
    skipped = sum(1 for u in ups if u.skipped)
    emit("step", "upload", "ok", f"새로 보냄 {n - skipped}개 · 이미 있음 {skipped}개")
    return playlist, n - skipped, skipped


def cloud_playlist_for_job(playlist: Sequence[dict]) -> list[dict]:
    """Cloud/예약 기록에 남길 Playlist (로컬 경로 제외)."""
    return [{k: p[k] for k in ("name", "sha256", "size", "duration") if k in p} for p in playlist]


def prepare_cloud_schedule(*, api, client, media_paths: Sequence[Path], reports: Sequence, rule: ScheduleRule,
                           template: MetadataTemplate, rule_id: str, store: ReservationStore, now: datetime,
                           saved_stream_id: str | None = None, channel: str = "", emit: Callable = _emit_noop,
                           cancel=None, saved_playlist: Sequence[dict] | None = None, start_session: int = 1,
                           thumb_counter: int = 0, on_stream: Callable[[str], None] | None = None,
                           on_playlist: Callable[[list], None] | None = None) -> CloudScheduleOutcome:
    """예약 준비 Pipeline. 예외 대신 outcome.failed_step/errors로 결과를 돌려준다 (부분 성공 보존)."""
    out = CloudScheduleOutcome(duration_minutes=int(rule.duration_minutes), media_count=len(media_paths),
                               privacy=template.privacy_status)
    secrets_: list[str] = []

    def fail(step: str, msg: str) -> CloudScheduleOutcome:
        msg = redact(str(msg), secrets_)
        emit("step", step, "fail", msg)
        out.failed_step = step
        out.errors.append(msg)
        return out

    # 1~3 영상 검사 / LIVE READY / Playlist 호환
    emit("step", "analyze", "run", "")
    step, msg = media_problem(media_paths, reports)
    for s in ("analyze", "ready", "compat"):
        if s == step:
            return fail(s, msg)
        emit("step", s, "ok", "")
    # 4 Cloud 연결 + worker v3 (예약 scheduler 포함)
    emit("step", "cloud", "run", "")
    try:
        client.check_connection()
        client.require_scheduler_worker()
    except Exception as e:  # CloudError 등
        return fail("cloud", e)
    emit("step", "cloud", "ok", "")
    # 5~7 저장 공간 / 전송 / SHA256 검증
    try:
        playlist, out.uploaded, out.reused = prepare_media(client, media_paths, reports, saved=saved_playlist,
                                                           emit=emit, cancel=cancel)
    except Exception as e:
        return fail("upload", e)
    if on_playlist:
        on_playlist(playlist)
    emit("step", "verify", "ok", "Cloud 파일 SHA256 = 원본")
    cloud_pl = cloud_playlist_for_job(playlist)
    emit("step", "manifest", "ok", f"{len(cloud_pl)}개 순서대로 반복")
    # 9 송출 스트림 (YouTube 재사용 stream) → Cloud Key 맞추기 + scheduler 켜기 (아직 YouTube 예약 없음)
    emit("step", "stream", "run", "")
    try:
        stream = api.ensure_reusable_stream(saved_stream_id)
        key = (stream.stream_name or "").strip()
        if not key:
            return fail("stream", "YouTube 송출 스트림 정보를 읽을 수 없습니다.")
        secrets_.append(key)
        ingest = stream.rtmps_ingestion_address or YOUTUBE_RTMPS_INGEST
        if on_stream:
            on_stream(stream.id)
        client.ensure_stream_key(key, ingest)
        client.ensure_scheduler()
        out.scheduler_ok = True
    except Exception as e:
        return fail("stream", e)
    emit("step", "stream", "ok", "")
    key_fp = key_fingerprint(key)
    # 10~11 YouTube 예약 (회차마다) + Cloud job
    existing = store.future_keys(rule_id, now)
    occs: list[ScheduleOccurrence] = plan_top_up(rule, existing, now)
    if not occs:
        emit("step", "youtube", "ok", "새로 만들 회차가 없습니다")
        emit("step", "job", "ok", "")
        emit("step", "readback", "ok", "")
        return out
    emit("step", "youtube", "run", "")
    expected: dict[str, tuple[str, int]] = {}
    for i, occ in enumerate(occs):
        session = start_session + len(existing) + i
        md = template.render(local_start=occ.local_start, session=session, channel=channel,
                             thumb_counter=thumb_counter + len(existing) + i)
        res = create_reservation(api, md, occ, stream_id=stream.id, auto_start_stop=True)
        if not res.broadcast_ok:
            fail("youtube", res.errors.get("broadcast", "YouTube 예약을 만들지 못했습니다."))
            break
        out.made += 1
        out.first_start_utc = out.first_start_utc or occ.start_utc.isoformat()
        rec = ReservationRecord(
            broadcast_id=res.broadcast_id, title=md.title, start_utc=occ.start_utc.isoformat(),
            end_utc=occ.end_utc.isoformat(), privacy=md.privacy_status, thumbnail_name=res.thumbnail_name,
            rule_id=rule_id, session=session, metadata_ok=res.metadata_ok, thumbnail_ok=res.thumbnail_ok,
            execution=EXECUTION_CLOUD, stream_id=stream.id, ingest_url=ingest, cloud_playlist=cloud_pl,
            cloud_state="PARTIAL")
        if not res.bind_ok:
            rec.cloud_error = "송출 스트림 연결 실패: " + res.errors.get("bind", "")
        else:
            job_id = new_job_id()
            job = build_job(job_id=job_id, broadcast_id=res.broadcast_id, stream_id=stream.id, start_utc=occ.start_utc,
                            end_utc=occ.end_utc, playlist=cloud_pl, ingest_url=ingest, key_fp=key_fp, now=now)
            try:
                client.add_job(job)
                rec.cloud_job_id = job_id
                expected[job_id] = (job["scheduled_at_utc"], len(cloud_pl))
            except Exception as e:
                rec.cloud_error = redact(str(e), secrets_)
        store.upsert(rec)
        out.records.append(rec)
    if not out.made:
        return out
    if not out.failed_step:
        emit("step", "youtube", "ok", f"{out.made}개")
    if not expected:
        emit("step", "job", "fail", out.records[0].cloud_error if out.records else "")
        out.failed_step = out.failed_step or "job"
        return out
    emit("step", "job", "ok" if len(expected) == out.made else "fail", f"{len(expected)}개")
    # 12 read-back
    emit("step", "readback", "run", "")
    try:
        listing = client.list_jobs()
        out.scheduler_ok = client.scheduler_active()
    except Exception as e:
        for r in out.records:
            if r.cloud_job_id:
                r.cloud_error = "Cloud에서 다시 확인하지 못했습니다: " + redact(str(e), secrets_)
                store.upsert(r)
        return fail("readback", e)
    problems = verify_readback(listing, expected)
    for r in out.records:
        if r.cloud_job_id and r.cloud_job_id not in problems:
            r.cloud_state, r.cloud_error = "READY", ""
            out.ready += 1
        elif r.cloud_job_id:
            r.cloud_error = problems[r.cloud_job_id]
        store.upsert(r)
    if out.ready != out.made or not out.scheduler_ok:
        emit("step", "readback", "fail", (out.partial[0].cloud_error if out.partial else "Cloud 자동 시작이 꺼져 있습니다."))
        out.failed_step = out.failed_step or "readback"
    else:
        emit("step", "readback", "ok", "")
    return out


def retry_cloud_job(rec: ReservationRecord, *, api, client, store: ReservationStore, now: datetime) -> ReservationRecord:
    """⚠ YouTube 예약은 있는데 Cloud 자동 시작 준비가 실패한 회차 → [Cloud 준비 다시 시도]."""
    if rec.execution != EXECUTION_CLOUD or not rec.cloud_playlist:
        raise ScheduledLiveError("Cloud 예약 정보가 없어 다시 시도할 수 없습니다.")
    start = datetime.fromisoformat(rec.start_utc)
    end = datetime.fromisoformat(rec.end_utc) if rec.end_utc else None
    if start <= now or end is None:
        raise ScheduledLiveError("예약 시각이 이미 지났습니다. 새 예약을 만드세요.")
    key = ""
    try:
        stream = api.ensure_reusable_stream(rec.stream_id or None)
        key = (stream.stream_name or "").strip()
        if not key:
            raise ScheduledLiveError("YouTube 송출 스트림 정보를 읽을 수 없습니다.")
        b = api.get_broadcast(rec.broadcast_id)
        if b.bound_stream_id != stream.id:
            api.bind_broadcast(rec.broadcast_id, stream.id)
        ingest = stream.rtmps_ingestion_address or rec.ingest_url or YOUTUBE_RTMPS_INGEST
        client.check_connection()
        client.require_scheduler_worker()
        client.ensure_stream_key(key, ingest)
        client.ensure_scheduler()
        job_id = rec.cloud_job_id or new_job_id()
        job = build_job(job_id=job_id, broadcast_id=rec.broadcast_id, stream_id=stream.id, start_utc=start,
                        end_utc=end, playlist=rec.cloud_playlist, ingest_url=ingest, key_fp=key_fingerprint(key), now=now)
        client.add_job(job)
        rec.cloud_job_id, rec.stream_id, rec.ingest_url = job_id, stream.id, ingest
        problems = verify_readback(client.list_jobs(), {job_id: (job["scheduled_at_utc"], len(rec.cloud_playlist))})
        if problems:
            raise ScheduledLiveError(problems[job_id])
        rec.cloud_state, rec.cloud_error = "READY", ""
    except ScheduledLiveError as e:
        rec.cloud_state, rec.cloud_error = "PARTIAL", str(e)
        store.upsert(rec)
        raise
    except Exception as e:
        rec.cloud_state, rec.cloud_error = "PARTIAL", redact(str(e), [key])
        store.upsert(rec)
        raise ScheduledLiveError(rec.cloud_error) from None
    store.upsert(rec)
    return rec


def cancel_reservation(rec: ReservationRecord, *, store: ReservationStore, client=None, api=None,
                       delete_youtube: bool = False) -> list[str]:
    """[예약 취소]: Cloud 자동 시작을 먼저 취소 (시작 전이면 CANCELLED, 송출 중이면 FFmpeg 정상 종료).
    YouTube 예약 삭제는 사용자가 [YouTube 예약도 삭제]를 고른 경우에만 (Cloud 취소가 성공한 뒤)."""
    lines = []
    if rec.execution == EXECUTION_CLOUD and rec.cloud_job_id and rec.cloud_state not in ("CANCELLED", "COMPLETE", "MISSED"):
        if client is None:
            raise ScheduledLiveError("무료 Cloud 연결이 필요합니다.")
        res = client.cancel_job(rec.cloud_job_id)
        running = res.get("state") in ("STARTING", "LIVE", "STOPPING")
        rec.cloud_state = "CANCELLED"
        rec.cloud_error = ""
        lines.append("✓ Cloud 송출을 멈추는 중입니다 (정상 종료)" if running else "✓ Cloud 자동 시작을 취소했습니다")
    if delete_youtube:
        if api is None:
            raise ScheduledLiveError("YouTube 연결이 필요합니다.")
        api.delete_broadcast(rec.broadcast_id)
        store.remove(rec.broadcast_id)
        lines.append("✓ YouTube 예약도 삭제했습니다")
    else:
        store.upsert(rec)
        if rec.execution == EXECUTION_CLOUD:
            lines.append("YouTube 예약은 그대로 남아 있습니다 (YouTube Studio에서 관리)")
    return lines


def sync_cloud_states(store: ReservationStore, client) -> int:
    """Cloud에서 job 상태를 읽어 로컬 예약 목록에 반영 (PC를 다시 켰을 때). 바뀐 수."""
    jobs = {j.get("job_id"): j for j in client.list_jobs()}
    changed = 0
    for rec in store.all():
        j = jobs.get(rec.cloud_job_id) if rec.cloud_job_id else None
        if j is None or rec.cloud_state == "PARTIAL":
            continue
        state = j.get("state") or "PENDING"
        new = "READY" if state == "PENDING" and not j.get("cancel_requested") else (
            "CANCELLED" if j.get("cancel_requested") and state in ("PENDING", "CANCELLED") else state)
        if new != rec.cloud_state:
            rec.cloud_state = new
            rec.cloud_error = str(j.get("message") or "") if new in ("FAILED", "MISSED") else ""
            store.upsert(rec)
            changed += 1
    return changed


def pending_cloud_reservations(store: ReservationStore, now: datetime) -> list[ReservationRecord]:
    """앞으로 Cloud가 자동 송출할 예약 (같은 송출 스트림으로 지금 직접 송출하면 예약 방송이 일찍 시작될 수 있음)."""
    out = []
    for r in store.all():
        if r.execution != EXECUTION_CLOUD or r.cloud_state not in ("READY", "PENDING", "PREPARING"):
            continue
        try:
            end = datetime.fromisoformat(r.end_utc or r.start_utc)
        except ValueError:
            continue
        if end > now:
            out.append(r)
    return out
