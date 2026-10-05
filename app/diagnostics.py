"""환경 점검 · 설정 점검 · 진단 정보 (Tk 없음).

- 사용자에게 명령어를 치게 하지 않는다. 결과는 ✓/⚠/✗ + 쉬운 문장 + 고칠 방법(fix 키).
- 진단 정보에는 token/secret/Stream Key/업로드 세션 주소를 넣지 않는다 (마지막에 redact로 한 번 더 지움).
"""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .help_content import APP_NAME, MANUAL_VERSION
from .settings import load_settings, settings_dir
from .ui_text import friendly_error, redact

OK, WARN, FAIL = "ok", "warn", "fail"
MARKS = {OK: "✓", WARN: "⚠", FAIL: "✗"}
APP_VERSION = "v0.3 (Studio v1.3)"


@dataclass
class CheckItem:
    status: str
    label: str
    detail: str = ""
    fix: str = ""  # ffmpeg | channels | reconnect | wizard | comments | ""

    @property
    def line(self) -> str:
        return f"{MARKS[self.status]} {self.label}" + (f" — {self.detail}" if self.detail else "")


def _internet(timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(("www.google.com", 443), timeout=timeout):
            return True
    except OSError:
        return False


def check_environment(*, ffmpeg_finder: Callable[[], tuple | None], internet: Callable[[], bool] | None = None,
                      disk_free: Callable[[Path], int] | None = None) -> list[CheckItem]:
    """STEP 1 기본 프로그램 점검."""
    internet = internet or (lambda: _internet())
    out = []
    pair = None
    try:
        pair = ffmpeg_finder()
    except Exception:  # noqa: BLE001 - 사용자에게 traceback을 보여주지 않는다
        pair = None
    if pair:
        out.append(CheckItem(OK, "FFmpeg 준비됨"))
        out.append(CheckItem(OK, "FFprobe 준비됨"))
    else:
        out.append(CheckItem(FAIL, "FFmpeg를 찾지 못했습니다.", "영상 늘리기와 LIVE에 필요합니다.", "ffmpeg"))
    try:
        d = settings_dir()
        probe = d / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        out.append(CheckItem(OK, "프로그램 설정 폴더 정상"))
        free = (disk_free or (lambda p: shutil.disk_usage(p).free))(d)
        out.append(CheckItem(OK if free > 5 * 1024 ** 3 else WARN, "저장 공간 확인됨" if free > 5 * 1024 ** 3 else "저장 공간이 적습니다",
                             f"남은 공간 약 {free / 1024 ** 3:.0f} GB"))
    except OSError:
        out.append(CheckItem(FAIL, "프로그램 설정 폴더에 저장할 수 없습니다.", "다른 프로그램이 막고 있는지 확인하세요."))
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Asia/Seoul"), ZoneInfo("Asia/Tokyo")
        out.append(CheckItem(OK, "시간대 정보 정상 (한국·일본)"))
    except Exception:  # noqa: BLE001
        out.append(CheckItem(FAIL, "시간대 정보를 찾지 못했습니다.", "프로그램을 다시 설치하세요 (tzdata 필요)."))
    offset = -time.timezone // 3600 if not time.localtime().tm_isdst else -time.altzone // 3600
    out.append(CheckItem(OK, "Windows 시간대 확인됨", f"UTC{offset:+d}"))
    try:
        online = internet()
    except Exception:  # noqa: BLE001
        online = False
    out.append(CheckItem(OK, "인터넷 연결됨") if online else
               CheckItem(WARN, "인터넷 연결을 확인하지 못했습니다.", "YouTube 연결·업로드에는 인터넷이 필요합니다."))
    return out


def check_settings(*, profiles, ffmpeg_ok: bool, comment_store=None) -> list[CheckItem]:
    """메인 [⚙ 설정 점검]: 무엇이 준비됐고 무엇을 고치면 되는지."""
    out = [CheckItem(OK, "FFmpeg") if ffmpeg_ok else CheckItem(FAIL, "FFmpeg를 찾지 못했습니다.", "", "ffmpeg")]
    all_p = profiles.all()
    if not all_p:
        out.append(CheckItem(WARN, "연결된 YouTube 채널이 없습니다.", "예약 업로드를 하려면 채널을 연결하세요.", "wizard"))
    for p in all_p:
        if profiles.is_connected(p):
            out.append(CheckItem(OK, f"{p.alias} 연결", p.channel_title or ""))
        else:
            out.append(CheckItem(WARN, f"{p.alias} 연결 안 됨", "[Google 계정 연결]이 필요합니다.", "channels"))
        if comment_store is not None and comment_store.has_settings(p.profile_id) \
                and comment_store.settings_for(p).needs_reauth:
            out.append(CheckItem(WARN, f"{p.alias} 댓글 권한 미설정", "이 채널을 다시 연결하세요.", "reconnect"))
    ready = any(profiles.is_connected(p) for p in all_p)
    out.append(CheckItem(OK, "예약 업로드 준비") if ready else
               CheckItem(WARN, "예약 업로드 준비 안 됨", "YouTube 채널을 먼저 연결하세요.", "wizard"))
    if comment_store is not None:
        monitored = [p for p in all_p if comment_store.has_settings(p.profile_id) and comment_store.settings_for(p).monitor]
        out.append(CheckItem(OK, "댓글 자동화 설정", f"새 댓글 확인 {len(monitored)}개 채널") if monitored else
                   CheckItem(OK, "댓글 자동화 설정", "사용 안 함 (필요할 때 [💬 댓글 관리])"))
    return out


def ffmpeg_version(path) -> str:
    try:
        r = subprocess.run([str(path), "-hide_banner", "-version"], capture_output=True, text=True, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.stdout or "").splitlines()[0] if r.stdout else "?"
    except (OSError, subprocess.SubprocessError):
        return "?"


def build_report(*, profiles=None, ffmpeg_pair=None, comment_store=None,
                 version_of: Callable = ffmpeg_version) -> str:
    """[진단 정보 복사]: 지인/개발자에게 붙여넣을 수 있는 내용. 비밀값은 넣지 않는다."""
    lines = [f"{APP_NAME} {APP_VERSION} · Manual {MANUAL_VERSION}",
             f"Windows: {platform.platform()}",
             f"Runtime: Python {sys.version.split()[0]}" + (" (EXE)" if getattr(sys, "frozen", False) else ""),
             f"FFmpeg: {ffmpeg_pair[0]} · {version_of(ffmpeg_pair[0])}" if ffmpeg_pair else "FFmpeg: 찾지 못함"]
    data = load_settings()
    if profiles is not None:
        ps = profiles.all()
        lines.append(f"YouTube 채널: {len(ps)}개")
        for p in ps:
            reauth = bool(comment_store and comment_store.has_settings(p.profile_id)
                          and comment_store.settings_for(p).needs_reauth)
            lines.append(f"  - {p.alias} · 언어 {p.language} · 시간대 {p.timezone} · "
                         f"{'연결됨' if profiles.is_connected(p) else '연결 안 됨'}" + (" · 댓글 권한 다시 승인 필요" if reauth else ""))
    jobs = [j for j in (data.get("upload_queue") or []) if isinstance(j, dict)]
    states: dict[str, int] = {}
    for j in jobs:
        states[j.get("status", "?")] = states.get(j.get("status", "?"), 0) + 1
    lines.append("예약 업로드 대기열: " + (", ".join(f"{k} {v}" for k, v in sorted(states.items())) or "비어 있음"))
    long_q = [j for j in (data.get("queue") or []) if isinstance(j, dict)]
    lines.append(f"영상 늘리기 대기열: {len(long_q)}개")
    errors = []
    for j in jobs:
        if j.get("error"):
            fe = friendly_error(message=str(j["error"]))
            errors.append(f"업로드: {fe.problem}")
    for t in data.get("comment_tasks") or []:
        if isinstance(t, dict) and t.get("error_kind"):
            errors.append(f"첫 댓글: {t['error_kind']}")
    lines.append("최근 오류 종류: " + (", ".join(dict.fromkeys(errors[-10:])) or "없음"))
    return redact("\n".join(lines))


def manual_path(name: str = "사용자_매뉴얼.html") -> Path | None:
    """EXE 옆 → EXE 안(배포 데이터) → 저장소 docs 순서로 찾는다."""
    cands = []
    if getattr(sys, "frozen", False):
        cands.append(Path(sys.executable).parent / name)
        cands.append(Path(getattr(sys, "_MEIPASS", "")) / "docs" / name)
    cands.append(Path(__file__).resolve().parent.parent / "docs" / name)
    return next((c for c in cands if c.is_file()), None)


def open_file(path: Path) -> bool:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
        else:
            import webbrowser
            webbrowser.open(path.as_uri())
        return True
    except OSError:
        return False
