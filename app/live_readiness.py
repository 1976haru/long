"""LIVE 초보자 화면용 준비 상태 계산 (Tk 비의존).

- 채널 이름표: 채널 A (기본) / 채널 B / 채널 C …
- 채널별 준비 체크리스트 6개 (영상 · LIVE READY · 실행 위치 · Stream Key · 서버 주소 · YouTube 연결 필요 여부)
- 서버 주소 / Stream Key 혼동 검사 (서버 주소 칸에 Key를 붙였거나, Key 칸에 주소를 넣은 경우)
Stream Key 값은 검사에만 쓰고 결과 문구에 넣지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .cloud_model import DEFAULT_LIVE_PROFILE, MAX_CONCURRENT_LIVE
from .live_profile import YOUTUBE_RTMP_INGEST, YOUTUBE_RTMPS_INGEST

DEFAULT_SERVER = YOUTUBE_RTMPS_INGEST  # 실제로 쓰는 기본 서버 (보안 연결)
DEFAULT_SERVER_PLAIN = YOUTUBE_RTMP_INGEST  # YouTube Studio 화면에 보이는 주소 (같은 서버의 일반 연결)
SERVER_EXAMPLE_OK = YOUTUBE_RTMP_INGEST
SERVER_EXAMPLE_BAD = YOUTUBE_RTMP_INGEST + "/abcd-efgh-ijkl-mnop-qrst"
KEY_EXAMPLE = "abcd-efgh-ijkl-mnop-qrst"
KEY_LIKE = re.compile(r"^[A-Za-z0-9]{4}(-[A-Za-z0-9]{4}){3,5}$")
MAX_TEXT = f"현재는 최대 {MAX_CONCURRENT_LIVE}개 채널까지 동시에 송출할 수 있습니다."

ST_UNSET, ST_READY, ST_STARTING, ST_LIVE, ST_ERROR = "미설정", "준비됨", "시작 중…", "송출중", "오류"
STATE_COLORS = {ST_UNSET: "gray30", ST_READY: "darkgreen", ST_STARTING: "darkorange", ST_LIVE: "red", ST_ERROR: "firebrick"}

SERVER_EMPTY = ("서버 주소가 비어 있어 시작할 수 없습니다.\n"
                "기본값을 쓰려면 'YouTube 기본 서버 사용'을 선택하세요.")
SERVER_BAD_FORMAT = ("서버 주소 형식이 올바르지 않습니다.\n"
                     f"rtmp:// 또는 rtmps:// 로 시작해야 합니다. 예: {SERVER_EXAMPLE_OK}")
SERVER_HAS_KEY = ("서버 주소 칸에 Stream Key가 같이 들어간 것 같습니다.\n"
                  f"서버 주소에는 {SERVER_EXAMPLE_OK} 까지만 넣고, Stream Key는 Stream Key 칸에 따로 넣으세요.")
KEY_IS_URL = ("Stream Key 칸에 서버 주소를 넣은 것 같습니다.\n"
              f"Stream Key는 '{KEY_EXAMPLE}' 같은 모양입니다. 서버 주소(rtmp://…)는 넣지 마세요.")
KEY_BAD = "Stream Key에 공백이나 / 같은 문자가 들어 있습니다. YouTube Studio의 'Stream Key'만 복사해 붙여넣으세요."
YT_NOT_NEEDED = ("지금은 Stream Key 방식이라 YouTube 계정 연결 없이도 송출할 수 있습니다.\n"
                 "단, 예약 LIVE와 자동 세션은 YouTube 연결이 필요합니다.")


def channel_letter(index: int) -> str:
    return chr(ord("A") + index) if 0 <= index < 26 else str(index + 1)


def channel_label(index: int, display_name: str, profile_id: str) -> str:
    """'채널 A (기본)' / '채널 A (기본) · 시니어 채널' / '채널 B · 일본 CHILI LAB'."""
    head = f"채널 {channel_letter(index)}" + (" (기본)" if profile_id == DEFAULT_LIVE_PROFILE else "")
    name = (display_name or "").strip()
    return head if not name or name == "기본 채널" else f"{head} · {name}"


@dataclass
class ServerCheck:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def check_server_and_key(server_mode: str, custom_url: str, stream_key: str | None, *, api_mode: bool = False) -> ServerCheck:
    """시작 전 검사. server_mode: 'youtube'(기본) | 'custom'(직접 입력). API 모드는 서버/Key를 YouTube API가 관리."""
    r = ServerCheck()
    if api_mode:
        return r
    key = (stream_key or "").strip()
    if key:
        if key.lower().startswith(("rtmp://", "rtmps://", "http://", "https://")) or "youtube.com" in key.lower():
            r.errors.append(KEY_IS_URL)
        elif any(c.isspace() for c in key) or "/" in key or "?" in key or "#" in key or "\\" in key:
            r.errors.append(KEY_BAD)
    if server_mode != "custom":
        return r
    url = (custom_url or "").strip()
    if not url:
        r.errors.append(SERVER_EMPTY)
        return r
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("rtmp", "rtmps") or not parts.hostname or parts.query or parts.fragment:
        r.errors.append(SERVER_BAD_FORMAT)
        return r
    segments = [s for s in parts.path.split("/") if s]
    looks_key = bool(segments) and bool(KEY_LIKE.match(segments[-1]))
    youtube_extra = "youtube.com" in (parts.hostname or "") and len(segments) > 1
    if (key and key in url) or looks_key or youtube_extra:
        r.warnings.append(SERVER_HAS_KEY)
    return r


@dataclass(frozen=True)
class CheckItem:
    key: str
    label: str
    ok: bool
    hint: str  # 부족할 때 할 일 (쉬운 말) / 완료면 짧은 상태


def channel_checklist(*, media_count: int, ready: bool | None, location_cloud: bool, cloud_configured: bool,
                      key_present: bool, server_mode: str, custom_url: str, stream_key: str | None, api_mode: bool,
                      yt_connected: bool) -> list[CheckItem]:
    """채널 1개의 준비 상태 6항목. ready: True=LIVE READY / False=아님 / None=분석 중·영상 없음."""
    items = [CheckItem("media", "영상 선택", media_count > 0,
                       f"{media_count}개 선택됨" if media_count else "① LIVE 영상에서 [영상 선택] 또는 Playlist에 [영상 추가]를 하세요.")]
    if not media_count:
        items.append(CheckItem("ready", "LIVE READY 확인", False, "영상을 먼저 고르세요."))
    elif ready is None:
        items.append(CheckItem("ready", "LIVE READY 확인", False, "영상 확인 중입니다. 잠시 기다리세요."))
    else:
        items.append(CheckItem("ready", "LIVE READY 확인", bool(ready), "확인 완료" if ready else
                               "[LIVE READY 파일 만들기] (Playlist는 [문제 영상 모두 LIVE READY로 만들기])를 누르세요."))
    if location_cloud:
        items.append(CheckItem("location", "Cloud/내 PC 선택", cloud_configured, "무료 Cloud" if cloud_configured else
                               "무료 Cloud가 아직 준비되지 않았습니다. [처음 설정 도우미]를 하거나 '내 PC'를 고르세요."))
    else:
        items.append(CheckItem("location", "Cloud/내 PC 선택", True, "내 PC (PC를 끄면 방송도 끝납니다)"))
    if api_mode:
        items.append(CheckItem("key", "Stream Key 입력", True, "YouTube API가 자동으로 관리"))
    else:
        items.append(CheckItem("key", "Stream Key 입력", key_present, "입력됨 (화면에는 가려서 표시)" if key_present else
                               "③ YouTube 송출의 Stream Key 칸에 YouTube Studio의 Stream Key를 붙여넣으세요."))
    sc = check_server_and_key(server_mode, custom_url, stream_key, api_mode=api_mode)
    if api_mode:
        items.append(CheckItem("server", "서버 주소 확인", True, "YouTube API가 자동으로 관리"))
    elif sc.errors:
        items.append(CheckItem("server", "서버 주소 확인", False, sc.errors[0]))
    else:
        items.append(CheckItem("server", "서버 주소 확인", True,
                               "자동 (YouTube 기본)" if server_mode != "custom" else "직접 입력한 주소"
                               + (" — ⚠ 확인 필요" if sc.warnings else "")))
    if api_mode:
        items.append(CheckItem("youtube", "YouTube 연결 필요 여부", yt_connected, "연결됨" if yt_connected else
                               "API 자동 세션은 YouTube 연결이 필요합니다. ③의 [YouTube 연결]을 누르세요."))
    else:
        items.append(CheckItem("youtube", "YouTube 연결 필요 여부", True, "필요 없음 (Stream Key 방식)"))
    return items


def missing(items: list[CheckItem]) -> list[CheckItem]:
    return [i for i in items if not i.ok]


def summary_line(items: list[CheckItem]) -> str:
    miss = missing(items)
    done = len(items) - len(miss)
    if not miss:
        return f"✓ 시작 가능 ({done}/{len(items)})"
    return f"준비 {done}/{len(items)} — 부족: " + ", ".join(i.label for i in miss)


def card_state(*, live: bool, busy: bool, failed: bool, ready: bool) -> str:
    if live:
        return ST_LIVE
    if busy:
        return ST_STARTING
    if failed:
        return ST_ERROR
    return ST_READY if ready else ST_UNSET
