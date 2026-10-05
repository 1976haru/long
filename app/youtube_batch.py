"""예약 업로드 일괄 작업 (순수 로직, Tk/네트워크 없음).

- 폴더 영상 검색 (이름순, 하위 폴더는 기본 OFF) — 파일 이동/삭제/이름 변경은 절대 하지 않는다.
- 영상 ↔ 썸네일 자동 매칭 (같은 이름 우선, 후보가 여럿이면 고르지 않고 표시).
- 여러 영상 예약 시각 계산 (매일/평일/2일마다/매주/N일마다, 채널 시간대 기준).
- 채널별 업로드 템플릿 (MetadataTemplate 재사용) 저장 + 채널마다 마지막/기본 템플릿.
- 대기열에 넣기 전 미리보기 계획 (오류는 추가 차단, 경고는 표시만).
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

from .settings import load_settings, update_settings
from .youtube_accounts import ChannelProfile, ProfileStore, new_profile_id
from .youtube_api import YouTubeApiError
from .youtube_comments import render_first_comment
from .youtube_metadata import (
    WEEKDAYS_BY_LANGUAGE, MetadataError, MetadataTemplate, parse_tags, pick_thumbnail, render_template,
    validate_description, validate_thumbnail, validate_title,
)
from .youtube_schedule import ScheduleError, get_zone, local_to_utc
from .youtube_upload import VIDEO_EXTENSIONS, validate_publish_at, validate_video_file

THUMB_EXTS = (".jpg", ".jpeg", ".png")
DAILY, WEEKDAYS, EVERY_2_DAYS, WEEKLY, EVERY_N_DAYS = "daily", "weekdays", "every2", "weekly", "every_n"
INTERVAL_LABELS = {DAILY: "매일", WEEKDAYS: "평일", EVERY_2_DAYS: "2일마다", WEEKLY: "매주", EVERY_N_DAYS: "N일마다"}
DEFAULT_TITLE_TEMPLATE = "{filename}"
SPEEDS_MBPS = (10, 50, 100)
LAST_FOLDER_KEY = "upload_last_folder"


def _natural_key(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


# ---------------- 폴더 ----------------

def scan_folder(folder, *, recursive: bool = False) -> list[Path]:
    """폴더 안 영상(MP4 등)을 이름순(001, 002, …, 10)으로. 제작 중 임시 파일(.part.mp4)은 뺀다."""
    d = Path(str(folder or "").strip().strip('"'))
    if not d.is_dir():
        raise ValueError("영상 폴더를 찾을 수 없습니다.")
    it = d.rglob("*") if recursive else d.iterdir()
    out = [p for p in it if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
           and not p.name.lower().endswith(".part.mp4")]
    return sorted(out, key=lambda p: ([str(x).lower() for x in p.parent.relative_to(d).parts], _natural_key(p)))


def remember_folder(folder) -> None:
    update_settings(**{LAST_FOLDER_KEY: str(folder)})


def last_folder() -> str:
    v = load_settings().get(LAST_FOLDER_KEY)
    return v if isinstance(v, str) and v and Path(v).is_dir() else ""


def episode_from_filename(name: str) -> str:
    """파일 이름의 마지막 숫자 → 회차 후보 ('여_003.mp4' → '3'). 없으면 '' (사용자가 직접 지정)."""
    nums = re.findall(r"\d+", Path(name).stem)
    return str(int(nums[-1])) if nums else ""


# ---------------- 썸네일 매칭 ----------------

@dataclass
class ThumbMatch:
    path: str = ""
    status: str = "missing"  # exact | suffix | template | manual | missing | ambiguous
    candidates: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.path:
            mark = {"template": " (템플릿)", "manual": " (직접 지정)"}.get(self.status, "")
            return f"{Path(self.path).name} ✓{mark}"
        if self.status == "ambiguous":
            return f"후보 {len(self.candidates)}개 ⚠ 직접 선택"
        return "없음 ⚠"


def _images_in(folder: Path) -> dict[str, list[Path]]:
    by_lower: dict[str, list[Path]] = {}
    try:
        for p in folder.iterdir():
            if p.is_file() and p.suffix.lower() in THUMB_EXTS:
                by_lower.setdefault(p.stem.lower(), []).append(p)
    except OSError:
        pass
    return by_lower


def _ordered(paths: list[Path]) -> list[Path]:
    return sorted(paths, key=lambda p: (THUMB_EXTS.index(p.suffix.lower()), p.name))


def match_thumbnail(video, images: dict[str, list[Path]] | None = None) -> ThumbMatch:
    """1) 같은 이름(001.jpg/.jpeg/.png)  2) 이름_thumbnail.*  — 같은 단계에 후보가 2개 이상이면 고르지 않는다."""
    v = Path(video)
    images = _images_in(v.parent) if images is None else images
    stem = v.stem.lower()
    for key, status in ((stem, "exact"), (stem + "_thumbnail", "suffix")):
        found = _ordered(images.get(key, []))
        if len(found) == 1:
            return ThumbMatch(str(found[0]), status)
        if len(found) > 1:
            return ThumbMatch("", "ambiguous", [str(p) for p in found])
    return ThumbMatch()


def match_all(videos: list, template: MetadataTemplate | None = None) -> list[ThumbMatch]:
    """같은 이름이 없는 영상은 템플릿 썸네일 방식(고정 1개 / 폴더에서 순서대로)으로 채운다. 애매한 것은 그대로 둔다."""
    cache: dict[Path, dict] = {}
    out = []
    for i, v in enumerate(videos):
        folder = Path(v).parent
        if folder not in cache:
            cache[folder] = _images_in(folder)
        m = match_thumbnail(v, cache[folder])
        if m.status == "missing" and template is not None:
            t = pick_thumbnail(template.thumbnail_mode, template.thumbnail_paths, template.thumbnail_folder, i)
            if t:
                m = ThumbMatch(t, "template")
        out.append(m)
    return out


# ---------------- 예약 시각 ----------------

def schedule_times(first_day: date, at: dtime, tz: str, count: int, interval: str = DAILY,
                   every_days: int = 1) -> list[datetime]:
    """첫 날짜/시각부터 count개 (UTC aware). 시각은 채널 시간대 현지 기준, 평일은 토·일을 건너뛴다."""
    if count <= 0:
        return []
    step = {DAILY: 1, EVERY_2_DAYS: 2, WEEKLY: 7, WEEKDAYS: 1}.get(interval)
    if interval == EVERY_N_DAYS:
        if not 1 <= int(every_days) <= 365:
            raise ScheduleError("간격은 1~365일이어야 합니다.")
        step = int(every_days)
    if step is None:
        raise ScheduleError("예약 간격이 올바르지 않습니다.")
    out, d = [], first_day
    while len(out) < count:
        if interval != WEEKDAYS or d.weekday() < 5:
            out.append(local_to_utc(d, at, tz))
        d += timedelta(days=step)
    return out


def estimate_seconds(total_bytes: int, mbps: float) -> float:
    return total_bytes * 8 / (mbps * 1_000_000) if mbps > 0 else 0.0


def human_size(n: int) -> str:
    return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.1f} MB"


def human_duration(sec: float) -> str:
    m = int(round(sec / 60))
    return f"{m // 60}시간 {m % 60}분" if m >= 60 else f"{max(1, m)}분"


# ---------------- 채널별 업로드 템플릿 ----------------

class UploadTemplateStore:
    """settings.json "upload_templates": [{template_id, profile_id, ...MetadataTemplate}] (비밀 없음)."""
    KEY = "upload_templates"
    LAST_KEY = "upload_last_template"  # profile_id → 마지막으로 쓴 template_id

    def _rows(self) -> list[dict]:
        return [r for r in (load_settings().get(self.KEY) or []) if isinstance(r, dict) and r.get("template_id")]

    def for_profile(self, profile_id: str) -> list[tuple[str, MetadataTemplate]]:
        return [(r["template_id"], MetadataTemplate.from_dict(r)) for r in self._rows() if r.get("profile_id") == profile_id]

    def get(self, template_id: str) -> tuple[str, MetadataTemplate] | None:
        r = next((r for r in self._rows() if r["template_id"] == template_id), None)
        return (r["profile_id"], MetadataTemplate.from_dict(r)) if r else None

    def save(self, profile_id: str, template: MetadataTemplate, template_id: str = "") -> str:
        template.validate()
        rows = self._rows()
        same = next((r for r in rows if r.get("profile_id") == profile_id and r.get("name") == template.name
                     and r["template_id"] != template_id), None)
        if same is not None:
            template_id = same["template_id"]  # 같은 채널 같은 이름 → 덮어쓰기
        template_id = template_id or secrets.token_hex(6)
        rows = [r for r in rows if r["template_id"] != template_id]
        rows.append({"template_id": template_id, "profile_id": profile_id, **template.to_dict()})
        update_settings(**{self.KEY: rows})
        return template_id

    def delete(self, template_id: str) -> None:
        update_settings(**{self.KEY: [r for r in self._rows() if r["template_id"] != template_id]})

    def remember(self, profile_id: str, template_id: str) -> None:
        cur = load_settings().get(self.LAST_KEY)
        cur = dict(cur) if isinstance(cur, dict) else {}
        cur[profile_id] = template_id
        update_settings(**{self.LAST_KEY: cur})

    def pick_for(self, profile: ChannelProfile) -> tuple[str, MetadataTemplate] | None:
        """마지막으로 쓴 템플릿 → 채널 기본 템플릿 → 그 채널의 첫 템플릿. 다른 채널 템플릿은 절대 쓰지 않는다."""
        own = dict(self.for_profile(profile.profile_id))
        last = (load_settings().get(self.LAST_KEY) or {}).get(profile.profile_id, "")
        for tid in (last, profile.default_template_id):
            if tid and tid in own:
                return tid, own[tid]
        return next(iter(own.items()), None)


def profile_defaults_template(profile: ChannelProfile) -> MetadataTemplate:
    """템플릿이 없는 채널: 이전 채널 값이 남지 않도록 채널 기본값만으로 새로 시작."""
    return MetadataTemplate(name="", title_template=DEFAULT_TITLE_TEMPLATE, category_id=profile.category_id,
                            privacy_status=profile.privacy, made_for_kids=profile.made_for_kids,
                            default_language=profile.language)


def clone_profile(profiles: ProfileStore, templates: UploadTemplateStore, source: ChannelProfile,
                  alias: str) -> ChannelProfile:
    """설정만 복제 (언어/시간대/카테고리/공개/아동용/템플릿). 채널 ID·이름·OAuth JSON·token은 복제하지 않는다."""
    new = ChannelProfile(new_profile_id(), alias, language=source.language, timezone=source.timezone,
                         category_id=source.category_id, privacy=source.privacy, made_for_kids=source.made_for_kids)
    profiles.add(new)
    for tid, tpl in templates.for_profile(source.profile_id):
        copied = templates.save(new.profile_id, replace(tpl))
        if tid == source.default_template_id:
            new.default_template_id = copied
    if new.default_template_id:
        profiles.save(new)
    return new


# ---------------- 미리보기 계획 ----------------

@dataclass
class BatchItem:
    video_path: str
    thumb: ThumbMatch = field(default_factory=ThumbMatch)
    episode: str = ""

    @property
    def name(self) -> str:
        return Path(self.video_path).name

    @property
    def size(self) -> int:
        try:
            return Path(self.video_path).stat().st_size
        except OSError:
            return 0


@dataclass
class PlannedUpload:
    n: int
    video_path: str
    thumbnail_path: str
    thumb_label: str
    publish_at: datetime | None
    local_text: str
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    category_id: str = "10"
    language: str = ""
    made_for_kids: bool = False
    privacy: str = "private"
    first_comment: str = ""  # 공개된 뒤 자동으로 달 첫 댓글 (비어 있으면 없음)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class BatchPlan:
    profile_id: str
    alias: str
    channel_title: str
    channel_id: str
    timezone: str
    items: list[PlannedUpload]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def all_errors(self) -> list[str]:
        return self.errors + [f"{i.n}. {i.title or Path(i.video_path).name}: {e}" for i in self.items for e in i.errors]

    @property
    def all_warnings(self) -> list[str]:
        return self.warnings + [f"{i.n}. {Path(i.video_path).name}: {w}" for i in self.items for w in i.warnings]

    @property
    def ok(self) -> bool:
        return bool(self.items) and not self.all_errors

    @property
    def thumb_count(self) -> int:
        return sum(bool(i.thumbnail_path) for i in self.items)

    @property
    def first_comment_count(self) -> int:
        return sum(bool(i.first_comment) for i in self.items)

    @property
    def first_comment_text(self) -> str:
        """미리보기 한 줄: '10/10 자동등록 예정 (공개 후)'."""
        n, total = self.first_comment_count, len(self.items)
        if not n:
            return "사용 안 함"
        when = "공개 후" if any(i.publish_at for i in self.items if i.first_comment) else "업로드·처리 후"
        private_now = sum(1 for i in self.items if i.first_comment and not i.publish_at and i.privacy == "private")
        tail = f" · 비공개 {private_now}개는 공개로 바꿀 때까지 대기" if private_now else ""
        return f"{n}/{total} 자동등록 예정 ({when}){tail}"


def build_plan(profile: ChannelProfile, items: list[BatchItem], template: MetadataTemplate, *,
               times: list[datetime] | None, privacy_now: str, now: datetime, queued_paths: set[str] = frozenset(),
               capacity: int = 100) -> BatchPlan:
    """영상마다 제목/설명/태그를 템플릿으로 만들고 검증. times=None이면 지금 올리기(privacy_now)."""
    zone = get_zone(profile.timezone)
    plan = BatchPlan(profile.profile_id, profile.alias, profile.channel_title, profile.channel_id, profile.timezone, [])
    if not items:
        plan.errors.append("업로드할 영상을 추가하세요.")
    if not profile.channel_id:
        plan.errors.append(f"'{profile.alias}' 채널이 아직 연결되지 않았습니다. [YouTube 채널 관리]에서 연결하세요.")
    if len(items) > capacity:
        plan.errors.append(f"대기열 남은 자리 {capacity}개보다 많습니다 ({len(items)}개).")
    if times is not None and len(times) < len(items):
        plan.errors.append("예약 시각 개수가 영상 개수보다 적습니다.")
    language = template.default_language or profile.language
    weekdays = WEEKDAYS_BY_LANGUAGE.get(language or "ko", WEEKDAYS_BY_LANGUAGE["ko"])
    try:
        tags = parse_tags(template.tags)
    except MetadataError as e:
        tags = []
        plan.errors.append(str(e))
    seen: set[str] = set()
    for i, it in enumerate(items, 1):
        when = times[i - 1] if times is not None and i - 1 < len(times) else None
        local = (when or now).astimezone(zone)
        pu = PlannedUpload(i, it.video_path, it.thumb.path, it.thumb.label, when,
                           f"{local:%Y-%m-%d} ({weekdays[local.weekday()]}) {local:%H:%M}" if when else "지금 올리기",
                           tags=list(tags), category_id=str(template.category_id), language=language or "",
                           made_for_kids=bool(template.made_for_kids),
                           privacy="private" if when else privacy_now)
        key = str(Path(it.video_path)).lower()
        if key in seen:
            pu.errors.append("같은 영상이 두 번 들어 있습니다.")
        seen.add(key)
        if key in queued_paths:
            pu.warnings.append("이미 예약 업로드 대기열에 있는 영상입니다 (중복 업로드 주의).")
        values = dict(local_start=local, session=i, n=i, channel=profile.channel_title or profile.alias,
                      series=template.series, episode=it.episode, filename=Path(it.video_path).stem,
                      language=language or "ko")
        try:
            pu.title = validate_title(render_template(template.title_template or DEFAULT_TITLE_TEMPLATE, **values),
                                      "영상 제목")
            pu.description = validate_description(render_template(template.description_template, **values))
        except ValueError as e:  # MetadataError 포함, 중괄호 오류
            pu.errors.append(str(e))
        if template.first_comment_enabled:
            try:
                pu.first_comment = render_first_comment(
                    template.first_comment_template, title=pu.title or Path(it.video_path).stem,
                    channel=profile.channel_title or profile.alias, local_start=local, series=template.series,
                    episode=it.episode, filename=Path(it.video_path).stem, language=language or "ko")
            except ValueError as e:
                pu.errors.append(f"첫 댓글: {e}")
            if not when and pu.privacy == "private":
                pu.warnings.append("비공개 영상에는 댓글을 달 수 없어, 공개/일부공개로 바꿀 때까지 첫 댓글이 대기합니다.")
        if ("{episode}" in (template.title_template + template.description_template)) and not it.episode:
            pu.warnings.append("회차 번호를 찾지 못했습니다 → [회차 지정]으로 입력하세요.")
        try:
            validate_video_file(it.video_path)
        except YouTubeApiError as e:
            pu.errors.append(str(e))
        if when is not None:
            try:
                validate_publish_at(when, now)
            except YouTubeApiError as e:
                pu.errors.append(str(e))
        if it.thumb.path:
            try:
                validate_thumbnail(it.thumb.path)
            except MetadataError as e:
                pu.warnings.append(f"썸네일 사용 안 함: {e}")
                pu.thumbnail_path, pu.thumb_label = "", "없음 ⚠"
        elif it.thumb.status == "ambiguous":
            pu.warnings.append("썸네일 후보가 여러 개라 자동으로 고르지 않았습니다 (썸네일 없이 업로드).")
        else:
            pu.warnings.append("썸네일 없음 (영상은 올라갑니다).")
        plan.items.append(pu)
    return plan


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
