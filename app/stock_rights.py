"""⑤ 권리 검토 — 보수적인 '참고 분류'이며 법적 사용 허가를 보증하지 않는다.

상태: 사용 검토 가능 / 저작자 표시 필요 / 추가 검토 필요 / 상업적 이용 차단 / 확인 불가
- NC·ND·권리 불명은 기본 다운로드와 상업 제작 사용을 막는다.
- CC0·CC BY라도 실제 촬영물의 권리(인물·상표·건물·음악 등 제3자 권리)는 사용자가 따로 확인해야 한다.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

OK = "사용 검토 가능"
ATTRIBUTION = "저작자 표시 필요"
REVIEW = "추가 검토 필요"
BLOCKED = "상업적 이용 차단"
UNKNOWN = "확인 불가"
STATES = (OK, ATTRIBUTION, REVIEW, BLOCKED, UNKNOWN)
DOWNLOADABLE = frozenset({OK, ATTRIBUTION, REVIEW})

PEXELS_LICENSE = "https://www.pexels.com/license/"
PIXABAY_LICENSE = "https://pixabay.com/service/license-summary/"
THIRD_PARTY = "제3자 권리(얼굴이 보이는 인물·상표·로고·건물·작품·음악) 미확인 — 사용 전 직접 확인"
DISCLAIMER = "이 분류는 참고용이며 법적 사용 허가를 보증하지 않습니다."


@dataclass(frozen=True)
class RightsReview:
    state: str
    reasons: tuple[str, ...]
    license_name: str
    license_url: str
    license_version: str
    attribution_required: bool
    attribution_text: str
    commercial_status: str
    third_party: str = THIRD_PARTY
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"))

    @property
    def downloadable(self) -> bool:
        return self.state in DOWNLOADABLE

    def record(self, *, provider: str, source_url: str, creator: str, creator_url: str = "") -> dict:
        d = asdict(self)
        d.update(provider=provider, source_url=source_url, creator=creator, creator_url=creator_url,
                 disclaimer=DISCLAIMER, reasons=list(self.reasons))
        return d


def _cc(url: str, name: str) -> tuple[str, str] | None:
    """(종류, 버전). 종류: zero / pdm / by / by-sa / nc-nd (NC 또는 ND 포함)."""
    text = f"{url} {name}".lower()
    if "publicdomain/zero" in text or re.search(r"\bcc0\b", text):
        return "zero", "1.0"
    if "publicdomain/mark" in text or "public domain" in text or re.search(r"\bpd\b|\bpdm\b", text):
        return "pdm", "1.0"
    m = re.search(r"licenses/([a-z-]+)/(\d\.\d)", text) or re.search(r"\bcc[ -]((?:by)(?:[- ](?:sa|nc|nd))*)[ -](\d\.\d)", text)
    if m:
        kind = m.group(1).replace(" ", "-")
        ver = m.group(2)
        if "nc" in kind or "nd" in kind:
            return "nc-nd", ver
        return ("by-sa" if "sa" in kind else "by"), ver
    if re.search(r"\bcc[ -]by[ -]n[cd]", text):
        return "nc-nd", ""
    return None


def review(provider: str, license_name: str, license_url: str, creator: str, title: str = "",
           source_url: str = "") -> RightsReview:
    lname = (license_name or "").strip()
    lurl = (license_url or "").strip()
    host = (urlsplit(lurl).hostname or "").lower().removeprefix("www.") if lurl else ""
    by = creator.strip() or "제작자 미상"
    attr_basic = f"\"{title or '영상'}\" — {by} / {provider}"
    if provider == "Pexels":
        return RightsReview(
            OK, ("Pexels 라이선스: 무료·변형 사용 가능, 출처 표시는 필수 아님(권장).",
                 "무변형 단독 재배포·판매 금지 조건 — 여러 영상 조합·음악 결합 등 실제 변형 여부를 확인하세요.",
                 "영상에 보이는 인물·상표의 상업적 이용은 별도 확인이 필요합니다."),
            "Pexels License", PEXELS_LICENSE, "", False, f"Video by {by} on Pexels", "검토 필요 (조건부)")
    if provider == "Pixabay":
        return RightsReview(
            OK, ("Pixabay Content License: 무료·상업 사용 가능, 출처 표시는 필수 아님(권장).",
                 "무변형 단독 배포·판매 금지 및 인물·상표·브랜드가 보이는 콘텐츠 제한 조건 — 실제 사용 형태를 확인하세요."),
            "Pixabay Content License", PIXABAY_LICENSE, "", False, f"Video by {by} from Pixabay", "검토 필요 (조건부)")
    if not lurl and not lname:
        return RightsReview(UNKNOWN, ("라이선스 정보가 없습니다 — 기본 다운로드 차단.",), "", "", "", False, "",
                            "차단 (권리 불명)")
    if lurl and host not in ("creativecommons.org", "commons.wikimedia.org", "gnu.org", "en.wikipedia.org",
                             "wikimedia.org") and "creativecommons" not in lname.lower():
        return RightsReview(UNKNOWN, ("공식 라이선스로 확인되지 않은 주소입니다 — 기본 다운로드 차단.",), lname, lurl, "",
                            False, "", "차단 (권리 불명)")
    cc = _cc(lurl, lname)
    if cc is None:
        if "gfdl" in lname.lower() or "gnu.org" in host:
            return RightsReview(REVIEW, ("GFDL: 영상에 쓰기 어려운 조건(라이선스 전문 동봉 등)이 있어 추가 검토가 필요합니다.",),
                                lname, lurl, "", True, f"{attr_basic} ({lname})", "검토 필요")
        return RightsReview(UNKNOWN, (f"지원하지 않는 라이선스 표기입니다: {lname or lurl} — 기본 다운로드 차단.",), lname,
                            lurl, "", False, "", "차단 (권리 불명)")
    kind, ver = cc
    if kind == "nc-nd":
        return RightsReview(BLOCKED, ("비영리(NC) 또는 변경금지(ND) 조건 — 수익 채널·편집 영상에 쓸 수 없어 차단합니다.",),
                            lname, lurl, ver, True, "", "차단 (NC/ND)")
    if kind in ("zero", "pdm"):
        return RightsReview(OK, ("CC0/퍼블릭 도메인 표시 — 표시가 정확한지(업로더가 실제 권리자인지)는 확인할 수 없습니다.",),
                            lname or ("CC0 1.0" if kind == "zero" else "Public Domain Mark 1.0"), lurl, ver, False,
                            attr_basic + " (Public Domain)", "검토 필요 (제3자 권리)")
    if kind == "by-sa":
        return RightsReview(REVIEW, ("CC BY-SA: 저작자 표시 + 편집한 결과물도 같은 라이선스로 공개해야 할 수 있어 추가 검토가 필요합니다.",),
                            lname or f"CC BY-SA {ver}", lurl, ver, True, f"{attr_basic} ({lname or 'CC BY-SA ' + ver}, {lurl})",
                            "검토 필요 (동일조건)")
    return RightsReview(ATTRIBUTION, ("CC BY: 영상 설명란 등에 저작자·출처·라이선스·변경 사실을 표시해야 합니다.",),
                        lname or f"CC BY {ver}", lurl, ver, True, f"{attr_basic} ({lname or 'CC BY ' + ver}, {lurl}) — 편집함",
                        "검토 필요 (저작자 표시)")
