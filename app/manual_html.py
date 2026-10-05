"""사용자 매뉴얼 HTML 만들기 (인터넷 없이 열림 · 외부 CDN/글꼴/스크립트 없음).

help_content(프로그램 안 도움말)와 같은 내용으로 docs/ 아래 3개 파일을 만든다:
  초보자_빠른시작.html · 사용자_매뉴얼.html · 문제해결.html
사용: python -m app.manual_html  (docs/를 다시 만든다)
"""
from __future__ import annotations

import html
from pathlib import Path

from . import help_content as hc

B = hc.B
CSS = """body{font-family:'Malgun Gothic','Segoe UI',sans-serif;max-width:860px;margin:24px auto;padding:0 16px;
line-height:1.7;color:#1d1d1f;background:#fff}h1{font-size:26px;border-bottom:3px solid #2f6fdf;padding-bottom:6px}
h2{font-size:20px;margin-top:32px;border-left:6px solid #2f6fdf;padding-left:10px}pre{white-space:pre-wrap;
font-family:inherit;background:#f5f7fb;border:1px solid #dde3ee;border-radius:8px;padding:12px 14px}
nav a{display:inline-block;margin:2px 10px 2px 0}.btn{background:#eef3fd;border:1px solid #b9cdf3;border-radius:5px;
padding:0 5px;white-space:nowrap}.note{background:#fff8e6;border:1px solid #f0d28a;border-radius:8px;padding:10px 14px}
.flow{font-weight:bold;background:#eef7ee;border:1px solid #b8dcb8;border-radius:8px;padding:10px 14px}
footer{margin-top:40px;color:#666;font-size:13px;border-top:1px solid #ddd;padding-top:8px}
@media (prefers-color-scheme: dark){body{background:#16181c;color:#e8e8e8}pre{background:#20242b;border-color:#333a45}
.btn{background:#1f2a40;border-color:#36507f}.note{background:#2b2616;border-color:#6b5a2a}.flow{background:#1b2a1b;border-color:#355235}}"""

EXTRA = {
    "intro": ("1. 프로그램 소개", f"""{hc.APP_NAME}는 세 가지 일을 한 프로그램에서 합니다.

{B('card_long')} — {hc.CARD_HELP['long']}
{B('card_live')} — {hc.CARD_HELP['live']}
{B('card_upload')} — {hc.CARD_HELP['upload']}

메인 화면 위쪽의 큰 카드 3개 중 하나를 누르면 됩니다. 처음이면 {B('guide')}를 누르세요."""),
    "first": ("2. 처음 실행", f"""처음 실행하면 '처음 사용하시나요?' 창이 나옵니다.

{B('setup')}: 4단계로 준비합니다.
  STEP 1 기본 프로그램 점검 (FFmpeg·저장 공간·시간대·인터넷 자동 확인)
  STEP 2 YouTube 채널 연결 ([한국 채널] / [일본 채널] / [직접 설정])
  STEP 3 기본 업로드 설정 (기본 예약 시간)
  STEP 4 완료
{B('quick')}: 이 매뉴얼의 '5분 빠른 시작'을 엽니다.
{B('later')}: 바로 메인 화면으로 갑니다.

다음 실행부터는 자동으로 나오지 않습니다. 언제든 메인의 {B('guide')}로 다시 볼 수 있습니다.
메인의 {B('check')}을 누르면 무엇이 준비됐고 무엇을 고쳐야 하는지 한눈에 보입니다.
'초보자 모드'(기본 켜짐)에서는 어려운 기술 용어와 고급 설정을 숨깁니다."""),
    "batch": ("6. 여러 영상 한꺼번에 예약", f"""1. {B('add_folder')}로 폴더를 고릅니다. 영상은 이름순(001, 002 … 010)으로 들어갑니다.
2. 첫 날짜·시간과 간격(매일 / 평일 / 2일마다 / 매주 / N일마다)을 고릅니다.
3. {B('preview')}에서 날짜가 하나씩 정해진 목록을 확인합니다.
4. {B('confirm_start')}을 누르면 대기열에 넣고 바로 업로드를 시작합니다.

예: 영상 10개 · 첫 시작 10월 6일 19:00 · 매일 → 10월 6일 ~ 10월 15일 매일 19:00에 하나씩 공개됩니다."""),
    "thumb": ("7. 썸네일 자동 연결", """영상과 같은 이름의 사진을 자동으로 연결합니다.

  001.mp4 ↔ 001.jpg / 001.jpeg / 001.png
  없으면 001_thumbnail.jpg 같은 이름
  그래도 없으면 상세 설정의 '썸네일 없을 때' 방식 (고정 1개 / 폴더에서 순서대로)

같은 이름 후보가 2개 이상이면 마음대로 고르지 않고 '썸네일 후보 2개 - 하나를 선택하세요'로 표시합니다. [썸네일 직접 지정]으로 고르세요. 고르지 않아도 영상은 썸네일 없이 올라갑니다.
썸네일이 없어도 영상 업로드는 됩니다."""),
    "live_schedule": ("9. 예약 LIVE", f"""{B('card_live')} 카드 아래의 '예약 LIVE'를 누릅니다.

• 한 번만 / 매일 / 평일 / 매주 / 요일 선택
• 앞으로 7일 동안, 최대 7개까지 미리 예약합니다.
• [저장된 규칙 부족분 보충]을 누르면 다음 회차를 이어서 만듭니다.
• 목록에서 지워도 YouTube 예약은 지워지지 않습니다 (YouTube Studio에서 관리)."""),
    "first_comment": ("10. 첫 댓글 자동등록", f"""예약 업로드의 {B('detail')}에서 '{hc.BUTTONS['first_comment']}'를 켜고 댓글 내용을 씁니다. [댓글 템플릿]에서 예시 문장을 고를 수 있습니다.

• 예약 영상은 공개되기 전(비공개)에는 댓글을 달 수 없습니다. 공개된 뒤 약 1분 후에 자동으로 답니다.
• 지금 공개/일부공개로 올린 영상은 처리가 끝나면 바로 답니다.
• 비공개로 올린 영상은 공개로 바꿀 때까지 기다립니다.
• 프로그램이 꺼져 있었다면 다시 켤 때 밀린 첫 댓글을 확인해 답니다.
• 미리보기에 '10/10 자동등록 예정 (공개 후)'처럼 표시됩니다. 실제로 달리기 전에는 '완료'로 표시하지 않습니다."""),
    "reply": ("11. 댓글 자동답글", f"""{B('comments')} → 채널 선택 → '자동답글'

• 사용 안 함 / 검토 후 답글(기본) / 자동답글 (안전형)
• {hc.TOOLTIPS['auto_reply']}
• 하루 최대 10 / 20 / 30개, 답글 사이 1분 이상. 같은 문장을 연달아 쓰지 않습니다.
• 답글 문구는 채널마다 5개 이상 넣는 것을 권장합니다. 구독·좋아요 요청 같은 홍보 문구는 쓰지 마세요.
• 이미 답한 댓글, YouTube Studio에서 직접 답한 댓글, 내 댓글에는 다시 답하지 않습니다."""),
    "exit": ("12. 안전하게 종료", """• 업로드나 댓글 자동 확인이 진행 중일 때 종료하면 '현재 작업이 진행 중입니다' 창이 나옵니다.
  프로그램을 종료하면 업로드와 댓글 자동 확인은 멈춥니다. 다음에 [▶ 예약 업로드 시작]을 누르면 받은 곳부터 이어서 올립니다.
• 내 PC LIVE 중에 종료하면 LIVE를 정상 종료한 뒤 닫습니다. 무료 Cloud LIVE는 기본으로 PC만 종료하고 방송은 계속됩니다.
• 업로드가 끝난 예약 영상은 컴퓨터를 꺼도 YouTube가 정한 시간에 공개합니다."""),
}


def _fmt(text: str) -> str:
    """[버튼] 표기를 눈에 띄게. 나머지는 이스케이프."""
    out = html.escape(text)
    for name in sorted(hc.BUTTONS.values(), key=len, reverse=True):
        esc = html.escape(f"[{name}]")
        out = out.replace(esc, f'<span class="btn">{html.escape(name)}</span>')
    return out


def _page(title: str, sections: list[tuple[str, str, str]], intro: str = "") -> str:
    nav = " ".join(f'<a href="#{k}">{html.escape(t)}</a>' for k, t, _ in sections)
    body = "\n".join(f'<h2 id="{k}">{html.escape(t)}</h2>\n<pre>{_fmt(b)}</pre>' for k, t, b in sections)
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><h1>{html.escape(title)}</h1>
{f'<p class="note">{_fmt(intro)}</p>' if intro else ''}
<nav>{nav}</nav>
{body}
<footer>{html.escape(hc.APP_NAME)} · Manual version {hc.MANUAL_VERSION} · 이 파일은 인터넷 없이 열립니다.</footer>
</body></html>
"""


def quick_start_html() -> str:
    t = hc.topic("quick")
    flow = "STEP 1 채널 선택 → STEP 2 영상 선택 → STEP 3 날짜 선택 → STEP 4 미리보기 → STEP 5 예약 시작"
    return _page("초보자 빠른 시작", [("quick", t.title, t.body), ("flow", "예약 업로드 흐름", flow),
                                    ("help", "더 알고 싶을 때", f"프로그램 안의 {B('help')}에서 기능별 설명과 문제 해결을 볼 수 있습니다.\n"
                                                               f"각 창 위쪽의 {B('usage')}는 그 창의 사용법만 짧게 보여줍니다.")],
                 intro=f"처음이면 프로그램 메인의 {B('guide')} → {B('setup')}부터 하세요.")


def manual_html() -> str:
    t = {x.key: x for x in hc.TOPICS}
    sections = [
        ("intro", *EXTRA["intro"]), ("first", *EXTRA["first"]),
        ("channels", "3. YouTube 채널 연결", t["channels"].body), ("long", "4. 영상 늘리기", t["long"].body),
        ("upload", "5. 예약 업로드", t["upload"].body), ("batch", *EXTRA["batch"]), ("thumb", *EXTRA["thumb"]),
        ("live", "8. LIVE 방송", t["live"].body), ("live_schedule", *EXTRA["live_schedule"]),
        ("first_comment", *EXTRA["first_comment"]), ("reply", *EXTRA["reply"]), ("exit", *EXTRA["exit"]),
        ("trouble", "13. 오류 해결", t["trouble"].body), ("faq", "14. FAQ", t["faq"].body),
    ]
    return _page(f"{hc.APP_NAME} 사용자 매뉴얼", sections, intro=hc.QUICK_START.splitlines()[0] + " — 아래 '5. 예약 업로드' 참고")


def trouble_html() -> str:
    t = {x.key: x for x in hc.TOPICS}
    errors = """오류 창은 항상 두 부분으로 나옵니다.

  무슨 문제가 생겼나요?  예: YouTube 채널이 다릅니다.
  무엇을 하면 되나요?    예: 예약 업로드에서 올바른 채널을 다시 선택하세요.  [채널 다시 선택]

기술적인 내용(HTTP 번호 등)은 [자세히 보기]를 눌렀을 때만 보입니다."""
    return _page("문제 해결", [("how", "오류 창 읽는 법", errors), ("trouble", t["trouble"].title, t["trouble"].body),
                              ("faq", t["faq"].title, t["faq"].body)])


FILES = {"초보자_빠른시작.html": quick_start_html, "사용자_매뉴얼.html": manual_html, "문제해결.html": trouble_html}


def build(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, fn in FILES.items():
        p = out_dir / name
        p.write_text(fn(), encoding="utf-8", newline="\n")
        written.append(p)
    return written


if __name__ == "__main__":
    for p in build(Path(__file__).resolve().parent.parent / "docs"):
        print(p)
