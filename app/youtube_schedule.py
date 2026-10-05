"""예약 LIVE (Phase 3B.1) — 반복 규칙, 시간대, 7일 rolling 준비, 예약 생성(부분 성공), 로컬 예약 목록.

- 반복은 YouTube 자체 반복 기능에 의존하지 않고 프로그램이 다음 회차를 계산해 Broadcast를 하나씩 만든다.
- 한 번에 앞으로 7일 / 최대 7개만 준비하고, 이후 부족분만 보충한다 (userBroadcastsExceedLimit 방지).
- 시간은 IANA 시간대(예: Asia/Seoul)의 aware datetime으로 계산해 UTC로 보낸다 (naive datetime 금지, DST 안전).
- 예약 생성 순서: insert → bind → videos.list → videos.update(태그/카테고리/언어, 기존 snippet 보존) → thumbnails.set → 최종 확인.
  뒤 단계가 실패해도 이미 만든 Broadcast는 지우지 않고 부분 결과를 돌려준다 ([썸네일 다시 올리기]/[메타데이터 다시 적용]).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .youtube_api import YouTubeApiClient, YouTubeApiError
from .youtube_metadata import BroadcastMetadata, MetadataError, MetadataTemplate, validate_thumbnail

ONCE, DAILY, WEEKDAYS, WEEKLY, CUSTOM_WEEKDAYS = "ONCE", "DAILY", "WEEKDAYS", "WEEKLY", "CUSTOM_WEEKDAYS"
MODES = (ONCE, DAILY, WEEKDAYS, WEEKLY, CUSTOM_WEEKDAYS)
MODE_LABELS = {ONCE: "한 번만", DAILY: "매일", WEEKDAYS: "평일", WEEKLY: "매주", CUSTOM_WEEKDAYS: "요일 선택"}
DAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
ROLLING_WINDOW_DAYS = 7
MAX_FUTURE_BROADCASTS = 7
DEFAULT_TZ = "Asia/Seoul"
STATUS_LABELS = {"created": "예약됨", "ready": "예약됨", "testing": "예약됨", "testStarting": "예약됨",
                 "liveStarting": "LIVE", "live": "LIVE", "complete": "완료", "revoked": "오류", "error": "오류"}


class ScheduleError(ValueError):
    pass


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError(f"시간대를 찾을 수 없습니다: {name}") from None


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ScheduleError("시간대 없는 날짜/시간은 사용할 수 없습니다.")
    return dt


def local_to_utc(day: date, at: dtime, tz: str) -> datetime:
    """현지 날짜/시각 → UTC. DST로 없는 시각(봄 앞당김)은 앞으로 밀고, 두 번 있는 시각(가을)은 첫 번째를 쓴다."""
    # fold=0: 두 번 있는 시각은 첫 번째, 없는 시각(02:30 등)은 바뀌기 전 offset으로 계산되어 앞으로(03:30) 밀린다.
    # (fold=1을 쓰면 없는 시각이 01:30으로 '뒤로' 밀린다 → 쓰지 않는다)
    zone = get_zone(tz)
    return datetime.combine(day, at, tzinfo=zone).replace(fold=0).astimezone(timezone.utc)


@dataclass
class ScheduleRule:
    mode: str = ONCE
    start_date: str = ""  # YYYY-MM-DD (현지)
    start_time_local: str = "07:00"  # HH:MM
    timezone: str = DEFAULT_TZ
    duration_minutes: int = 710  # 11:50
    custom_weekdays: list[str] = field(default_factory=list)

    def validate(self) -> "ScheduleRule":
        if self.mode not in MODES:
            raise ScheduleError("반복 방식이 올바르지 않습니다.")
        try:
            date.fromisoformat(self.start_date)
            h, m = self.start_time_local.split(":")
            dtime(int(h), int(m))
        except (ValueError, AttributeError):
            raise ScheduleError("예약 날짜/시간 형식이 올바르지 않습니다 (예: 2026-10-06, 07:00).") from None
        get_zone(self.timezone)
        if not (1 <= int(self.duration_minutes) <= 12 * 60):
            raise ScheduleError("방송 길이는 1분 ~ 12시간이어야 합니다.")
        if self.mode == CUSTOM_WEEKDAYS:
            bad = [d for d in self.custom_weekdays if d not in DAY_CODES]
            if bad or not self.custom_weekdays:
                raise ScheduleError("요일을 하나 이상 선택하세요.")
        return self

    @property
    def local_time(self) -> dtime:
        h, m = self.start_time_local.split(":")
        return dtime(int(h), int(m))

    def runs_on(self, d: date, first: date) -> bool:
        if d < first:
            return False
        if self.mode == ONCE:
            return d == first
        if self.mode == DAILY:
            return True
        if self.mode == WEEKDAYS:
            return d.weekday() < 5
        if self.mode == WEEKLY:
            return d.weekday() == first.weekday()
        return DAY_CODES[d.weekday()] in self.custom_weekdays

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ScheduleRule":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass(frozen=True)
class ScheduleOccurrence:
    start_utc: datetime
    end_utc: datetime
    local_start: datetime  # aware, 규칙 시간대

    @property
    def key(self) -> str:
        return self.start_utc.strftime("%Y-%m-%dT%H:%MZ")


def occurrences(rule: ScheduleRule, now: datetime, *, window_days: int = ROLLING_WINDOW_DAYS,
                max_count: int = MAX_FUTURE_BROADCASTS) -> list[ScheduleOccurrence]:
    """now(aware) 이후, 앞으로 window_days 안의 회차를 최대 max_count개."""
    rule.validate()
    _aware(now)
    zone = get_zone(rule.timezone)
    first = date.fromisoformat(rule.start_date)
    today_local = now.astimezone(zone).date()
    out = []
    d = max(first, today_local)
    end_day = today_local + timedelta(days=window_days)
    if rule.mode == ONCE:
        end_day = max(end_day, first)  # 한 번 예약은 7일보다 먼 날짜도 허용
    while d <= end_day and len(out) < max_count:
        if rule.runs_on(d, first):
            start = local_to_utc(d, rule.local_time, rule.timezone)
            if start > now:
                out.append(ScheduleOccurrence(start, start + timedelta(minutes=int(rule.duration_minutes)),
                                              start.astimezone(zone)))
        d += timedelta(days=1)
    return out


def plan_top_up(rule: ScheduleRule, existing_future_keys: set[str], now: datetime, *,
                max_future: int = MAX_FUTURE_BROADCASTS) -> list[ScheduleOccurrence]:
    """이미 만든 미래 예약과 합쳐 최대 max_future개가 되도록 '아직 없는 회차'만."""
    room = max(0, max_future - len(existing_future_keys))
    return [o for o in occurrences(rule, now) if o.key not in existing_future_keys][:room]


# ---------------- 예약 생성 ----------------

@dataclass
class ReservationResult:
    broadcast_id: str = ""
    scheduled_start_utc: str = ""
    privacy: str = ""
    broadcast_ok: bool = False
    bind_ok: bool = False
    metadata_ok: bool = False
    thumbnail_ok: bool | None = None  # None = 썸네일 없음
    tags_applied: list[str] = field(default_factory=list)
    thumbnail_name: str = ""
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return self.broadcast_ok and self.bind_ok and self.metadata_ok and self.thumbnail_ok is not False

    def summary_lines(self) -> list[str]:
        lines = ["✓ 방송 예약 생성" if self.broadcast_ok else f"✗ 방송 예약 실패: {self.errors.get('broadcast', '')}"]
        if self.broadcast_ok:
            lines.append("✓ 송출 스트림 연결" if self.bind_ok else f"⚠ 송출 스트림 연결 실패: {self.errors.get('bind', '')}")
            lines.append(f"✓ 태그/카테고리 적용 ({len(self.tags_applied)}개)" if self.metadata_ok
                         else f"⚠ 태그/카테고리 적용 실패: {self.errors.get('metadata', '')}")
            if self.thumbnail_ok is True:
                lines.append(f"✓ 썸네일 ({self.thumbnail_name})")
            elif self.thumbnail_ok is False:
                lines.append(f"⚠ 썸네일 업로드 실패: {self.errors.get('thumbnail', '')}")
            lines.append(f"YouTube video ID: {self.broadcast_id}")
        return lines


def apply_metadata(api: YouTubeApiClient, video_id: str, md: BroadcastMetadata, result: ReservationResult) -> None:
    try:
        api.update_video_metadata(video_id, tags=md.tags, category_id=md.category_id,
                                  default_language=md.default_language or None)
        result.metadata_ok = True
        result.tags_applied = list(md.tags)
        result.errors.pop("metadata", None)
    except YouTubeApiError as e:
        result.metadata_ok = False
        result.errors["metadata"] = str(e)


def upload_thumbnail(api: YouTubeApiClient, video_id: str, path: str, result: ReservationResult) -> None:
    if not path:
        result.thumbnail_ok = None
        return
    try:
        info = validate_thumbnail(path)
        api.set_thumbnail(video_id, info.path.read_bytes(), info.mime)
        result.thumbnail_ok = True
        result.thumbnail_name = info.path.name
        result.errors.pop("thumbnail", None)
    except (YouTubeApiError, MetadataError, OSError) as e:
        result.thumbnail_ok = False
        result.thumbnail_name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
        result.errors["thumbnail"] = str(e)


def create_reservation(api: YouTubeApiClient, md: BroadcastMetadata, occ: ScheduleOccurrence, *,
                       stream_id: str | None = None) -> ReservationResult:
    md.validate()
    r = ReservationResult(scheduled_start_utc=occ.start_utc.isoformat(), privacy=md.privacy_status)
    try:
        b = api.insert_broadcast(title=md.title, description=md.description, privacy=md.privacy_status,
                                 made_for_kids=md.made_for_kids, scheduled_start=occ.start_utc.timestamp(),
                                 scheduled_end=occ.end_utc.timestamp())
    except YouTubeApiError as e:
        r.errors["broadcast"] = str(e)
        return r
    r.broadcast_id, r.broadcast_ok = b.id, True
    if stream_id:
        try:
            api.bind_broadcast(b.id, stream_id)
            r.bind_ok = True
        except YouTubeApiError as e:
            r.errors["bind"] = str(e)
    else:
        r.bind_ok = True  # 연결은 시작할 때 (수동 Key 모드 등)
    apply_metadata(api, b.id, md, r)
    upload_thumbnail(api, b.id, md.thumbnail_path, r)
    try:
        api.get_broadcast(b.id)  # 최종 확인
    except YouTubeApiError as e:
        r.errors["verify"] = str(e)
    return r


# ---------------- 로컬 예약 목록 (settings.json, 비밀 없음) ----------------

@dataclass
class ReservationRecord:
    broadcast_id: str
    title: str
    start_utc: str
    end_utc: str = ""
    privacy: str = "unlisted"
    thumbnail_name: str = ""
    status: str = "created"
    rule_id: str = ""
    session: int = 1
    auto_rollover: bool = False
    metadata_ok: bool = True
    thumbnail_ok: bool | None = None

    @property
    def status_label(self) -> str:
        if not self.metadata_ok or self.thumbnail_ok is False:
            return "예약됨 (일부 실패)" if STATUS_LABELS.get(self.status) == "예약됨" else STATUS_LABELS.get(self.status, "오류")
        return STATUS_LABELS.get(self.status, "오류")

    def start_local(self, tz: str = DEFAULT_TZ) -> datetime:
        return datetime.fromisoformat(self.start_utc).astimezone(get_zone(tz))

    @property
    def youtube_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.broadcast_id}"


class ReservationStore:
    KEY = "reservations"

    def __init__(self, load, save):
        self._load, self._save = load, save

    def all(self) -> list[ReservationRecord]:
        items = (self._load() or {}).get(self.KEY) or []
        known = set(ReservationRecord.__dataclass_fields__)
        out = []
        for d in items:
            if isinstance(d, dict) and d.get("broadcast_id"):
                out.append(ReservationRecord(**{k: v for k, v in d.items() if k in known}))
        return sorted(out, key=lambda r: r.start_utc)

    def _write(self, records: list[ReservationRecord]) -> None:
        self._save(**{self.KEY: [asdict(r) for r in records]})

    def upsert(self, rec: ReservationRecord) -> None:
        recs = [r for r in self.all() if r.broadcast_id != rec.broadcast_id] + [rec]
        self._write(recs)

    def remove(self, broadcast_id: str) -> None:
        self._write([r for r in self.all() if r.broadcast_id != broadcast_id])

    def future_keys(self, rule_id: str, now: datetime) -> set[str]:
        out = set()
        for r in self.all():
            if r.rule_id == rule_id and r.status not in ("complete", "revoked"):
                st = datetime.fromisoformat(r.start_utc)
                if st > now:
                    out.add(st.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"))
        return out


def top_up(api: YouTubeApiClient, store: ReservationStore, *, rule_id: str, rule: ScheduleRule,
           template: MetadataTemplate, now: datetime, stream_id: str | None = None, channel: str = "",
           start_session: int = 1, thumb_counter: int = 0, auto_rollover: bool = False) -> list[ReservationResult]:
    """반복 규칙의 부족분만 만든다 (최대 7개, 7일). 각 회차마다 템플릿 변수/썸네일 순환 적용."""
    results = []
    existing = store.future_keys(rule_id, now)
    for i, occ in enumerate(plan_top_up(rule, existing, now)):
        session = start_session + len(existing) + i
        md = template.render(local_start=occ.local_start, session=session, channel=channel,
                             thumb_counter=thumb_counter + len(existing) + i)
        res = create_reservation(api, md, occ, stream_id=stream_id)
        results.append(res)
        if res.broadcast_ok:
            store.upsert(ReservationRecord(
                broadcast_id=res.broadcast_id, title=md.title, start_utc=occ.start_utc.isoformat(),
                end_utc=occ.end_utc.isoformat(), privacy=md.privacy_status, thumbnail_name=res.thumbnail_name,
                rule_id=rule_id, session=session, auto_rollover=auto_rollover, metadata_ok=res.metadata_ok,
                thumbnail_ok=res.thumbnail_ok))
        else:
            break  # 생성 자체가 실패하면 나머지는 다음 보충 때
    return results
