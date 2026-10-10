"""LIVE UI UX hotfix — YouTube 연결 영역 항상 표시 / 송출 방식 분리 / Playlist 안내 / 예약 LIVE 연결 상태 자동 반영.
UI만 확인한다 (실제 YouTube/OAuth/Cloud 접속 없음)."""
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.live_secrets import SessionStreamKeyStore
from app.youtube_config import save_youtube_settings
from app.youtube_session import STREAM_MODE_API, STREAM_MODE_MANUAL


@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    r.withdraw()
    yield r
    r.destroy()


def pump(root, cond, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.03)
    return cond()


def live_window(root):
    from app.cloud_client import CloudLiveController
    from app.live_ui import LiveWindow
    return LiveWindow(root, tools=lambda: (None, None), key_store=SessionStreamKeyStore(),
                      cloud=CloudLiveController(lambda: None, poll_seconds=3600))


def shown(widget) -> bool:
    """위젯과 부모가 모두 배치되어 있는지 (pack/grid)."""
    w = widget
    while w is not None and w.winfo_class() not in ("Tk", "Toplevel"):
        if not w.winfo_manager():
            return False
        w = w.master
    return True


def test_youtube_connect_button_visible_in_both_modes(root, monkeypatch):
    import app.live_ui as live_ui
    w = live_window(root)
    try:
        assert w.yt_mode.get() == STREAM_MODE_MANUAL
        assert shown(w.btn_yt_setup) and w.btn_yt_setup.cget("text") == "YouTube 연결"
        assert "연결 안 됨" in w.yt_status.get() and shown(w.lbl_yt)
        assert shown(w.cmb_channel) and w.channel_var.get() == "채널 A (기본)"  # 초보자 문구: 기본 채널 → 채널 A (기본)
        w.yt_mode.set(STREAM_MODE_API)
        w._on_yt_mode()
        assert shown(w.btn_yt_setup) and shown(w.yt_frame)
        # 연결 후: 송출 방식(Stream Key)과 상관없이 연결 상태/채널 이름 표시
        w.yt_mode.set(STREAM_MODE_MANUAL)
        w._on_yt_mode()
        save_youtube_settings(channel_title="CHILI LAB")
        monkeypatch.setattr(live_ui, "is_connected", lambda: True)
        w._update_yt_status()
        assert w.yt_status.get() == "✓ 연결됨 · 채널: CHILI LAB" and w.btn_yt_setup.cget("text") == "다시 연결"
        assert shown(w.btn_yt_setup) and not shown(w.yt_frame)  # 연결 영역은 보이고 API 설정은 숨김
        # 다른 창에서 연결이 바뀌어도 tick이 반영
        monkeypatch.setattr(live_ui, "is_connected", lambda: False)
        assert pump(root, lambda: "연결 안 됨" in w.yt_status.get(), timeout=8)
    finally:
        w.destroy()


def test_playlist_and_single_mode_guidance(root):
    w = live_window(root)
    try:
        assert w.source_hint.get() == "완성 MP4 1개를 반복 송출합니다."
        assert shown(w.btn_video) and not shown(w.btn_pl_add)
        w.source_mode.set("playlist")
        w._on_source_mode()
        assert "위에서 아래 순서로 반복" in w.source_hint.get()
        for b in (w.btn_pl_add, w.btn_pl_remove, w.btn_pl_up, w.btn_pl_down, w.btn_pl_clear, w.ptree):
            assert shown(b)
        assert [w.ptree.heading(c)["text"] for c in w.ptree["columns"]] == ["순서", "파일명", "길이", "해상도", "FPS", "상태"]
        w.source_mode.set("single")
        w._on_source_mode()
        assert shown(w.btn_video) and not shown(w.btn_pl_add) and "1개" in w.source_hint.get()
    finally:
        w.destroy()


def test_schedule_window_reads_connection_without_reopen(root, monkeypatch):
    from tkinter import messagebox

    from app.youtube_live_schedule_ui import LiveScheduleWindow
    from youtube_fakes import FakeClock
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(messagebox, name, lambda *a, **k: None)
    state = {"ok": False}
    save_youtube_settings(channel_title="CHILI LAB")
    clock = FakeClock(datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc).timestamp())
    w = LiveScheduleWindow(root, api_factory=lambda: None, connected=lambda: state["ok"], clock=clock,
                           playlist_source=lambda: [], tools=lambda: (Path("ffmpeg"), Path("ffprobe")),
                           cloud_factory=lambda: None, cloud_configured=lambda: True,
                           analyze=lambda paths, ffprobe: [])
    try:
        assert "연결되지 않았습니다" in w.account.get() and str(w.btn_create.cget("state")) == "disabled"
        state["ok"] = True  # LIVE 창에서 YouTube 연결 완료
        assert pump(root, lambda: w.account.get() == "✓ YouTube 연결됨 · 채널: CHILI LAB")
        assert pump(root, lambda: str(w.btn_create.cget("state")) == "normal")
    finally:
        w.destroy()
