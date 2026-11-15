"""⑤ 배경 영상 검색 — 주제·검색어 변환, 제공처 어댑터, 길이 모드·해상도 필터, 주제 적합도 근거.

원칙 (참고: 1976haru/newvideo feature/v2-research-rebuild 의 SourceAdapter·SearchCoordinator·C024~C030 설계)
- 길이 모드 2개는 완전히 분리한다: 장편 원본(제공처가 알려준 실제 원본 길이 ≥ 180초) / 짧은 클립(10~120초).
  길이를 모르는 영상은 어느 모드에서도 '확인된 길이'로 통과시키지 않고, 결과가 없어도 다른 모드로 자동 대체하지 않는다.
- Pexels는 업로드 상한이 2분이라 장편 원본 제공처에서 제외한다 (API 요청 전 차단).
- 해상도는 '제공처 메타데이터 기준'이다. 실제 파일 해상도는 다운로드 후 ffprobe로 따로 확인한다.
- 모든 네트워크 요청은 작업 스레드에서 stock_net.ProviderClient로만 한다 (허용 호스트·간격·캐시).
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field, replace
from html import unescape
from typing import Callable, Sequence
from urllib.parse import quote, urlsplit

from .stock_net import ARCHIVE, COMMONS, PEXELS, PIXABAY, PROVIDERS, NetError, ProviderClient, url_allowed

MODE_SHORT = "short"
MODE_LONG = "long"
MODE_LABELS = {MODE_SHORT: "짧은 클립 조합 (10~120초 → ④에서 3~60분)", MODE_LONG: "3분 이상 원본 (확인된 길이만)"}
SHORT_MIN, SHORT_MAX = 10.0, 120.0
LONG_MIN = 180.0

RES_FHD, RES_4K_PREF, RES_4K_ONLY = "fhd", "4k_pref", "4k_only"
RES_LABELS = {RES_FHD: "FHD 이상", RES_4K_PREF: "4K 우선 (FHD 포함)", RES_4K_ONLY: "4K만"}

# 제공처가 지원하는 모드 (정책). Pexels: 업로드 최대 2분 → 장편 제외 / Archive: 장편 후보 중심
PROVIDER_MODES = {
    PEXELS: frozenset({MODE_SHORT}),
    PIXABAY: frozenset({MODE_SHORT, MODE_LONG}),
    COMMONS: frozenset({MODE_SHORT, MODE_LONG}),
    ARCHIVE: frozenset({MODE_LONG}),
}
NEEDS_KEY = {PEXELS: True, PIXABAY: True, COMMONS: False, ARCHIVE: False}
PROVIDER_NOTES = {
    PEXELS: "짧은 클립 전용 (업로드 상한 2분) · API 키 필요 · 결과 화면에 Pexels 출처 표시",
    PIXABAY: "API 키 필요 · 24시간 캐시 · 대량 자동 다운로드 금지 (선택한 영상만)",
    COMMONS: "키 없음 · CC 라이선스 개별 확인 · WebM/OGV가 많음 (④에서 사용 가능)",
    ARCHIVE: "키 없음 · 장편 후보 · 라이선스(licenseurl)가 있는 자료만 · 상위 8개 항목의 파일 목록만 확인 (오래된 저해상도 많음)",
}


# ---------------- 주제 ----------------

@dataclass(frozen=True)
class Topic:
    key: str
    label: str
    group: str
    terms: tuple[str, ...]       # 영어 검색어 (첫 번째가 대표), 적합도 근거로도 사용
    ko: tuple[str, ...] = ()     # 직접 입력한 한국어에서 찾을 말


TOPICS: tuple[Topic, ...] = (
    Topic("lake", "호수", "자연", ("lake", "pond", "lakeside"), ("호수", "호숫가", "연못")),
    Topic("lakepark", "호수공원", "자연", ("lake park", "park lake", "lake", "park"), ("호수공원",)),
    Topic("ripple", "물결", "자연", ("ripple", "ripples", "water surface", "rippling"), ("물결", "잔물결", "수면")),
    Topic("mountain", "산", "자연", ("mountain", "mountains", "peak"), ("산", "산맥", "봉우리")),
    Topic("forest", "숲", "자연", ("forest", "woods", "trees"), ("숲", "숲속", "나무")),
    Topic("trail", "산책로", "자연", ("trail", "path", "walkway", "footpath"), ("산책로", "오솔길", "길")),
    Topic("sea", "바다", "자연", ("sea", "ocean"), ("바다", "바닷가")),
    Topic("beach", "해변", "자연", ("beach", "shore", "coast"), ("해변", "해안", "모래사장")),
    Topic("waves", "파도", "자연", ("waves", "wave", "surf"), ("파도",)),
    Topic("waterfall", "폭포", "자연", ("waterfall", "falls", "cascade"), ("폭포",)),
    Topic("valley", "계곡", "자연", ("stream", "creek", "valley", "river"), ("계곡", "시냇물", "개울", "강")),
    Topic("cherry", "벚꽃", "계절", ("cherry blossom", "sakura", "cherry blossoms"), ("벚꽃", "사쿠라")),
    Topic("spring", "봄", "계절", ("spring", "blossom", "flowers"), ("봄",)),
    Topic("summer", "여름", "계절", ("summer",), ("여름",)),
    Topic("autumn", "가을", "계절", ("autumn", "fall foliage", "fall leaves", "fall"), ("가을", "단풍")),
    Topic("winter", "겨울", "계절", ("winter",), ("겨울",)),
    Topic("snow", "눈", "계절", ("snow", "snowfall", "snowing", "snowy"), ("눈", "눈 내리는", "눈내리는", "설경", "폭설")),
    Topic("christmas", "크리스마스", "계절", ("christmas", "xmas", "holiday lights"), ("크리스마스", "성탄")),
    Topic("firstsnow", "첫눈", "계절", ("first snow", "snowfall", "snow"), ("첫눈",)),
    Topic("sunset", "노을", "시간", ("sunset", "dusk", "golden hour"), ("노을", "석양", "해질녘")),
    Topic("sunrise", "일출", "시간", ("sunrise", "dawn"), ("일출", "해돋이", "새벽")),
    Topic("night", "야경", "시간", ("night", "city lights", "nightscape"), ("야경", "밤")),
    Topic("rain", "비", "날씨", ("rain", "rainy", "raining", "raindrops"), ("비", "비 오는", "비오는", "빗방울")),
    Topic("cafe", "카페", "장소", ("cafe", "coffee shop", "coffee"), ("카페", "커피숍")),
    Topic("window", "카페 창밖", "장소", ("window", "window view", "through window"), ("창밖", "창문", "창가")),
    Topic("tokyo", "도쿄", "도시", ("tokyo",), ("도쿄", "동경")),
    Topic("osaka", "오사카", "도시", ("osaka",), ("오사카",)),
    Topic("kyoto", "교토", "도시", ("kyoto",), ("교토",)),
    Topic("seoul", "서울", "도시", ("seoul",), ("서울",)),
    Topic("japan", "일본", "도시", ("japan", "japanese"), ("일본",)),
    Topic("city", "도시", "도시", ("city", "street", "urban"), ("도시", "거리", "시내")),
    Topic("static", "고정 촬영", "촬영", ("static", "tripod", "timelapse", "time lapse", "still"), ("고정", "고정 촬영")),
    Topic("walking", "산책 촬영", "촬영", ("walking", "walk", "walking tour", "pov"), ("산책 촬영", "걷기", "워킹")),
    Topic("drone", "드론 촬영", "촬영", ("drone", "aerial"), ("드론", "항공")),
)
TOPIC_BY_KEY = {t.key: t for t in TOPICS}
TOPIC_GROUPS = ("자연", "계절", "시간", "날씨", "장소", "도시", "촬영")


@dataclass(frozen=True)
class QueryPlan:
    text: str                                  # 제공처에 보낼 영어 검색어
    groups: tuple[tuple[str, tuple[str, ...]], ...]  # (주제 이름, 근거 단어들) — 모두 맞아야 '복합 일치'
    unknown_words: tuple[str, ...] = ()        # 사전에 없어 검색어에서 뺀 한국어
    raw: str = ""


KO_PARTICLES = frozenset({"", "이", "가", "은", "는", "을", "를", "의", "에", "에서", "과", "와", "로", "으로", "도", "속", "길",
                          "가의", "변"})
KO_STOPWORDS = frozenset({"보이는", "있는", "내리는", "오는", "풍경", "영상", "배경", "같은", "느낌", "분위기", "속", "의", "및",
                          "그리고", "예쁜", "아름다운", "고요한", "잔잔한"})


def _has_hangul(s: str) -> bool:
    return any("가" <= c <= "힣" for c in s)


def build_query(topic_keys: Sequence[str], free_text: str = "") -> QueryPlan:
    """선택 주제 + 직접 입력 → 영어 검색어와 주제별 근거 단어. 한국어 입력은 주제 사전으로 바꾼다."""
    groups: list[tuple[str, tuple[str, ...]]] = []
    seen: set[str] = set()
    for k in topic_keys:
        t = TOPIC_BY_KEY.get(k)
        if t and t.key not in seen:
            groups.append((t.label, t.terms))
            seen.add(t.key)
    words: list[str] = []
    unknown: list[str] = []
    text = re.sub(r"\s+", " ", (free_text or "").strip())[:100]

    def add(t: Topic):
        if t.key not in seen:
            groups.append((t.label, t.terms))
            seen.add(t.key)

    if text:
        rest = f" {text} "
        # 1) 띄어쓰기가 있는 표현을 통째로 (예: '눈 내리는', '비 오는', '산책 촬영') — 낱말 경계에서만
        for t in TOPICS:
            for w in sorted((w for w in t.ko if " " in w), key=len, reverse=True):
                if f" {w} " in rest:
                    rest = rest.replace(f" {w} ", " ")
                    add(t)
        # 2) 낱말 단위: 사전 단어와 같거나, 사전 단어 + 조사 (예: '호수의', '바다가'). 부분 문자열은 쓰지 않음
        ko_words = sorted(((w, t) for t in TOPICS for w in t.ko if " " not in w), key=lambda x: -len(x[0]))
        for tok in re.split(r"[\s,·/]+", rest):
            tok = tok.strip(" .!?\"'()[]")
            if not tok:
                continue
            if _has_hangul(tok):
                if tok in KO_STOPWORDS:
                    continue
                hit = next((t for w, t in ko_words if tok == w or (tok.startswith(w) and tok[len(w):] in KO_PARTICLES)),
                           None)
                if hit:
                    add(hit)
                else:
                    unknown.append(tok)
            elif re.fullmatch(r"[A-Za-z][A-Za-z0-9'-]{1,30}", tok):
                words.append(tok.lower())
                groups.append((tok.lower(), (tok.lower(),)))
    q = " ".join([g[1][0] for g in groups if g[1][0] not in words] + words)
    q = " ".join(dict.fromkeys(q.split()))[:100]
    return QueryPlan(q, tuple(groups), tuple(unknown), text)


# ---------------- 후보 ----------------

@dataclass(frozen=True)
class Rendition:
    width: int
    height: int
    url: str
    size: int = 0
    fps: float = 0.0
    label: str = ""

    @property
    def is_4k(self) -> bool:
        return max(self.width, self.height) >= 3840 and min(self.width, self.height) >= 2160

    @property
    def is_fhd(self) -> bool:
        return max(self.width, self.height) >= 1920 and min(self.width, self.height) >= 1080

    @property
    def text(self) -> str:
        return f"{self.width}×{self.height}"


@dataclass(frozen=True)
class Candidate:
    provider: str
    source_id: str
    page_url: str
    title: str
    duration: float | None           # 제공처가 알려준 원본 길이 (None = 확인 불가)
    renditions: tuple[Rendition, ...]
    thumb_url: str = ""
    creator: str = ""
    creator_url: str = ""
    license_name: str = ""
    license_url: str = ""
    evidence_text: str = ""          # 주제 적합도 근거로 쓰는 메타데이터 (제목·태그·설명·주소)
    duration_source: str = ""
    notes: tuple[str, ...] = ()
    match: tuple[int, int] = (0, 0)  # (맞은 주제 수, 전체)
    match_evidence: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.source_id}"

    def best(self, res: str = RES_FHD) -> Rendition | None:
        """다운로드할 파일: 4K만/4K 우선 → 가장 큰 4K(너무 큰 8K보다 4K), 그 외 → FHD 중 가장 작은 것."""
        rs = [r for r in self.renditions if r.url]
        if not rs:
            return None
        if res in (RES_4K_ONLY, RES_4K_PREF):
            k4 = sorted((r for r in rs if r.is_4k), key=lambda r: (r.width * r.height, r.size))
            if k4:
                return k4[0]
            if res == RES_4K_ONLY:
                return None
        fhd = sorted((r for r in rs if r.is_fhd), key=lambda r: (r.width * r.height, r.size))
        if fhd:
            return fhd[0]
        return max(rs, key=lambda r: r.width * r.height)

    def preview(self) -> Rendition | None:
        """미리보기 재생용: 360p 이상 중 가장 작은 파일 (빠르게)."""
        rs = sorted((r for r in self.renditions if r.url), key=lambda r: (r.width * r.height, r.size))
        return next((r for r in rs if min(r.width, r.height) >= 360), rs[-1] if rs else None)

    @property
    def max_res(self) -> Rendition | None:
        rs = [r for r in self.renditions if r.url]
        return max(rs, key=lambda r: r.width * r.height) if rs else None


def _word_hit(term: str, text: str) -> bool:
    return re.search(r"(?<![a-z])" + re.escape(term.lower()) + r"(?![a-z])", text) is not None


def score(c: Candidate, plan: QueryPlan) -> Candidate:
    """주제 적합도: 주제마다 근거 단어가 메타데이터에 있는지. 화면·자동 판정 모두 이 근거만 쓴다 (영상 내용 인식 아님)."""
    text = (c.evidence_text or c.title).lower()
    hit = 0
    ev = []
    for name, terms in plan.groups:
        found = next((t for t in terms if _word_hit(t, text)), None)
        if found:
            hit += 1
            ev.append(f"{name}←'{found}'")
        else:
            ev.append(f"{name}✗")
    return replace(c, match=(hit, len(plan.groups)), match_evidence=", ".join(ev))


def match_label(c: Candidate) -> str:
    hit, total = c.match
    if total == 0:
        return "주제 미지정"
    if hit == total:
        return f"복합 일치 {hit}/{total}" if total > 1 else "일치"
    if hit == 0:
        return f"근거 없음 0/{total}"
    return f"부분 일치 {hit}/{total}"


# ---------------- 필터 ----------------

def duration_reason(c: Candidate, mode: str) -> str:
    """빈 문자열 = 통과. 아니면 제외 이유 (모드별로 따로 센다)."""
    if c.duration is None:
        return "길이 미확인"
    if mode == MODE_LONG:
        if c.provider == PEXELS:
            return "Pexels는 장편 제외 (업로드 최대 2분)"
        return "" if c.duration >= LONG_MIN else "3분 미만"
    if c.duration < SHORT_MIN:
        return "10초 미만"
    if c.duration > SHORT_MAX:
        return "120초 초과"
    return ""


def res_unknown(c: Candidate) -> bool:
    return bool(c.renditions) and all(not r.width or not r.height for r in c.renditions)


def res_reason(c: Candidate, res: str) -> str:
    if res_unknown(c):  # 메타데이터에 해상도가 없음: 4K만에서는 제외, 그 외에는 '미확인'으로 표시 (다운로드 후 ffprobe)
        return "해상도 미확인" if res == RES_4K_ONLY else ""
    r = c.best(res)
    if r is None:
        return "4K 아님 (메타데이터)" if res == RES_4K_ONLY else "다운로드 파일 없음"
    if not r.is_fhd and not r.is_4k:
        return "FHD 미만"
    return ""


def res_text(c: Candidate, res: str) -> str:
    if res_unknown(c):
        return "미확인 (다운로드 후 확인)"
    r = c.best(res)
    if r is None:
        return "-"
    return f"{r.text}{' 4K' if r.is_4k else ''} (메타)"


@dataclass
class ProviderResult:
    provider: str
    status: str = ""            # "정상" | "키 없음" | "오류" | "이 모드 미지원" | "선택 안 함"
    message: str = ""
    raw_count: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)


def apply_filters(cands: Sequence[Candidate], mode: str, res: str, plan: QueryPlan) -> tuple[list[Candidate], dict]:
    kept: list[Candidate] = []
    excluded: dict[str, int] = {}
    for c in cands:
        why = duration_reason(c, mode) or res_reason(c, res)
        if why:
            excluded[why] = excluded.get(why, 0) + 1
            continue
        kept.append(score(c, plan))
    return kept, excluded


def sort_results(cands: Sequence[Candidate], res: str) -> list[Candidate]:
    def k(c: Candidate):
        hit, total = c.match
        full = 1 if total and hit == total else 0
        r = c.best(res)
        k4 = 1 if (r and r.is_4k and res != RES_FHD) else 0
        return (-full, -hit, -k4)
    return sorted(cands, key=k)  # 안정 정렬: 같은 순위는 제공처 순서 그대로


# ---------------- 유틸 ----------------

def _int(v, hi=100000) -> int:
    return v if type(v) is int and 0 < v <= hi else 0


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if 0 < x <= 7 * 86400 and x == x else None


def _clean(v, n=200) -> str:
    if not isinstance(v, str):
        return ""
    v = re.sub(r"<[^>]{0,500}>", " ", v)
    return re.sub(r"\s+", " ", unescape(v)).strip()[:n]


def _hms(v) -> float | None:
    """'123.4' / '02:03' / '1:02:03' → 초."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return _num(v)
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip()
    if ":" in s:
        try:
            parts = [float(x) for x in s.split(":")]
        except ValueError:
            return None
        sec = 0.0
        for p in parts:
            sec = sec * 60 + p
        return _num(sec)
    return _num(s)


# ---------------- 어댑터 ----------------

class Adapter:
    name = ""

    def __init__(self, client: ProviderClient, key_lookup: Callable[[], str | None] | None = None):
        self.client = client
        self.key_lookup = key_lookup or (lambda: None)

    def search(self, plan: QueryPlan, mode: str, res: str, cancel: threading.Event | None = None) -> tuple[int, list[Candidate]]:
        raise NotImplementedError

    def _key(self) -> str:
        k = (self.key_lookup() or "").strip()
        if not k:
            raise NetError("KEY_MISSING")
        return k


class PexelsAdapter(Adapter):
    name = PEXELS
    ENDPOINT = "https://api.pexels.com/v1/videos/search"

    def search(self, plan, mode, res, cancel=None):
        if mode not in PROVIDER_MODES[PEXELS]:
            raise NetError("INVALID", "mode")
        params = {"query": plan.text, "per_page": 40, "page": 1, "orientation": "landscape",
                  "size": "large" if res == RES_4K_ONLY else "medium"}
        data = self.client.get_json(self.ENDPOINT, params, {"Authorization": self._key()}, cancel=cancel)
        vids = data.get("videos")
        if not isinstance(vids, list):
            raise NetError("INVALID")
        return len(vids), [c for v in vids[:80] if (c := parse_pexels(v)) is not None]


def parse_pexels(v) -> Candidate | None:
    if not isinstance(v, dict) or not _int(v.get("id"), 2**62):
        return None
    page = v.get("url")
    if not url_allowed(page, PEXELS, "page"):
        return None
    rends = []
    for f in v.get("video_files") or []:
        if not isinstance(f, dict):
            continue
        w, h, link = _int(f.get("width")), _int(f.get("height")), f.get("link")
        if not w or not h or f.get("file_type") not in ("video/mp4", None) or not url_allowed(link, PEXELS, "download"):
            continue
        rends.append(Rendition(w, h, link, 0, float(_num(f.get("fps")) or 0.0), str(f.get("quality") or "")))
    if not rends:
        return None
    slug = urlsplit(page).path.strip("/").split("/")[-1]
    words = re.sub(r"-?\d+$", "", slug).replace("-", " ").strip()
    user = v.get("user") if isinstance(v.get("user"), dict) else {}
    thumb = v.get("image") if url_allowed(v.get("image"), PEXELS, "poster") else ""
    dur = _num(v.get("duration"))
    return Candidate(PEXELS, str(v["id"]), page, (words or f"Pexels video {v['id']}").capitalize()[:120], dur,
                     tuple(rends), thumb, _clean(user.get("name"), 80),
                     user.get("url") if url_allowed(user.get("url"), PEXELS, "page") else "",
                     "Pexels License", "https://www.pexels.com/license/", words, "Pexels API (초 단위)",
                     ("제목은 Pexels 주소의 설명어로 만든 것입니다 (API에 제목 없음).",))


class PixabayAdapter(Adapter):
    name = PIXABAY
    ENDPOINT = "https://pixabay.com/api/videos/"

    def search(self, plan, mode, res, cancel=None):
        params = {"key": self._key(), "q": plan.text, "lang": "en", "video_type": "film", "safesearch": "true",
                  "per_page": 50, "page": 1, "order": "popular"}
        if res == RES_4K_ONLY:
            params["min_width"] = 3840
        else:
            params["min_width"] = 1920
        data = self.client.get_json(self.ENDPOINT, params, secret_params=("key",), cancel=cancel)
        hits = data.get("hits")
        if not isinstance(hits, list):
            raise NetError("INVALID")
        return len(hits), [c for h in hits[:200] if (c := parse_pixabay(h)) is not None]


def parse_pixabay(h) -> Candidate | None:
    if not isinstance(h, dict) or not _int(h.get("id"), 2**62):
        return None
    page = h.get("pageURL")
    if not url_allowed(page, PIXABAY, "page"):
        return None
    rends, thumb = [], ""
    vids = h.get("videos") if isinstance(h.get("videos"), dict) else {}
    for name in ("large", "medium", "small", "tiny"):
        r = vids.get(name)
        if not isinstance(r, dict):
            continue
        w, h2, url = _int(r.get("width")), _int(r.get("height")), r.get("url")
        if w and h2 and url_allowed(url, PIXABAY, "download"):
            rends.append(Rendition(w, h2, url, _int(r.get("size"), 10**12), 0.0, name))
        if not thumb and url_allowed(r.get("thumbnail"), PIXABAY, "poster"):
            thumb = r["thumbnail"]
    if not rends:
        return None
    tags = _clean(h.get("tags"), 200)
    return Candidate(PIXABAY, str(h["id"]), page, tags or f"Pixabay video {h['id']}", _num(h.get("duration")),
                     tuple(rends), thumb, _clean(h.get("user"), 80), "", "Pixabay Content License",
                     "https://pixabay.com/service/license-summary/", tags, "Pixabay API (초 단위)",
                     ("'large'라는 이름이 4K를 뜻하지는 않습니다 — 실제 크기는 메타데이터·다운로드 후 확인.",))


class CommonsAdapter(Adapter):
    name = COMMONS
    ENDPOINT = "https://commons.wikimedia.org/w/api.php"

    def search(self, plan, mode, res, cancel=None):
        params = {"action": "query", "format": "json", "formatversion": "2", "generator": "search",
                  "gsrnamespace": "6", "gsrsearch": "filetype:video " + plan.text, "gsrlimit": "30",
                  "prop": "videoinfo", "viprop": "url|size|mime|mediatype|metadata|extmetadata|user|dimensions",
                  "viurlwidth": "320",
                  "viextmetadatafilter": "LicenseUrl|LicenseShortName|UsageTerms|Artist|ImageDescription|Categories",
                  "viextmetadatalanguage": "en", "maxlag": "5"}
        data = self.client.get_json(self.ENDPOINT, params, cancel=cancel)
        if "error" in data:
            code = str((data.get("error") or {}).get("code", ""))
            raise NetError("RATE_LIMIT" if code in ("maxlag", "ratelimited") else "INVALID", code)
        pages = (data.get("query") or {}).get("pages") or []
        if not isinstance(pages, list):
            raise NetError("INVALID")
        return len(pages), [c for p in pages[:50] if (c := parse_commons(p)) is not None]


def _ext(ext, name) -> str:
    v = ext.get(name) if isinstance(ext, dict) else None
    return _clean(v.get("value"), 400) if isinstance(v, dict) else ""


def parse_commons(page) -> Candidate | None:
    if not isinstance(page, dict) or page.get("ns") != 6:
        return None
    title = page.get("title")
    infos = page.get("videoinfo")
    if not isinstance(title, str) or not title.startswith("File:") or not isinstance(infos, list) or not infos:
        return None
    vi = infos[0] if isinstance(infos[0], dict) else {}
    if vi.get("mediatype") != "VIDEO" or vi.get("mime") not in ("video/webm", "video/ogg", "application/ogg", "video/mp4"):
        return None
    url = vi.get("url")
    w, h = _int(vi.get("width")), _int(vi.get("height"))
    if not url_allowed(url, COMMONS, "download") or not w or not h:
        return None
    durs = [d for m in (vi.get("metadata") or []) if isinstance(m, dict) and m.get("name") in ("playtime_seconds", "length")
            and (d := _num(m.get("value"))) is not None]
    dur = durs[0] if durs and max(durs) - min(durs) <= 1 else None
    if dur is None:
        dur = _num(vi.get("duration"))
    ext = vi.get("extmetadata")
    lic_url = ""
    lu = (ext or {}).get("LicenseUrl") if isinstance(ext, dict) else None
    if isinstance(lu, dict) and isinstance(lu.get("value"), str):
        cand = lu["value"].strip()
        if cand.startswith("//"):
            cand = "https:" + cand
        if cand.startswith("http://"):
            cand = "https://" + cand[7:]
        lic_url = cand[:300]
    page_url = "https://commons.wikimedia.org/wiki/" + quote(title.replace(" ", "_"), safe=":()_,.-")
    thumb = vi.get("thumburl") if url_allowed(vi.get("thumburl"), COMMONS, "poster") else ""
    desc = _ext(ext, "ImageDescription")
    cats = _ext(ext, "Categories")
    name = title[5:].rsplit(".", 1)[0].replace("_", " ")
    return Candidate(COMMONS, title, page_url, name[:120], dur, (Rendition(w, h, url, _int(vi.get("size"), 10**12)),),
                     thumb, _ext(ext, "Artist")[:80] or _clean(vi.get("user"), 80), "", _ext(ext, "LicenseShortName"),
                     lic_url, f"{name} {desc} {cats}".replace("|", " "),
                     "Commons 파일 메타데이터" if dur is not None else "",
                     ("Commons 파일 메타데이터 기준 (실제 파일은 다운로드 후 ffprobe).",
                      "이용조건: " + (_ext(ext, "UsageTerms") or "개별 확인 필요")))


class ArchiveAdapter(Adapter):
    name = ARCHIVE
    SEARCH = "https://archive.org/advancedsearch.php"
    META = "https://archive.org/metadata/"
    MAX_ITEMS = 8   # 항목마다 metadata 요청 1번 → 직렬 + 간격, 결과 수 제한

    def search(self, plan, mode, res, cancel=None):
        if mode not in PROVIDER_MODES[ARCHIVE]:
            raise NetError("INVALID", "mode")
        terms = " AND ".join(f"({t})" if " " not in t else f'("{t}")' for t in plan.text.split()[:6]) or "nature"
        params = {"q": f"{terms} AND mediatype:(movies) AND licenseurl:*", "rows": "20", "page": "1", "output": "json",
                  "fl[]": ["identifier", "title", "licenseurl", "creator", "subject", "description"],
                  "sort[]": "downloads desc"}
        data = self.client.get_json(self.SEARCH, params, cancel=cancel)
        docs = ((data.get("response") or {}).get("docs")) if isinstance(data.get("response"), dict) else None
        if not isinstance(docs, list):
            raise NetError("INVALID")
        out = []
        for d in docs[: self.MAX_ITEMS]:
            if cancel is not None and cancel.is_set():
                raise NetError("CANCELLED")
            ident = d.get("identifier") if isinstance(d, dict) else None
            if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", ident):
                continue
            try:
                meta = self.client.get_json(self.META + ident, cancel=cancel)
            except NetError as e:
                if e.code in ("RATE_LIMIT", "CANCELLED"):
                    raise
                continue
            c = parse_archive(ident, d, meta)
            if c is not None:
                out.append(c)
        return min(len(docs), self.MAX_ITEMS), out  # 상위 MAX_ITEMS개만 파일 목록을 조회 (요청 수 절약)


_IA_VIDEO_FORMATS = ("h.264", "mpeg4", "512kb mpeg4", "h.264 ia", "ogg video", "matroska", "webm", "quicktime")


def _flat(v, n=300) -> str:
    if isinstance(v, list):
        v = " ".join(str(x) for x in v[:20])
    return _clean(v if isinstance(v, str) else "", n)


def parse_archive(ident: str, doc: dict, meta) -> Candidate | None:
    if not isinstance(meta, dict):
        return None
    md = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    files = meta.get("files") if isinstance(meta.get("files"), list) else []
    best = None
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("name"), str):
            continue
        fmt = str(f.get("format", "")).lower()
        name = f["name"]
        if fmt not in _IA_VIDEO_FORMATS and not name.lower().endswith((".mp4", ".webm", ".ogv", ".mkv", ".mov")):
            continue
        if "/" in name and ".." in name:
            continue
        w, h = _int(_numint(f.get("width"))), _int(_numint(f.get("height")))
        dur = _hms(f.get("length"))
        size = _int(_numint(f.get("size")), 10**12)
        url = f"https://archive.org/download/{quote(ident)}/{quote(name)}"
        if not url_allowed(url, ARCHIVE, "download"):
            continue
        score_ = (1 if name.lower().endswith(".mp4") else 0, w * h, size)
        if best is None or score_ > best[0]:
            best = (score_, Rendition(w, h, url, size, 0.0, fmt), dur)
    if best is None:
        return None
    _, rend, dur = best
    if not rend.width or not rend.height:
        rend = replace(rend, width=rend.width or 0, height=rend.height or 0)
    lic = _flat(md.get("licenseurl") or doc.get("licenseurl"), 300)
    if lic.startswith("http://"):
        lic = "https://" + lic[7:]
    title = _flat(md.get("title") or doc.get("title"), 120) or ident
    subj = _flat(md.get("subject") or doc.get("subject"), 300)
    desc = _flat(md.get("description") or doc.get("description"), 400)
    thumb = f"https://archive.org/services/img/{quote(ident)}"
    return Candidate(ARCHIVE, ident, f"https://archive.org/details/{quote(ident)}", title, dur, (rend,), thumb,
                     _flat(md.get("creator") or doc.get("creator"), 80), "", _license_name(lic), lic,
                     f"{title} {subj} {desc}", "Archive 파일 목록 length" if dur is not None else "",
                     ("Archive 파일 목록 기준 (해상도 정보가 없으면 다운로드 후 ffprobe로 확인).",))


def _numint(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _license_name(url: str) -> str:
    m = re.search(r"creativecommons\.org/(licenses|publicdomain)/([a-z-]+)/(\d\.\d)", url or "")
    if not m:
        return ""
    if m.group(1) == "publicdomain":
        return "CC0 1.0" if m.group(2) == "zero" else "Public Domain Mark 1.0"
    return f"CC {m.group(2).upper()} {m.group(3)}"


# ---------------- 검색 조정 ----------------

ADAPTERS = {PEXELS: PexelsAdapter, PIXABAY: PixabayAdapter, COMMONS: CommonsAdapter, ARCHIVE: ArchiveAdapter}


def run_search(adapters: dict[str, Adapter], providers: Sequence[str], plan: QueryPlan, mode: str, res: str,
               cancel: threading.Event | None = None, on_result: Callable[[ProviderResult], None] | None = None
               ) -> list[ProviderResult]:
    """선택한 제공처를 차례로 검색 (제공처마다 결과·오류·제외 이유를 따로). 다른 모드로 자동 대체하지 않는다."""
    out = []
    for name in PROVIDERS:
        r = ProviderResult(name)
        if name not in providers:
            r.status = "선택 안 함"
        elif mode not in PROVIDER_MODES[name]:
            r.status = "이 모드 미지원"
            r.message = PROVIDER_NOTES[name]
        elif not plan.text:
            r.status = "오류"
            r.message = "검색어가 없습니다 (주제를 고르거나 영어/아는 한국어 단어를 입력하세요)."
        else:
            try:
                raw, cands = adapters[name].search(plan, mode, res, cancel)
                r.raw_count = raw
                r.candidates, r.excluded = apply_filters(cands, mode, res, plan)
                bad_parse = raw - len(cands)
                if bad_parse > 0:
                    r.excluded["정보 불완전"] = r.excluded.get("정보 불완전", 0) + bad_parse
                r.status = "정상"
                r.message = f"받은 {raw}개 중 조건에 맞는 {len(r.candidates)}개"
                if not r.candidates:
                    r.message = ("확인된 장편 원본 없음 — " if mode == MODE_LONG else "조건에 맞는 클립 없음 — ") + r.message
            except NetError as e:
                r.status = "키 없음" if e.code == "KEY_MISSING" else "오류"
                r.message = e.message
            except Exception as e:  # 예상 못한 응답도 다른 제공처 검색을 막지 않는다
                r.status = "오류"
                r.message = f"처리 중 오류: {type(e).__name__}"
        out.append(r)
        if on_result:
            on_result(r)
        if cancel is not None and cancel.is_set():
            break
    return out
