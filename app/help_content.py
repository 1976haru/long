"""내장 도움말/사용자 매뉴얼 내용 (Tk 없음). 프로그램 안 도움말과 docs/*.html이 같은 내용을 쓴다.

버튼 이름은 BUTTONS에 모아 두고, 실제 화면 문구와 같은지 테스트로 확인한다.
"""
from __future__ import annotations

from dataclasses import dataclass

APP_NAME = "YouTube Playlist Studio"
MANUAL_VERSION = "v1.3"

# 화면에 실제로 있는 버튼/메뉴 이름 (매뉴얼은 이 이름 그대로 쓴다)
BUTTONS = {
    "card_long": "① 영상 늘리기", "card_live": "② 실시간 스트리밍", "card_upload": "③ 예약 업로드",
    "guide": "? 처음 사용 가이드", "help": "? 도움말", "check": "⚙ 설정 점검", "comments": "댓글 관리",
    "channels": "YouTube 채널 관리", "new_channel": "＋ 새 채널", "connect": "Google 계정 연결",
    "pick_oauth": "Google 연결 파일 선택", "what_oauth": "이 파일이 뭔가요?",
    "pick_videos": "영상 선택", "add_folder": "폴더 한꺼번에 추가", "last_folder": "최근 폴더 다시 불러오기",
    "detail": "▼ 제목·설명·태그·썸네일 상세 설정", "preview": "▶ 미리보기 후 대기열에 추가",
    "confirm_start": "맞습니다. 예약 업로드 시작", "reselect": "채널 다시 선택", "start_upload": "▶ 예약 업로드 시작",
    "stop_upload": "■ 중지 (나중에 이어 올리기)", "add_set": "＋ 영상 추가", "add_job": "＋ 현재 설정을 대기열에 추가",
    "start_long": "▶ 대기열 자동 시작", "send_upload": "③ 예약 업로드로 보내기", "live_start": "▶ 24H LIVE 시작",
    "live_preset": "초보자 추천 설정", "live_schedule": "예약 LIVE", "check_comments": "지금 새 댓글 확인",
    "first_comment": "공개 후 첫 댓글 자동등록", "copy_diag": "진단 정보 복사", "usage": "? 사용법",
    "setup": "처음부터 설정하기", "quick": "5분 빠른 사용법", "later": "나중에 하기",
    "first_connect": "Google 계정 처음 연결하기", "have_file": "있어요 - 파일 선택",
    "first_time": "처음이에요 - 만드는 방법 보기", "open_cloud": "Google Cloud 열기",
    "open_official": "Google 공식 설명 열기", "copy_steps": "설정 순서 복사", "pick_downloaded": "다운로드한 연결 파일 선택",
    "playlist_new": "+ 새로 만들기", "playlist_refresh": "새로고침", "playlist_none": "재생목록에 넣지 않음",
    "playlist_retry": "재생목록만 다시 추가", "playlist_multi": "여러 재생목록에 추가",
    "jp_setup": "무료 일본어 도우미 설정", "jp_external": "일본 영상 댓글 도우미",
}

GOOGLE_CLOUD_URL = "https://console.cloud.google.com/"
GOOGLE_OFFICIAL_URL = "https://developers.google.com/youtube/v3/guides/auth/installed-apps"
GOOGLE_FILE_INTRO = ("이 파일은 YouTube Playlist Studio가 내 YouTube 채널에 업로드할 수 있도록\n"
                     "Google에게 허락받기 위한 연결 파일입니다.\n"
                     "Google 비밀번호가 들어 있는 파일은 아닙니다.\n\n"
                     "하지만 다른 사람에게 보내거나 GitHub 같은 인터넷에 올리지 마세요.")
GOOGLE_FILE_STEPS = (
    ("Google Cloud 열기", "Google 계정으로 로그인하고 프로젝트를 하나 만들거나 기존 프로젝트를 선택하세요."),
    ("YouTube Data API 사용", "'API 및 서비스'에서 YouTube Data API v3를 찾아 [사용] 또는 [Enable]을 누르세요."),
    ("Google 앱 설정", "Google Auth Platform(또는 OAuth 동의 화면)에서 앱 이름과 지원 이메일을 정하세요.\n"
                     "혼자 쓰는 테스트라면 'Testing'(테스트) 상태로 둬도 됩니다. 이때는 사용할 Google 계정을 "
                     "'Test users'(테스트 사용자)에 추가해야 할 수 있습니다."),
    ("OAuth Client 만들기", "Clients → Create client(클라이언트 만들기) → Application type: Desktop app(데스크톱 앱)\n"
                           "이름 예: YouTube Playlist Studio → 만들기"),
    ("JSON 다운로드", "만든 Desktop client의 JSON 파일을 내려받으세요 (보통 '다운로드' 폴더에 저장됩니다)."),
    ("프로그램으로 돌아오기", "이 프로그램에서 [다운로드한 연결 파일 선택]을 누르고 방금 받은 파일을 고르세요."),
)


def google_steps_text() -> str:
    return "\n\n".join(f"STEP {i}. {t}\n{d}" for i, (t, d) in enumerate(GOOGLE_FILE_STEPS, 1))


def B(key: str) -> str:
    """매뉴얼 문장 안의 버튼 이름 표기: [버튼]."""
    return f"[{BUTTONS[key]}]"


@dataclass(frozen=True)
class HelpTopic:
    key: str
    title: str
    body: str
    keywords: tuple = ()


QUICK_START = f"""5분 만에 예약 업로드하기

1. 메인 화면 위쪽의 {B('card_upload')} 카드를 누르세요.

2. YouTube 채널을 선택하세요.
   처음이면 {B('channels')} → {B('new_channel')} → {B('connect')} 순서로 연결합니다.
   Google 연결 파일이 없으면 {B('first_connect')} → {B('first_time')}을 누르세요.

3. {B('add_folder')}를 누르고 영상이 있는 폴더를 고르세요.
   같은 이름의 사진(001.mp4 ↔ 001.jpg)이 있으면 썸네일로 자동 연결됩니다.

4. 첫 날짜와 시간을 고르세요. (예: 내일 19:00, 매일)

5. {B('preview')}를 누르세요.

6. 채널 이름과 날짜가 맞으면 {B('confirm_start')}을 누르세요.

끝. 창을 닫아도 프로그램이 켜져 있는 동안 업로드는 계속됩니다."""

FEATURE_USAGE = {  # 각 창의 [? 사용법] — 5단계 이하
    "upload": ("예약 업로드 사용법", [
        "YouTube 채널을 선택하세요.", f"영상 또는 폴더를 선택하세요 ({B('pick_videos')} / {B('add_folder')}).",
        "첫 날짜와 시간, 간격(매일·평일 등)을 선택하세요.", f"{B('preview')}에서 채널 이름과 날짜를 확인하세요.",
        f"{B('confirm_start')}을 누르세요."]),
    "long": ("영상 늘리기 사용법", [
        f"{B('add_set')}으로 완성된 SET 영상을 넣으세요.", "몇 회 반복할지(회차) 또는 몇 시간짜리로 만들지 고르세요.",
        f"{B('add_job')}를 누르세요.", f"{B('start_long')}을 누르세요.",
        f"다 만들어지면 작업을 선택하고 {B('send_upload')}를 누르면 바로 예약 업로드할 수 있습니다."]),
    "live": ("실시간 LIVE 사용법", [
        "위쪽 채널 카드에서 송출할 채널(채널 A 또는 두 번째 송출 채널)의 [이 채널 설정하기]를 누르세요.",
        "① LIVE 영상에서 영상 1개(단일 영상) 또는 여러 개(Playlist)를 고르세요.",
        "③ YouTube 송출의 Stream Key 칸에 그 채널의 Stream Key만 붙여넣으세요. 서버 주소는 자동입니다.",
        f"'준비 상태'가 모두 ☑이면 {B('live_start')} 또는 카드의 [▶ 시작]을 누르세요.",
        "두 채널을 함께 하려면 다른 채널 카드로 바꿔 2~4를 한 번 더 하거나 [▶▶ 두 채널 동시 시작]을 누르세요 (최대 2채널)."]),
    "comments": ("댓글 관리 사용법", [
        "YouTube 채널을 선택하세요.", f"{B('check_comments')}을 누르면 새 댓글을 가져옵니다.",
        "답글을 달 댓글을 고르고 [추천 답글 사용] 또는 [답글 작성]을 누르세요.",
        "자동답글은 기본으로 꺼져 있습니다 ('검토 후 답글'). 필요할 때만 '자동답글 (안전형)'을 켜세요.",
        "프로그램이 꺼져 있으면 댓글을 달 수 없습니다. 다시 켜면 밀린 작업을 확인합니다."]),
    "channels": ("YouTube 채널 연결 사용법", [
        f"{B('new_channel')}을 누르고 별칭·언어·시간대를 정하세요.",
        f"{B('pick_oauth')}으로 Google 연결 파일을 고르세요. 없으면 {B('what_oauth')} → 만드는 방법을 따라 하세요.",
        f"{B('connect')}을 누르면 브라우저가 열립니다.", "Google 계정으로 로그인하고 YouTube 채널을 고른 뒤 [허용]을 누르세요.",
        "연결된 채널 이름이 화면에 나오면 완료입니다."]),
}

TOOLTIPS = {
    "timezone": "예약 시간이 계산되는 지역입니다.\n한국 채널은 Asia/Seoul, 일본 채널은 Asia/Tokyo를 사용하세요.",
    "kids": "어린이를 주 시청자로 만든 영상인지 YouTube에 알려주는 설정입니다.\n잘 모르면 끈 채로 두세요.",
    "schedule": "예약 공개를 고르면 영상은 먼저 비공개로 올라가고, 정한 날짜·시간에 YouTube가 자동으로 공개합니다.",
    "thumb": "같은 이름의 사진(001.mp4 ↔ 001.jpg)이 없을 때만 사용합니다.",
    "auto_reply": ("시청자가 남긴 간단한 감사·응원 댓글에 프로그램이 저장된 문구로 답글을 답니다.\n"
                   "질문이나 링크가 있는 댓글은 자동으로 답하지 않고 검토 목록에 둡니다."),
    "first_comment": "예약 영상은 공개되기 전에는 댓글을 달 수 없어서, 공개된 뒤 자동으로 첫 댓글을 답니다.",
    "beginner": "초보자 모드에서는 어려운 기술 용어와 고급 설정을 숨깁니다. 끄면 고급 설정이 모두 보입니다.",
    "playlist": "업로드한 영상을 같은 주제별로 모아두는 YouTube 재생목록입니다.\n선택하지 않아도 업로드는 됩니다.",
}

CARD_HELP = {
    "long": "여러 곡 영상을 연결해서 1시간·3시간·10시간 같은 긴 영상을 만들 때 사용합니다.",
    "live": "만든 영상을 YouTube에서 계속 반복 방송할 때 사용합니다.",
    "upload": "여러 영상을 날짜와 시간을 정해서 YouTube에 자동으로 올립니다.",
}

TOPICS = (
    HelpTopic("quick", "① 5분 빠른 시작", QUICK_START, ("시작", "처음", "업로드", "예약")),
    HelpTopic("long", "② 영상 늘리기", f"""{CARD_HELP['long']}

1. {B('add_set')}으로 CapCut 등에서 완성한 SET 영상(MP4)을 넣습니다. 여러 개면 목록 순서대로 1바퀴가 1회차입니다.
2. 회차 기준(권장) 또는 시간 기준을 고릅니다. 회차 기준은 SET 중간에서 끊기지 않습니다.
3. 저장 폴더와 파일 이름을 확인하고 {B('add_job')}를 누릅니다 (최대 5개).
4. {B('start_long')}을 누르면 1개씩 순서대로 만듭니다.
5. 화질을 지키기 위해 다시 인코딩하지 않습니다. SET 영상들의 해상도·FPS·코덱이 같아야 합니다.
6. 다 만든 영상은 작업을 선택하고 {B('send_upload')}를 누르면 예약 업로드 창으로 바로 넘어갑니다.""",
              ("영상", "늘리기", "회차", "SET", "FFmpeg", "반복")),
    HelpTopic("upload", "③ 예약 업로드", f"""{CARD_HELP['upload']}

STEP 1 채널 선택 → STEP 2 영상 선택 → STEP 3 날짜 선택 → STEP 4 미리보기 → STEP 5 예약 시작

• {B('pick_videos')}: 영상 몇 개만 고를 때
• {B('add_folder')}: 폴더 안 영상을 이름순(001, 002 …)으로 한꺼번에. 파일은 옮기거나 지우지 않습니다.
• {B('last_folder')}: 마지막으로 쓴 폴더를 다시 읽습니다.
• 썸네일: 001.mp4 ↔ 001.jpg / 001.png 처럼 같은 이름의 사진을 자동으로 연결합니다. 후보가 2개 이상이면 고르지 않고 표시합니다. 썸네일이 없어도 업로드는 됩니다.
• 여러 영상 간격: 매일 / 평일 / 2일마다 / 매주 / N일마다. 날짜·시간은 그 채널의 시간대 기준입니다.
• {B('detail')}: 제목·설명·태그·썸네일·첫 댓글. 채널마다 템플릿으로 저장할 수 있습니다.
• {B('preview')}: 실제 YouTube 채널 이름을 한 번 더 확인합니다. 채널이 다르면 추가할 수 없습니다.
• 업로드 직전마다 채널을 다시 확인하고, 다르면 업로드하지 않습니다.
• 프로그램이 켜져 있는 동안 업로드는 계속됩니다. 중지하면 다음에 받은 곳부터 이어서 올립니다.""",
              ("업로드", "예약", "폴더", "썸네일", "날짜", "시간대", "미리보기")),
    HelpTopic("live", "④ 실시간 LIVE", f"""{CARD_HELP['live']}

• {B('card_live')} 카드를 누르면 LIVE 창이 열립니다.
• 무료 Cloud(권장): PC를 꺼도 방송이 계속됩니다. 처음 한 번 [처음 설정 도우미]가 필요합니다.
• 내 PC: Cloud를 쓸 수 없을 때. PC를 끄면 방송도 끝납니다.
• 잘 모르면 {B('live_preset')}을 누르세요: 재인코딩 없이 그대로 송출, 11시간 50분 안전 종료, 끊기면 자동 재연결.
• {B('live_schedule')}: 앞으로 7일 동안의 LIVE를 미리 예약합니다 (② 카드의 '예약 LIVE').
• LIVE 종료 전에는 확인 창이 나옵니다.

채널 1개 시작하기
1. LIVE 창 위쪽 채널 카드에서 [이 채널 설정하기] → 2. 영상/Playlist 선택 → 3. Stream Key 붙여넣기
→ 4. '준비 상태'가 모두 ☑ → 5. [▶ 시작]. ② 카드의 '▶ (채널) 시작'을 누르면 간단 시작 마법사가 순서대로 안내합니다.

2채널 동시 시작하기 (예: 채널 A 시니어 + 두 번째 송출 채널 도쿄칠)
• 두 번째 카드의 [두 번째 송출 채널 ▼]에서 함께 송출할 채널을 고르세요. 채널이 없으면 [＋ 두 번째 채널 만들기]를 누르세요.
• 채널은 여러 개 만들 수 있고, 두 번째 카드에 어떤 채널을 보여 줄지는 언제든 바꿀 수 있습니다 (바꿔도 송출 중인 LIVE는 멈추지 않음).
• 채널마다 영상과 Stream Key가 따로 저장됩니다.
• 채널 A를 시작한 뒤 두 번째 카드로 바꿔 같은 순서로 시작하거나 [▶▶ (채널 A) + (두 번째 채널) 동시 시작]을 누르세요.
• 채널 A의 이름은 카드의 [이름 바꾸기]로 '시니어 채널'처럼 바꿀 수 있습니다 (송출 설정은 그대로).
• 현재는 최대 2개 채널까지 동시에 송출할 수 있습니다. 세 번째는 시작되지 않습니다.
• 채널 A를 중지해도 두 번째 채널은 계속 송출됩니다 (반대도 같음).

⑦ YouTube 방송 정보 (채널별)
• 제목·설명·태그·썸네일·카테고리·YouTube 재생목록·공개 상태를 채널마다 따로 저장합니다 (다른 채널과 섞이지 않음).
• [기본값 저장]은 이 PC에만 저장합니다. YouTube에는 [YouTube에 적용…]을 눌렀을 때만 반영됩니다.
• Stream Key 직접 송출: YouTube 연결이 있어야 적용할 수 있고, 진행 중/예정 방송 목록에서 적용할 방송을 직접 고릅니다.
• YouTube API 자동 세션: LIVE를 시작하면 이 방송 정보로 새 방송을 만듭니다.
• 'YouTube 재생목록'은 YouTube 채널 안의 재생목록입니다. ①의 '송출 영상 Playlist'(반복할 MP4 목록)와 다릅니다.
• 일부 항목(예: 썸네일)만 실패하면 나머지는 그대로 두고 [실패한 항목만 다시 적용]으로 다시 시도합니다.

서버 주소와 Stream Key는 다릅니다
• 서버 주소: YouTube가 영상을 받는 주소 (예: rtmp://a.rtmp.youtube.com/live2). 초보자 모드에서는 자동입니다.
• Stream Key: 어느 채널 방송인지 알려주는 비밀 번호 (예: xxxx-xxxx-xxxx-xxxx). 채널마다 다르며 Stream Key 칸에만 넣습니다.
• 서버 주소 칸에 Stream Key를 붙여 넣으면 시작 전에 경고합니다. 직접 입력은 '고급 모드'에서만 할 수 있습니다.

YouTube 계정 연결이 꼭 필요한가요?
• Stream Key 방식은 YouTube 계정 연결 없이도 송출할 수 있습니다. 예약 LIVE와 API 자동 세션은 연결이 필요합니다.""",
              ("LIVE", "라이브", "방송", "스트리밍", "Cloud", "Stream Key", "서버 주소", "2채널", "채널 B")),
    HelpTopic("comments", "⑤ 댓글 자동화", f"""{B('comments')}에서 사용합니다.

• 첫 댓글 자동등록: 예약 업로드의 {B('detail')} → '{BUTTONS['first_comment']}'. 예약 영상은 공개된 뒤 약 1분 후에 첫 댓글을 답니다.
• 새 댓글 확인: 프로그램이 켜져 있을 때 10분마다, 또는 {B('check_comments')}.
• 자동답글 기본값은 '검토 후 답글'입니다. '자동답글 (안전형)'은 짧은 감사·응원 댓글에만, 하루 최대 10/20/30개, 1분 간격으로 답합니다.
• 질문(?), 링크, 긴 댓글, 제외 키워드가 있는 댓글은 자동으로 답하지 않고 '검토 필요'로 둡니다.
• 같은 댓글에 두 번 답하지 않습니다. YouTube Studio에서 직접 단 답글도 확인합니다.
• 프로그램이 꺼져 있으면 댓글을 달 수 없습니다. 다시 켜면 밀린 첫 댓글을 확인해 등록합니다.

무료 일본어 도우미
• {B('jp_setup')}에서 Ollama와 무료 Qwen3 모델을 준비합니다. 번역과 문장 만들기는 내 PC에서 처리되며 유료 AI API 키가 필요하지 않습니다.
• 일본어 댓글의 자연스러운 한국어 번역·뉘앙스·답글 3안을 만들 수 있습니다. 선택 후에도 기존 수동 답글 창에서 직접 보내야 합니다.
• {B('jp_external')} 사용 순서: 1) 일본 영상 주소 붙여넣기 2) 내 감상 한 줄 적기 3) 일본어 댓글 만들기 4) 3개 중 하나 선택 5) 작성 채널·영상·댓글을 최종 확인한 뒤 1건 게시.
• 게시 직전에 실제 YouTube 채널을 다시 확인하며, 같은 영상에 같은 문장은 차단합니다. 방금 작성한 댓글은 삭제할 수 있습니다.
• 자동으로 여러 영상에 댓글을 다는 기능은 없습니다. 검색·순회·예약·백그라운드 외부 댓글 기능도 없습니다.
• JP 2030s Female Natural은 문체 스타일일 뿐 사용자의 실제 신분을 주장하지 않습니다.""",
              ("댓글", "답글", "자동답글", "첫 댓글", "일본어", "Ollama", "로컬", "무료")),
    HelpTopic("channels", "⑥ YouTube 채널 연결", f"""예약 업로드·댓글은 YouTube 채널을 연결해야 사용할 수 있습니다.

1. {B('card_upload')} → {B('channels')} → {B('new_channel')}
   (처음이면 {B('guide')}의 'YouTube 채널 연결'에서 [한국 채널]/[일본 채널]을 누르면 언어·시간대가 자동으로 정해집니다.)
2. {B('pick_oauth')}: Google Cloud에서 받은 '데스크톱 앱용 JSON 파일'입니다. ({B('what_oauth')}를 누르면 설명이 나옵니다.)
3. {B('connect')}: 브라우저에서 로그인 → YouTube 채널 선택 → [허용] → 프로그램으로 돌아옵니다.
4. 연결된 채널 이름·언어·시간대가 나오면 완료입니다.

• 한국 채널과 일본 채널은 연결 정보가 따로 저장되어 섞이지 않습니다.
• 비밀번호는 프로그램에 입력하지 않습니다. 로그인은 Google 화면에서만 합니다.
• Google 앱이 '테스트' 상태이면 7일 뒤 연결이 끊길 수 있습니다. 그때는 {B('connect')}을 다시 누르세요.""",
              ("채널", "연결", "Google", "로그인", "JSON", "연결 파일")),
    HelpTopic("google_file", "⑦ Google 연결 파일 만들기", f"""{GOOGLE_FILE_INTRO}

처음 연결할 때 {B('first_connect')} → {B('first_time')}을 누르면 아래 순서가 담긴 도우미 창이 열립니다.
도우미 창의 {B('open_cloud')}로 브라우저를 열고, 창을 옆에 둔 채 따라 하세요. ({B('copy_steps')}로 순서를 복사할 수 있습니다.)

{google_steps_text()}

이미 파일이 있으면 {B('have_file')}을 누르세요.""",
              ("Google", "연결 파일", "JSON", "OAuth", "Cloud", "처음", "client")),
    HelpTopic("playlists", "⑧ 재생목록 사용하기", f"""{TOOLTIPS['playlist']}

1. 예약 업로드에서 YouTube 채널을 선택하세요.
2. '재생목록'에서 넣을 재생목록을 선택하세요. 목록이 비어 있으면 {B('playlist_refresh')}을 누르세요.
3. 없으면 {B('playlist_new')}로 만드세요. '이 템플릿의 기본 재생목록으로 저장'을 켜 두면 다음부터 자동으로 선택됩니다.
4. 업로드하면 영상이 자동으로 그 재생목록에 들어갑니다 (예약 영상도 업로드가 끝나면 바로 들어갑니다).

• 재생목록은 그 YouTube 채널의 것만 보여주고, 다른 채널의 재생목록이면 업로드하지 않습니다.
• 재생목록 추가만 실패하면 영상은 그대로 두고 '일부 실패'로 표시합니다. {B('playlist_retry')}을 누르세요.
• 같은 영상을 같은 재생목록에 두 번 넣지 않습니다.
• 넣고 싶지 않으면 '{BUTTONS['playlist_none']}'을 고르세요.""",
              ("재생목록", "플레이리스트", "playlist", "시리즈")),
    HelpTopic("faq", "⑨ 자주 묻는 질문", """Q. 창을 닫으면 업로드가 멈추나요?
A. 예약 업로드 창을 닫아도 프로그램이 켜져 있으면 계속됩니다. 프로그램 자체를 종료하면 멈추고, 다음에 [▶ 예약 업로드 시작]을 누르면 받은 곳부터 이어서 올립니다.

Q. 컴퓨터를 꺼도 예약 공개가 되나요?
A. 네. 업로드가 끝난 영상은 YouTube가 정한 시간에 공개합니다. 단, 첫 댓글과 새 댓글 확인은 프로그램이 켜져 있어야 합니다.

Q. 한국 영상이 일본 채널에 올라갈 수 있나요?
A. 업로드 직전마다 실제 채널을 확인하고, 다르면 업로드하지 않습니다.

Q. 썸네일이 없으면요?
A. 썸네일 없이도 업로드됩니다. 나중에 YouTube Studio에서 바꿀 수 있습니다.

Q. 영상이 비공개로만 올라가요.
A. 'Google 설정 확인 필요'로 표시됩니다. 검수되지 않은 Google 프로젝트는 비공개로만 올릴 수 있습니다. 같은 영상을 다시 올리지 마세요.""",
              ("질문", "FAQ", "비공개", "끄면", "종료")),
    HelpTopic("trouble", "⑩ 문제 해결", f"""• Google 연결 파일이 없습니다 → {B('first_connect')} → {B('first_time')} (도움말 '⑦ Google 연결 파일 만들기').
• 선택한 재생목록이 현재 YouTube 채널의 것이 아닙니다 → 재생목록을 다시 고르세요 ({B('playlist_refresh')}).
• 재생목록 추가 실패 → 영상은 그대로 있습니다. {B('playlist_retry')}을 누르세요.
• FFmpeg를 찾지 못했습니다 → 메인의 [FFmpeg 설정]에서 ffmpeg.exe를 선택하세요 (같은 폴더에 ffprobe.exe 필요).
• Google 연결이 끊겼습니다 → {B('channels')}에서 그 채널의 {B('connect')}을 다시 누르세요.
• Google 연결 권한이 부족합니다 (댓글) → 그 채널을 '댓글 기능 권한도 함께 요청'을 켠 채 다시 연결하세요.
• YouTube 채널이 다릅니다 → 예약 업로드에서 올바른 채널을 고르거나, 그 채널로 다시 연결하세요.
• 예약 시간이 지났습니다 → 지금보다 5분 이상 뒤로 다시 고르세요.
• 오늘 사용량 한도 → 내일 다시 시도하세요. 작업은 그대로 남아 있습니다.
• 어디가 문제인지 모르겠으면 메인의 {B('check')}을 누르세요.
• 도움을 요청할 때는 {B('copy_diag')}를 눌러 복사한 내용을 붙여넣으세요 (비밀값은 들어가지 않습니다).""",
              ("문제", "오류", "에러", "FFmpeg", "진단", "해결")),
)


def topic(key: str) -> HelpTopic:
    return next(t for t in TOPICS if t.key == key)


def search(query: str) -> list[HelpTopic]:
    q = (query or "").strip().lower()
    if not q:
        return list(TOPICS)
    words = q.split()
    return [t for t in TOPICS if all(w in (t.title + " " + t.body + " " + " ".join(t.keywords)).lower() for w in words)]


def usage_text(key: str) -> tuple[str, str]:
    title, steps = FEATURE_USAGE[key]
    return title, "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
