"""초보자용 화면 문구 (Tk 없음) — 초보자 모드, 쉬운 상태 문장, 오류 → '무슨 문제 / 무엇을 하면 되나' 변환.

화면에서는 'YouTube 채널', 'Google 연결 파일', 'Google 계정 연결', '예약 공개 시간'처럼 쉬운 말을 쓰고,
기술 용어(HTTP 코드, reason 등)는 [자세히 보기]에서만 보여준다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .settings import load_settings, update_settings

BEGINNER_KEY = "beginner_mode"
FIRST_RUN_KEY = "first_run_completed"

# 사용자에게 보이는 용어 (기술 용어 → 쉬운 말)
TERMS = {
    "OAuth JSON": "Google 연결 파일",
    "OAuth 연결": "Google 계정 연결",
    "Channel Profile": "YouTube 채널",
    "publishAt": "예약 공개 시간",
    "API_REVIEW_REQUIRED": "Google 설정 확인 필요",
    "Resumable upload": "업로드 중",
}
OAUTH_FILE_HELP = ("Google 연결 파일은 Google Cloud에서 받은 '데스크톱 앱용 JSON 파일'입니다.\n\n"
                   "이 프로그램이 내 YouTube 채널에 영상을 올려도 되는지 Google에 물어볼 때 사용합니다. "
                   "파일 안의 내용은 프로그램 설정에 복사하지 않고, 파일 위치만 기억합니다.\n\n"
                   "다른 사람과 공유하거나 인터넷에 올리지 마세요.")
CONNECT_STEPS = ("1. 인터넷 브라우저가 열립니다.", "2. 사용할 Google 계정으로 로그인합니다.",
                 "3. YouTube 채널을 선택합니다.", "4. [허용]을 누릅니다.", "5. 완료되면 이 프로그램으로 돌아옵니다.")
CONNECTING_TEXT = "브라우저에서 Google 로그인을 완료해 주세요."

# 업로드 단계 → 쉬운 문장
FRIENDLY_STATUS = {
    "PENDING": "업로드를 기다리고 있습니다.",
    "VERIFYING_CHANNEL": "업로드할 YouTube 채널을 확인하고 있습니다.",
    "CREATING_SESSION": "업로드를 준비하고 있습니다.",
    "UPLOADING": "영상을 YouTube에 올리고 있습니다.",
    "PROCESSING": "YouTube가 영상을 처리하고 있습니다.",
    "APPLYING_THUMBNAIL": "썸네일을 적용하고 있습니다.",
    "VERIFYING_SCHEDULE": "예약 시간이 제대로 등록됐는지 확인하고 있습니다.",
    "COMPLETE": "예약 업로드가 끝났습니다.",
    "PARTIAL": "영상은 올라갔지만 썸네일이 적용되지 않았습니다.",
    "PAUSED": "업로드를 잠시 멈췄습니다. 다시 시작하면 이어서 올립니다.",
    "CANCELLED": "취소했습니다.",
    "FAILED": "업로드하지 못했습니다.",
    "BLOCKED": "선택한 YouTube 채널과 연결된 채널이 달라 업로드를 멈췄습니다.",
    "API_REVIEW_REQUIRED": "Google 설정 확인이 필요합니다.",
}


def is_beginner() -> bool:
    v = load_settings().get(BEGINNER_KEY)
    return True if v is None else bool(v)


def set_beginner(on: bool) -> None:
    update_settings(**{BEGINNER_KEY: bool(on)})


def first_run_done() -> bool:
    return bool(load_settings().get(FIRST_RUN_KEY))


def mark_first_run_done() -> None:
    update_settings(**{FIRST_RUN_KEY: True})


def friendly_status(state: str) -> str:
    return FRIENDLY_STATUS.get(state, state)


# ---------------- 오류 → 해결 방법 ----------------
A_RECONNECT, A_RESELECT, A_FFMPEG, A_RETRY_LATER, A_PICK_FILE, A_STUDIO, A_HELP, A_REPICK_TIME = (
    "reconnect", "reselect", "ffmpeg", "later", "pick_file", "studio", "help", "repick_time")
ACTION_LABELS = {A_RECONNECT: "채널 다시 연결", A_RESELECT: "채널 다시 선택", A_FFMPEG: "FFmpeg 찾기",
                 A_RETRY_LATER: "나중에 다시 시도", A_PICK_FILE: "파일 다시 선택", A_STUDIO: "YouTube Studio 열기",
                 A_HELP: "도움말 보기", A_REPICK_TIME: "날짜·시간 다시 선택"}


@dataclass(frozen=True)
class FriendlyError:
    problem: str  # 무슨 문제가 생겼나요?
    action: str  # 무엇을 하면 되나요?
    action_key: str = A_HELP
    detail: str = ""  # [자세히 보기]에서만 (HTTP 코드, reason, 원문)

    def text(self, *, beginner: bool = True) -> str:
        out = f"문제: {self.problem}\n해결: {self.action}"
        return out if beginner or not self.detail else f"{out}\n\n(자세히: {self.detail})"


# (검사할 reason/문구, 문제, 해결, 행동)
_RULES = (
    (("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "INSUFFICIENT_PERMISSION", "다시 승인하세요"),
     "Google 연결 권한이 부족합니다.", "댓글 기능을 사용하려면 이 YouTube 채널을 한 번 다시 연결해야 합니다.", A_RECONNECT),
    (("channelMismatch", "CHANNEL_MISMATCH", "채널 불일치"),
     "YouTube 채널이 다릅니다.", "예약 업로드에서 올바른 채널을 다시 선택하거나, 그 채널로 Google 계정을 다시 연결하세요.", A_RESELECT),
    (("invalid_grant", "AUTH_EXPIRED", "만료", "연결되어 있지 않습니다", "연결되지 않았습니다", "unauthorized"),
     "Google 연결이 끊겼거나 만료되었습니다.", "[YouTube 채널 관리]에서 이 채널의 [Google 계정 연결]을 다시 누르세요.", A_RECONNECT),
    (("apiProjectPrivateOnly", "API_REVIEW_REQUIRED", "API 프로젝트 제한"),
     "Google 설정 확인이 필요합니다.", "영상은 올라갔지만 Google 프로젝트 제한 때문에 비공개로만 올라갑니다. "
     "같은 영상을 다시 올리지 말고 YouTube Studio에서 확인하세요.", A_STUDIO),
    (("quotaExceeded", "QUOTA_EXCEEDED", "사용량 한도"),
     "오늘 YouTube 사용량 한도에 도달했습니다.", "내일 다시 시도하세요. 예약된 작업은 그대로 남아 있습니다.", A_RETRY_LATER),
    (("rateLimitExceeded", "RATE_LIMITED", "일시적으로 응답하지"),
     "YouTube가 잠시 바쁩니다.", "잠시 뒤 자동으로 다시 시도합니다. 그대로 기다려 주세요.", A_RETRY_LATER),
    (("network", "NETWORK_ERROR", "네트워크", "연결할 수 없습니다"),
     "인터넷 연결이 끊겼습니다.", "인터넷 연결을 확인하세요. 다시 연결되면 이어서 진행합니다.", A_RETRY_LATER),
    (("commentsDisabled", "COMMENTS_DISABLED", "댓글 사용이 꺼져"),
     "이 영상은 댓글 사용이 꺼져 있습니다.", "YouTube Studio에서 댓글을 켜면 다음부터 사용할 수 있습니다.", A_STUDIO),
    (("publishAtPast", "지났거나 너무 가깝습니다", "naivePublishAt"),
     "예약 공개 시간이 이미 지났거나 너무 가깝습니다.", "지금보다 5분 이상 뒤의 날짜·시간을 선택하세요.", A_REPICK_TIME),
    (("fileChanged", "영상 파일이 변경되었습니다"),
     "예약 등록 후 영상 파일이 바뀌었습니다.", "대기열에서 이 작업을 지우고 영상을 다시 추가하세요.", A_PICK_FILE),
    (("noVideoFile", "영상 파일을 찾을 수 없습니다", "emptyVideo", "badVideoFile"),
     "영상 파일을 찾을 수 없거나 열 수 없습니다.", "영상 파일이 그 자리에 있는지 확인하고 다시 선택하세요.", A_PICK_FILE),
    (("Google 연결 파일", "데스크톱 앱"),
     "Google 연결 파일이 올바르지 않습니다.", "Google Cloud에서 받은 '데스크톱 앱용 JSON 파일'을 다시 선택하세요.", A_PICK_FILE),
    (("ffmpeg", "FFmpeg"),
     "FFmpeg를 찾지 못했습니다.", "[FFmpeg 찾기]를 눌러 ffmpeg.exe를 선택하세요 (같은 폴더에 ffprobe.exe 필요).", A_FFMPEG),
    (("videoNotFound", "VIDEO_NOT_FOUND"),
     "YouTube에서 영상을 찾을 수 없습니다.", "YouTube Studio에서 영상이 삭제되지 않았는지 확인하세요.", A_STUDIO),
)


def friendly_error(err=None, *, reason: str = "", message: str = "", status: int = 0) -> FriendlyError:
    """예외 또는 (reason, 메시지) → 쉬운 오류. 모르는 오류도 traceback 대신 해결 방법을 준다."""
    if err is not None:
        reason = reason or str(getattr(err, "reason", "") or getattr(err, "kind", "") or "")
        status = status or int(getattr(err, "status", 0) or 0)
        message = message or str(err)
    haystack = f"{reason} {message}"
    detail = " ".join(x for x in (f"HTTP {status}" if status else "", reason, message) if x).strip()
    for keys, problem, action, key in _RULES:
        if any(k and k in haystack for k in keys):
            return FriendlyError(problem, action, key, detail)
    return FriendlyError("작업을 끝내지 못했습니다.", "잠시 뒤 다시 시도하세요. 계속되면 [? 도움말] → [문제 해결]의 "
                         "[진단 정보 복사]로 내용을 복사해 도움을 요청하세요.", A_HELP, detail)


_SECRET_PATTERNS = (  # (패턴, 바꿀 문자열)
    (re.compile(r"(?i)(access_token|refresh_token|client_secret|stream_?key|stream_?name)([\"'=:\s]+)[^\s\"'&,}]+"),
     r"\1\2[숨김]"),
    (re.compile(r"ya29\.[0-9A-Za-z_\-.]+"), "[숨김]"),  # access token
    (re.compile(r"1//[0-9A-Za-z_\-]{10,}"), "[숨김]"),  # refresh token
    (re.compile(r"GOCSPX-[0-9A-Za-z_\-]+"), "[숨김]"),  # client secret
    (re.compile(r"(upload_id=)[^&\s\"']+"), r"\1[숨김]"),  # 업로드 세션 주소
    (re.compile(r"(rtmps?://[^\s/]+/[^\s/]+/)\S+"), r"\1[숨김]"),  # 송출 주소 뒤의 Stream Key
    (re.compile(r"\b[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}\b"), "[숨김]"),  # Stream Key 형식
)


def redact(text: str) -> str:
    """진단/오류 문구에서 비밀값 지우기 (access/refresh token, client secret, 세션 주소, Stream Key)."""
    out = str(text or "")
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out
