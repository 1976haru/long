"""다채널 예약 업로드 대기열 — 한 번에 1개씩 순차 업로드, settings.json에 저장해 재실행 시 복구.

각 작업 시작 전 순서 (한국/일본 채널이 섞여 있어도 작업마다 처음부터 다시 확인):
  profile load → 프로필 전용 OAuth load → channels.list(mine=true)로 채널 ID 확인(다르면 차단)
  → resumable upload(이어 올리기) → 썸네일 → publishAt 확인

상태 머신 (TRANSITIONS 밖의 전이는 InvalidTransition):
  PENDING → VERIFYING_CHANNEL → CREATING_SESSION → UPLOADING → PROCESSING → APPLYING_THUMBNAIL → VERIFYING_SCHEDULE
          → COMPLETE | PARTIAL(썸네일 실패) | API_REVIEW_REQUIRED(API 프로젝트 제한)
  진행 중 어느 단계에서든 → PAUSED(중지, 이어 올리기 가능) | FAILED | BLOCKED(채널 불일치)
  COMPLETE·CANCELLED는 끝 상태 (다시 올리지 않음). CANCELLED는 자동으로 다시 시작하지 않는다.

- 업로드 세션 URL은 비밀로 취급: Windows DPAPI로 암호화한 값만 저장 (Windows 외: 메모리만 → 재실행 시 새 세션).
  화면/로그/오류 메시지/repr에 넣지 않는다.
- video_id는 업로드가 끝나는 즉시 저장 → 재시도/재실행 때 videos.insert를 다시 하지 않는다.
- 앱이 업로드 도중 꺼졌으면 다음 실행 때 PENDING으로 복구하고, 저장된 세션에 받은 위치를 물어 이어 올린다
  (서버가 이미 다 받았으면 그 응답의 video id를 쓴다 → 중복 업로드 없음).
- Tk를 참조하지 않는다. UI는 events 큐로만 상태를 받는다.
"""
from __future__ import annotations

import base64
import os
import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .settings import load_settings, update_settings
from .youtube_accounts import ChannelMismatchError, ChannelProfile, ProfileStore, build_profile_api, verify_channel
from .youtube_api import YouTubeApiClient, YouTubeApiError
from .youtube_metadata import BroadcastMetadata, MetadataError, parse_tags, validate_thumbnail
from .youtube_oauth import OAuthError
from .youtube_upload import (
    ApiRestrictedError, ResumableUploader, SessionExpired, UploadCancelled, UploadProgress, build_video_body,
    file_signature, parse_utc, signature_matches, utc_iso, validate_publish_at, validate_video_file, verify_publish_at,
)
from .youtube_usage import record_api_calls

SETTINGS_KEY = "upload_queue"
MAX_JOBS = 100  # 요구: 최소 50 (UI도 같은 값 사용)

PENDING = "PENDING"
VERIFYING_CHANNEL = "VERIFYING_CHANNEL"
CREATING_SESSION = "CREATING_SESSION"
UPLOADING = "UPLOADING"
PROCESSING = "PROCESSING"
APPLYING_THUMBNAIL = "APPLYING_THUMBNAIL"
VERIFYING_SCHEDULE = "VERIFYING_SCHEDULE"
COMPLETE = "COMPLETE"
PARTIAL = "PARTIAL"
PAUSED = "PAUSED"
CANCELLED = "CANCELLED"
FAILED = "FAILED"
BLOCKED = "BLOCKED"
API_REVIEW_REQUIRED = "API_REVIEW_REQUIRED"

ACTIVE_STATES = (VERIFYING_CHANNEL, CREATING_SESSION, UPLOADING, PROCESSING, APPLYING_THUMBNAIL, VERIFYING_SCHEDULE)
RETRYABLE_STATES = (PAUSED, FAILED, BLOCKED, PARTIAL, API_REVIEW_REQUIRED)
STATE_LABELS = {
    PENDING: "대기", VERIFYING_CHANNEL: "채널 확인 중", CREATING_SESSION: "업로드 준비", UPLOADING: "업로드 중",
    PROCESSING: "처리 중", APPLYING_THUMBNAIL: "썸네일 적용", VERIFYING_SCHEDULE: "예약 확인", COMPLETE: "예약 완료",
    PARTIAL: "일부 실패 (썸네일)", PAUSED: "일시 중지", CANCELLED: "취소됨", FAILED: "실패", BLOCKED: "차단 (채널 불일치)",
    API_REVIEW_REQUIRED: "확인 필요 (API 제한)",
}
_INTERRUPT = (PAUSED, FAILED, BLOCKED)
TRANSITIONS: dict[str, tuple[str, ...]] = {
    PENDING: (VERIFYING_CHANNEL, CANCELLED),
    VERIFYING_CHANNEL: (CREATING_SESSION, UPLOADING, APPLYING_THUMBNAIL, VERIFYING_SCHEDULE) + _INTERRUPT,
    CREATING_SESSION: (UPLOADING,) + _INTERRUPT,
    UPLOADING: (CREATING_SESSION, PROCESSING) + _INTERRUPT,  # 세션 만료 → 새 세션
    PROCESSING: (APPLYING_THUMBNAIL, VERIFYING_SCHEDULE) + _INTERRUPT,
    APPLYING_THUMBNAIL: (VERIFYING_SCHEDULE,) + _INTERRUPT,
    VERIFYING_SCHEDULE: (COMPLETE, PARTIAL, API_REVIEW_REQUIRED) + _INTERRUPT,
    COMPLETE: (),
    CANCELLED: (),
    PARTIAL: (PENDING, CANCELLED),
    PAUSED: (PENDING, CANCELLED),
    FAILED: (PENDING, CANCELLED),
    BLOCKED: (PENDING, CANCELLED),
    API_REVIEW_REQUIRED: (PENDING, CANCELLED),
}


class QueueError(ValueError):
    pass


class InvalidTransition(RuntimeError):
    pass


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, ())


# ---------------- 세션 URL 보호 ----------------

def _protect(text: str, is_windows: bool) -> str:
    if not text or not is_windows:
        return ""
    from .live_secrets import dpapi_protect
    return base64.b64encode(dpapi_protect(text.encode("utf-8"))).decode("ascii")


def _unprotect(blob: str, is_windows: bool) -> str:
    if not blob or not is_windows:
        return ""
    from .live_secrets import DpapiError, dpapi_unprotect
    try:
        return dpapi_unprotect(base64.b64decode(blob)).decode("utf-8")
    except (ValueError, DpapiError, UnicodeDecodeError):
        return ""


@dataclass
class UploadJob:
    job_id: str
    profile_id: str
    channel_id: str  # 등록할 때의 프로필 채널 ID. 업로드 직전 실제 채널과 비교한다.
    profile_alias: str
    video_path: str
    title: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    thumbnail_path: str = ""
    category_id: str = "10"
    language: str = ""
    made_for_kids: bool = False
    privacy: str = "private"
    publish_at_utc: str = ""  # 비어 있으면 즉시 공개 상태(privacy)로 업로드
    timezone: str = "Asia/Seoul"
    status: str = PENDING
    progress: float = 0.0
    video_id: str = ""
    thumbnail_done: bool | None = None
    publish_verified: bool = False
    file_sig: str = ""
    session_blob: str = field(default="", repr=False)  # DPAPI(base64) 업로드 세션 URL
    error: str = ""
    created_at: float = 0.0

    def metadata(self) -> BroadcastMetadata:
        return BroadcastMetadata(title=self.title, description=self.description, tags=list(self.tags),
                                 thumbnail_path=self.thumbnail_path, category_id=self.category_id,
                                 privacy_status=self.privacy, made_for_kids=self.made_for_kids,
                                 default_language=self.language)

    @property
    def publish_at(self) -> datetime | None:
        return parse_utc(self.publish_at_utc) if self.publish_at_utc else None

    @property
    def status_label(self) -> str:
        return STATE_LABELS.get(self.status, self.status)

    def publish_local_text(self) -> str:
        if not self.publish_at_utc:
            return "즉시"
        from .youtube_schedule import get_zone
        return self.publish_at.astimezone(get_zone(self.timezone)).strftime("%Y-%m-%d %H:%M ") + self.timezone

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "UploadJob":
        known = set(cls.__dataclass_fields__)
        j = cls(**{k: v for k, v in (d or {}).items() if k in known})
        if j.status in ACTIVE_STATES:  # 진행 중에 앱이 꺼졌다 → 다음 시작 때 이어 올리기/확인
            j.status = PENDING
        elif j.status not in TRANSITIONS:
            j.status = FAILED
            j.error = j.error or "알 수 없는 상태였습니다. [다시 시도]하세요."
        return j


def queue_counts(data: dict | None = None) -> dict:
    """대시보드용: 대기/완료 개수 (settings.json만 읽음)."""
    rows = (data if data is not None else load_settings()).get(SETTINGS_KEY) or []
    states = [r.get("status") for r in rows if isinstance(r, dict)]
    return {"waiting": sum(s == PENDING or s in ACTIVE_STATES for s in states),
            "done": sum(s == COMPLETE for s in states),
            "attention": sum(s in RETRYABLE_STATES for s in states), "total": len(states)}


def default_api_factory(profile: ChannelProfile, profiles: ProfileStore) -> YouTubeApiClient:
    """작업마다 새 API client + 새 OAuth session (앞 작업의 token/채널이 다음 작업으로 넘어가지 않음)."""
    return build_profile_api(profile, profiles.token_store(profile.profile_id))


class UploadQueue:
    def __init__(self, profiles: ProfileStore, *,
                 api_factory: Callable[[ChannelProfile, ProfileStore], YouTubeApiClient] = default_api_factory,
                 uploader_factory: Callable[..., ResumableUploader] = ResumableUploader,
                 clock: Callable[[], float] = time.time, is_windows: bool | None = None):
        self.profiles = profiles
        self.api_factory = api_factory
        self.uploader_factory = uploader_factory
        self.clock = clock
        self.is_windows = (os.name == "nt") if is_windows is None else is_windows
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._sessions: dict[str, str] = {}  # job_id → 세션 URL (메모리)
        self.jobs: list[UploadJob] = self._load()

    def __repr__(self) -> str:
        return f"UploadQueue(jobs={len(self.jobs)}, running={self.running})"

    # ---------- 저장 ----------
    def _load(self) -> list[UploadJob]:
        out = []
        for d in (load_settings().get(SETTINGS_KEY) or [])[:MAX_JOBS]:
            if isinstance(d, dict) and d.get("job_id"):
                try:
                    out.append(UploadJob.from_dict(d))
                except TypeError:
                    continue
        return out

    def save(self) -> None:
        with self._lock:
            update_settings(**{SETTINGS_KEY: [j.to_dict() for j in self.jobs]})

    def snapshot(self) -> list[UploadJob]:
        with self._lock:
            return [UploadJob(**j.to_dict()) for j in self.jobs]

    def counts(self) -> dict:
        with self._lock:
            return queue_counts({SETTINGS_KEY: [j.to_dict() for j in self.jobs]})

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---------- 편집 ----------
    def make_job(self, *, profile_id: str, video_path: str, title: str, description: str = "", tags="",
                 thumbnail_path: str = "", category_id: str | None = None, language: str | None = None,
                 made_for_kids: bool | None = None, privacy: str | None = None,
                 publish_at: datetime | None = None) -> UploadJob:
        """입력 검증 후 작업 생성 (아직 대기열에 넣지 않음). 프로필 기본값을 빈 칸에 쓴다."""
        profile = self.profiles.get(profile_id)
        if profile is None:
            raise QueueError("채널을 선택하세요.")
        if not profile.channel_id:
            raise QueueError(f"'{profile.alias}' 채널이 아직 연결되지 않았습니다. [채널 관리]에서 연결하세요.")
        try:
            p = validate_video_file(video_path)
            validate_publish_at(publish_at, datetime.fromtimestamp(self.clock(), timezone.utc))
            md = BroadcastMetadata(title=title, description=description, tags=parse_tags(tags),
                                   thumbnail_path=(thumbnail_path or "").strip().strip('"'),
                                   category_id=category_id or profile.category_id,
                                   privacy_status=privacy or profile.privacy,
                                   made_for_kids=profile.made_for_kids if made_for_kids is None else made_for_kids,
                                   default_language=profile.language if language is None else language).validate("영상 제목")
        except (YouTubeApiError, MetadataError) as e:
            raise QueueError(str(e)) from None
        return UploadJob(job_id=os.urandom(6).hex(), profile_id=profile.profile_id, channel_id=profile.channel_id,
                         profile_alias=profile.alias, video_path=str(p), title=md.title, description=md.description,
                         tags=md.tags, thumbnail_path=md.thumbnail_path, category_id=str(md.category_id),
                         language=md.default_language, made_for_kids=md.made_for_kids, privacy=md.privacy_status,
                         publish_at_utc=utc_iso(publish_at) if publish_at else "", timezone=profile.timezone,
                         file_sig=file_signature(p), created_at=self.clock())

    def add(self, job: UploadJob) -> UploadJob:
        with self._lock:
            if len(self.jobs) >= MAX_JOBS:
                raise QueueError(f"예약 업로드 대기열은 최대 {MAX_JOBS}개입니다. 완료된 작업을 정리하세요.")
            if any(j.job_id == job.job_id for j in self.jobs):
                raise QueueError("이미 대기열에 있는 작업입니다.")
            self.jobs.append(job)
            self.save()
        return job

    def _find(self, job_id: str) -> UploadJob:
        j = next((x for x in self.jobs if x.job_id == job_id), None)
        if j is None:
            raise QueueError("작업을 찾을 수 없습니다.")
        return j

    def remove(self, job_id: str) -> None:
        with self._lock:
            j = self._find(job_id)
            if j.status in ACTIVE_STATES:
                raise QueueError("업로드 중인 작업은 먼저 중지하세요.")
            self.jobs.remove(j)
            self._sessions.pop(job_id, None)
            self.save()

    def move(self, job_id: str, delta: int) -> None:
        with self._lock:
            j = self._find(job_id)
            i = self.jobs.index(j)
            k = i + delta
            if 0 <= k < len(self.jobs):
                self.jobs[i], self.jobs[k] = self.jobs[k], self.jobs[i]
                self.save()

    def retry(self, job_id: str) -> None:
        """일시 중지/실패/차단/일부 실패/API 확인 필요 → 대기. 이미 끝난 단계(업로드, 썸네일)는 건너뛴다."""
        with self._lock:
            j = self._find(job_id)
            if j.status not in RETRYABLE_STATES:
                raise QueueError(f"'{j.status_label}' 상태는 다시 시도할 수 없습니다.")
            self._transition(j, PENDING, error="")

    retry_thumbnail = retry  # 썸네일만 실패(PARTIAL)한 작업: 영상은 다시 올리지 않고 썸네일+예약 확인만

    def cancel_job(self, job_id: str) -> None:
        with self._lock:
            j = self._find(job_id)
            if j.status in ACTIVE_STATES:
                raise QueueError("업로드 중인 작업은 먼저 중지하세요.")
            self._transition(j, CANCELLED)

    def clear_done(self) -> None:
        with self._lock:
            self.jobs = [j for j in self.jobs if j.status not in (COMPLETE, CANCELLED)]
            self.save()

    # ---------- 상태 ----------
    def _emit(self, job: UploadJob) -> None:
        self.events.put(("job", job.job_id, job.status, job.progress))

    def _transition(self, job: UploadJob, dst: str, **kw) -> None:
        with self._lock:
            if job.status != dst and not can_transition(job.status, dst):
                raise InvalidTransition(f"{job.status} → {dst}")
            job.status = dst
            for k, v in kw.items():
                setattr(job, k, v)
            self.save()
        self._emit(job)

    def _update(self, job: UploadJob, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(job, k, v)
            self.save()
        self._emit(job)

    def _session_url(self, job: UploadJob) -> str:
        return self._sessions.get(job.job_id) or _unprotect(job.session_blob, self.is_windows)

    def _remember_session(self, job: UploadJob, url: str) -> None:
        self._sessions[job.job_id] = url
        if job.status == CREATING_SESSION:
            self._transition(job, UPLOADING, session_blob=_protect(url, self.is_windows))
        else:
            self._update(job, session_blob=_protect(url, self.is_windows))

    # ---------- 실행 ----------
    def run_job(self, job: UploadJob) -> None:
        """작업 1개 (예외를 밖으로 내지 않고 상태로 남긴다)."""
        self._transition(job, VERIFYING_CHANNEL, error="")
        api = None
        try:
            profile = self.profiles.get(job.profile_id)
            if profile is None:
                raise ChannelMismatchError(job.channel_id, "(프로필 삭제됨)")
            if profile.channel_id != job.channel_id:
                raise ChannelMismatchError(job.channel_id, profile.channel_id or "(연결 안 됨)")
            api = self.api_factory(profile, self.profiles)
            verify_channel(api, job.channel_id)
            if not job.video_id:
                self._upload(api, job)
            warnings = []
            if job.thumbnail_path and job.thumbnail_done is not True:
                self._transition(job, APPLYING_THUMBNAIL)
                try:
                    info = validate_thumbnail(job.thumbnail_path)
                    api.set_thumbnail(job.video_id, info.path.read_bytes(), info.mime)
                    self._update(job, thumbnail_done=True)
                except (YouTubeApiError, MetadataError, OSError) as e:  # 영상은 지우지 않는다
                    self._update(job, thumbnail_done=False)
                    warnings.append(f"썸네일 실패: {e}")
            self._transition(job, VERIFYING_SCHEDULE)
            verify_publish_at(api, job.video_id, publish_at=job.publish_at, privacy=job.privacy,
                              made_for_kids=job.made_for_kids)
            self._transition(job, PARTIAL if warnings else COMPLETE, publish_verified=True, progress=1.0,
                             error="; ".join(warnings))
        except ChannelMismatchError as e:
            self._transition(job, BLOCKED, error=str(e))
        except ApiRestrictedError as e:
            self._transition(job, API_REVIEW_REQUIRED, error=str(e))
        except UploadCancelled:
            self._transition(job, PAUSED, error="중지했습니다. [다시 시도]하면 이어서 올립니다.")
        except (YouTubeApiError, OAuthError) as e:
            self._transition(job, FAILED, error=str(e))
        except Exception as e:  # 예상 밖 오류도 다음 작업은 계속 (메시지에 내부 값/URL을 넣지 않음)
            self._transition(job, FAILED, error=f"업로드 오류 ({type(e).__name__})")
        finally:
            if api is not None:
                record_api_calls(getattr(api, "calls", []), self.clock)  # 이 프로그램 기준 사용량 (참고용)

    def _upload(self, api: YouTubeApiClient, job: UploadJob) -> None:
        validate_video_file(job.video_path)
        session = self._session_url(job)
        if not signature_matches(job.video_path, job.file_sig):
            raise YouTubeApiError("예약 등록 후 영상 파일이 변경되었습니다. 작업을 지우고 다시 등록하세요.",
                                  kind="config", reason="fileChanged")
        validate_publish_at(job.publish_at, datetime.fromtimestamp(self.clock(), timezone.utc))
        body = build_video_body(job.metadata(), job.publish_at)
        uploader = self.uploader_factory(api, cancel=self.cancel)
        self._transition(job, UPLOADING if session else CREATING_SESSION)

        def progress(p: UploadProgress):
            job.progress = round(p.fraction * 0.95, 4)
            self._emit(job)
        try:
            res = uploader.upload(job.video_path, body, session_url=session,
                                  on_session=lambda u: self._remember_session(job, u), on_progress=progress)
        except SessionExpired:
            self._sessions.pop(job.job_id, None)
            self._transition(job, CREATING_SESSION, session_blob="")
            res = uploader.upload(job.video_path, body, on_session=lambda u: self._remember_session(job, u),
                                  on_progress=progress)
        self._sessions.pop(job.job_id, None)
        # video_id를 바로 저장 → 이후 실패/재실행에서도 videos.insert를 다시 하지 않는다
        self._transition(job, PROCESSING, video_id=str(res["id"]), session_blob="", progress=0.95)

    def run_pending(self) -> None:
        """대기 작업을 순서대로 하나씩. 실패해도 다음 작업 계속, 중지하면 멈춤. CANCELLED는 건드리지 않는다."""
        while not self.cancel.is_set():
            with self._lock:
                job = next((j for j in self.jobs if j.status == PENDING), None)
            if job is None:
                break
            self.run_job(job)
        self.events.put(("finish", "", "", 0.0))

    def start(self) -> bool:
        if self.running:
            return False
        self.cancel.clear()
        self._thread = threading.Thread(target=self.run_pending, name="youtube-upload", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self.cancel.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)
