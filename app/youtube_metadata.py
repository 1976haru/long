"""YouTube 예약 LIVE 메타데이터 (Phase 3B.1) — 제목/설명/태그/카테고리/언어/썸네일, 템플릿 변수, 썸네일 회전.

순수 로직 (Tk/네트워크 없음). 썸네일 해상도는 PNG/JPEG 헤더만 읽어 확인한다 (이미지 라이브러리 없음).
"""
from __future__ import annotations

import string
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

TITLE_MAX = 100
DESCRIPTION_MAX = 5000
TAGS_TOTAL_MAX = 500  # YouTube 태그 전체 길이 한도
THUMBNAIL_MAX_BYTES = 50 * 1024 * 1024  # thumbnails.set 공식 최대
THUMBNAIL_RECOMMENDED = (1280, 720)
TEMPLATE_VARIABLES = ("date", "yyyy", "mm", "dd", "month", "day", "weekday", "session", "channel",
                      "series", "episode", "n", "filename")
WEEKDAYS_KO = ("월", "화", "수", "목", "금", "토", "일")
WEEKDAYS_BY_LANGUAGE = {"ko": WEEKDAYS_KO, "ja": ("月", "火", "水", "木", "金", "土", "日"),
                        "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
                        "fr": ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")}
PRIVACY_LABELS = {"public": "공개", "unlisted": "일부공개", "private": "비공개"}
# 친숙한 이름 → YouTube categoryId (기본값). 실제 목록은 videoCategories.list로도 받을 수 있다.
DEFAULT_CATEGORIES = {"10": "음악", "24": "엔터테인먼트", "22": "인물/블로그", "29": "비영리/사회운동"}
DEFAULT_CATEGORY_ID = "10"
LANGUAGES = {"ko": "한국어", "en": "English", "ja": "日本語", "fr": "Français", "": "설정 안 함"}
TIMEZONES = ("Asia/Seoul", "Asia/Tokyo", "UTC", "America/New_York", "Europe/Paris")
THUMB_FIXED, THUMB_FOLDER, THUMB_ROTATE = "fixed", "folder", "rotate"


class MetadataError(ValueError):
    pass


def validate_title(title: str, label: str = "LIVE 제목") -> str:
    t = (title or "").strip()
    if not t:
        raise MetadataError(f"{label}을 입력하세요.")
    if len(t) > TITLE_MAX:
        raise MetadataError(f"{label}은 {TITLE_MAX}자 이하여야 합니다 (현재 {len(t)}자).")
    if "<" in t or ">" in t:
        raise MetadataError(f"{label}에 < > 문자는 쓸 수 없습니다.")
    return t


def validate_description(desc: str) -> str:
    d = (desc or "").replace("\r\n", "\n")
    if len(d) > DESCRIPTION_MAX:
        raise MetadataError(f"설명은 {DESCRIPTION_MAX}자 이하여야 합니다 (현재 {len(d)}자).")
    if "<" in d or ">" in d:
        raise MetadataError("설명에 < > 문자는 쓸 수 없습니다.")
    return d


COMMENT_MAX = 2000  # 첫 댓글/답글 (YouTube 한도보다 훨씬 짧게 — 긴 자동 댓글은 스팸처럼 보인다)


def validate_comment_text(text: str, label: str = "첫 댓글") -> str:
    t = (text or "").replace("\r\n", "\n").strip()
    if not t:
        raise MetadataError(f"{label} 내용을 입력하세요.")
    if len(t) > COMMENT_MAX:
        raise MetadataError(f"{label}은 {COMMENT_MAX}자 이하로 입력하세요 (현재 {len(t)}자).")
    return t


def parse_tags(text) -> list[str]:
    """쉼표로 나누기 → 앞뒤 공백 제거 → 빈 값 제거 → 중복 제거(대소문자 무시, 처음 것 유지)."""
    raw = text if isinstance(text, (list, tuple)) else str(text or "").replace("\n", ",").split(",")
    out, seen = [], set()
    for t in raw:
        t = str(t).strip().lstrip("#").strip()
        if not t or t.lower() in seen:
            continue
        if "<" in t or ">" in t:
            raise MetadataError(f"태그에 < > 문자는 쓸 수 없습니다: {t}")
        seen.add(t.lower())
        out.append(t)
    if tags_length(out) > TAGS_TOTAL_MAX:
        raise MetadataError(f"태그 전체 길이가 {TAGS_TOTAL_MAX}자를 넘습니다 (현재 {tags_length(out)}자).")
    return out


def tags_length(tags) -> int:
    """YouTube 방식: 공백 있는 태그는 따옴표 2자 추가, 태그 사이 쉼표 1자."""
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + max(0, len(tags) - 1)


# ---------------- 템플릿 변수 ----------------

def template_variables(text: str) -> set[str]:
    names = set()
    for _, name, _, _ in string.Formatter().parse(text or ""):
        if name is not None:
            names.add(name)
    return names


def check_template(text: str) -> None:
    unknown = template_variables(text) - set(TEMPLATE_VARIABLES)
    if unknown:
        raise MetadataError("알 수 없는 변수: " + ", ".join("{" + u + "}" for u in sorted(unknown))
                            + "  (사용 가능: " + " ".join("{" + v + "}" for v in TEMPLATE_VARIABLES) + ")")


def render_template(text: str, *, local_start: datetime, session: int, channel: str = "", series: str = "",
                    episode: str = "", n: int | None = None, filename: str = "", language: str = "ko") -> str:
    """local_start는 시간대가 있는(aware) 현지 시각. {session}은 2자리 (01, 02 …), {n}은 순번 (1, 2 …).
    {weekday}는 채널 언어 기준 (한국어 '화', 일본어 '火'). {filename}은 확장자 없는 파일 이름."""
    if local_start.tzinfo is None:
        raise MetadataError("시간대 없는 날짜는 사용할 수 없습니다.")
    check_template(text)
    weekdays = WEEKDAYS_BY_LANGUAGE.get(language or "ko", WEEKDAYS_KO)
    values = {
        "date": local_start.strftime("%Y.%m.%d"), "yyyy": local_start.strftime("%Y"), "mm": local_start.strftime("%m"),
        "dd": local_start.strftime("%d"), "month": local_start.strftime("%m"),
        "day": local_start.strftime("%d"), "weekday": weekdays[local_start.weekday()],
        "session": f"{session:02d}", "channel": channel or "", "series": series or "", "episode": episode or "",
        "n": str(session if n is None else n), "filename": filename or "",
    }
    return (text or "").format(**values)


# ---------------- 썸네일 ----------------

@dataclass(frozen=True)
class ThumbnailInfo:
    path: Path
    mime: str
    size: int
    width: int
    height: int

    @property
    def is_16_9(self) -> bool:
        return self.height > 0 and abs(self.width / self.height - 16 / 9) < 0.02

    @property
    def note(self) -> str:
        notes = []
        if (self.width, self.height) == THUMBNAIL_RECOMMENDED:
            notes.append("권장 해상도 1280×720")
        elif not self.is_16_9:
            notes.append("16:9 비율을 권장합니다")
        return ", ".join(notes)


def _png_size(head: bytes) -> tuple[int, int]:
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def _jpeg_size(path: Path) -> tuple[int, int]:
    with open(path, "rb") as f:
        f.read(2)
        while True:
            b = f.read(1)
            while b and b != b"\xff":
                b = f.read(1)
            while b == b"\xff":
                b = f.read(1)
            if not b:
                break
            marker = b[0]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                continue
            seg = f.read(2)
            if len(seg) < 2:
                break
            length = int.from_bytes(seg, "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                data = f.read(5)
                return int.from_bytes(data[3:5], "big"), int.from_bytes(data[1:3], "big")
            f.seek(length - 2, 1)
    return 0, 0


def validate_thumbnail(path) -> ThumbnailInfo:
    """존재, 확장자(JPG/JPEG/PNG), 실제 형식(파일 머리), 크기 ≤ 50MB. 해상도는 권장만 안내하고 막지 않는다."""
    p = Path(str(path or "").strip().strip('"'))
    if not str(path or "").strip() or not p.is_file():
        raise MetadataError("썸네일 파일을 찾을 수 없습니다.")
    ext = p.suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png"):
        raise MetadataError("썸네일은 JPG 또는 PNG 파일만 쓸 수 있습니다.")
    size = p.stat().st_size
    if size == 0:
        raise MetadataError("썸네일 파일이 비어 있습니다.")
    if size > THUMBNAIL_MAX_BYTES:
        raise MetadataError(f"썸네일은 50MB 이하여야 합니다 (현재 {size / 1024 / 1024:.1f}MB).")
    with open(p, "rb") as f:
        head = f.read(32)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        mime, (w, h) = "image/png", _png_size(head)
    elif head.startswith(b"\xff\xd8"):
        mime, (w, h) = "image/jpeg", _jpeg_size(p)
    else:
        raise MetadataError("썸네일 파일 내용이 JPG/PNG가 아닙니다.")
    if (mime == "image/png") != (ext == ".png"):
        raise MetadataError("파일 확장자와 실제 이미지 형식이 다릅니다.")
    return ThumbnailInfo(p, mime, size, w, h)


def thumbnail_candidates(mode: str, paths: list[str], folder: str = "") -> list[str]:
    if mode == THUMB_FOLDER and folder:
        d = Path(folder)
        return sorted(str(x) for x in d.iterdir() if x.suffix.lower() in (".jpg", ".jpeg", ".png")) if d.is_dir() else []
    if mode == THUMB_ROTATE:
        return [p for p in paths if p][:3]
    return [p for p in paths if p][:1]


def pick_thumbnail(mode: str, paths: list[str], folder: str, counter: int) -> str:
    """고정 1개 / 폴더에서 순서대로 / 후보 3개 순환 (01 → 02 → 03 → 01)."""
    c = thumbnail_candidates(mode, paths, folder)
    return c[counter % len(c)] if c else ""


# ---------------- 모델 ----------------

@dataclass
class BroadcastMetadata:
    title: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    thumbnail_path: str = ""
    category_id: str = DEFAULT_CATEGORY_ID
    privacy_status: str = "unlisted"
    made_for_kids: bool = False
    default_language: str = ""

    def validate(self, label: str = "LIVE 제목") -> "BroadcastMetadata":
        self.title = validate_title(self.title, label)
        self.description = validate_description(self.description)
        self.tags = parse_tags(self.tags)
        if self.privacy_status not in PRIVACY_LABELS:
            raise MetadataError("공개 상태가 올바르지 않습니다.")
        if not str(self.category_id).isdigit():
            raise MetadataError("카테고리가 올바르지 않습니다.")
        if self.thumbnail_path:
            validate_thumbnail(self.thumbnail_path)
        return self


@dataclass
class MetadataTemplate:
    """저장 가능한 메타데이터 템플릿. Stream Key/OAuth token은 넣지 않는다."""
    name: str
    title_template: str
    description_template: str = ""
    tags: list[str] = field(default_factory=list)
    thumbnail_mode: str = THUMB_FIXED
    thumbnail_paths: list[str] = field(default_factory=list)
    thumbnail_folder: str = ""
    category_id: str = DEFAULT_CATEGORY_ID
    privacy_status: str = "unlisted"
    made_for_kids: bool = False
    default_language: str = ""
    series: str = ""  # 예약 업로드 {series}
    first_comment_enabled: bool = False  # 예약 업로드: 공개된 뒤 첫 댓글 자동등록
    first_comment_template: str = ""  # 변수: {title} {channel} {date} {series} {episode} {filename}

    def validate(self) -> "MetadataTemplate":
        if not (self.name or "").strip():
            raise MetadataError("템플릿 이름을 입력하세요.")
        check_template(self.title_template)
        check_template(self.description_template)
        self.tags = parse_tags(self.tags)
        if self.thumbnail_mode not in (THUMB_FIXED, THUMB_FOLDER, THUMB_ROTATE):
            raise MetadataError("썸네일 방식이 올바르지 않습니다.")
        if self.privacy_status not in PRIVACY_LABELS:
            raise MetadataError("공개 상태가 올바르지 않습니다.")
        return self

    def render(self, *, local_start: datetime, session: int, channel: str = "", thumb_counter: int = 0) -> BroadcastMetadata:
        """실제 값으로 바꿔 검증까지 (100자 넘으면 예약 생성 전에 차단)."""
        md = BroadcastMetadata(
            title=render_template(self.title_template, local_start=local_start, session=session, channel=channel),
            description=render_template(self.description_template, local_start=local_start, session=session, channel=channel),
            tags=list(self.tags),
            thumbnail_path=pick_thumbnail(self.thumbnail_mode, self.thumbnail_paths, self.thumbnail_folder, thumb_counter),
            category_id=self.category_id, privacy_status=self.privacy_status, made_for_kids=self.made_for_kids,
            default_language=self.default_language)
        return md.validate()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MetadataTemplate":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})
