"""여러 채널 Cloud LIVE — LIVE 창/메인 화면/채널 관리 창 (가짜 controller, 실제 Cloud/YouTube 접속 없음)."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cloud_client import CloudLiveController, CloudStatus
from app.cloud_model import CONCURRENT_BUSY
from app.live_channels import LiveChannelStore, key_store_for
from app.live_secrets import SessionStreamKeyStore


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


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from tkinter import messagebox
    shown = []
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(messagebox, name, lambda *a, _n=name, **k: shown.append((_n, a)))
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    return shown


class SpyController(CloudLiveController):
    def __init__(self, pid):
        super().__init__(lambda: None, poll_seconds=3600, profile_id=pid)
        self.pid = pid
        self.calls = []

    def stop_async(self):
        self.calls.append("stop")
        self.status = CloudStatus(reachable=True, installed=True, state="STOPPED", profile_id=self.pid or "default")
        self.live_started = False
        return True

    def start_async(self, **kw):
        self.calls.append(("start", kw))
        return True

    def go_live(self, seconds=61.0, bitrate="6300.0kbits/s"):
        self.status = CloudStatus(reachable=True, installed=True, service_active=True, state="RUNNING",
                                  runtime_seconds=seconds, bitrate=bitrate, profile_id=self.pid or "default")


def make_window(root, *, channels=("senior", "chili")):
    from app.live_ui import LiveWindow
    store = LiveChannelStore()
    names = {"senior": "시니어 채널", "chili": "일본 채널", "third": "세번째 채널"}
    for pid in channels:
        store.add(names[pid], profile_id=pid)
    ctls = {}

    def factory(pid):
        ctls[pid] = SpyController(pid)
        return ctls[pid]
    default = SpyController(None)
    ctls["default"] = default
    w = LiveWindow(root, tools=lambda: (None, None), key_store=SessionStreamKeyStore(), cloud=default,
                   channels=store, cloud_factory=factory)
    return w, store, ctls


def pick(w, pid):
    w.cmb_channel.current(w._channel_ids.index(pid))
    w._on_channel_selected()


def test_channel_switch_isolates_key_playlist_and_controller(root, tmp_path):
    w, store, ctls = make_window(root)
    try:
        assert w._channel_ids == ["default", "senior", "chili"] and str(w.cmb_channel.cget("state")) == "readonly"
        w.key_var.set("default-key-not-real")
        a, b = tmp_path / "a_LIVE_READY.mp4", tmp_path / "b_LIVE_READY.mp4"
        for p in (a, b):
            p.write_bytes(b"x")
        pick(w, "senior")
        assert w.channel_id == "senior" and w.cloud is ctls["senior"] and w.key_var.get() == ""
        assert "시니어 채널" in w.f_video.cget("text")
        w.source_mode.set("playlist")
        w._on_source_mode()
        w.playlist.add(a)
        w.playlist.add(b)
        w.key_var.set("senior-key-not-real")
        w.remember.set(True)
        w._apply_remember()
        pick(w, "chili")
        assert w.cloud is ctls["chili"] and len(w.playlist) == 0 and w.key_var.get() == ""
        pick(w, "senior")  # 돌아오면 그 채널 Playlist / Key 그대로
        assert [p.name for p in w.playlist.paths] == [a.name, b.name] and w.key_var.get() == "senior-key-not-real"
        assert store.get("senior").media_playlist == [str(a), str(b)]  # 경로만 저장 (재실행 후 복구용)
        assert key_store_for("senior").get() == "senior-key-not-real" and key_store_for("chili").get() is None
        pick(w, "default")
        assert w.cloud is ctls["default"] and w.store is w._default_store
        assert store.selected_id() == "default"
    finally:
        w.destroy()


def test_independent_stop_and_third_channel_blocked(root, quiet):
    w, store, ctls = make_window(root, channels=("senior", "chili", "third"))
    try:
        pick(w, "senior")
        ctls["senior"].go_live(6162.0)
        pick(w, "chili")  # LIVE 중인 다른 채널이 있어도 채널을 바꿀 수 있다
        ctls["chili"].go_live(37 * 60 + 5.0)
        w._tick()
        assert w.any_cloud_live and len(w.live_channels()) == 2
        assert "시니어 채널" in w.channel_live_text.get() and "일본 채널" in w.channel_live_text.get()
        assert "1:42:42" in w.channel_live_text.get() or "01:42:42" in w.channel_live_text.get()
        assert "예상 Cloud 송출 대역폭" in w.bandwidth_text.get()
        assert any("● LIVE" in v for v in w.cmb_channel.cget("values"))
        # 세 번째 채널: 시작 거부 (다른 채널은 그대로)
        pick(w, "third")
        w._start()
        assert ("showwarning", ("Cloud LIVE", CONCURRENT_BUSY)) in [(n, a[:2]) for n, a in quiet]
        assert not any(c[0] == "start" for c in ctls["third"].calls if isinstance(c, tuple))
        # 일본 채널 중지 → 시니어는 계속
        pick(w, "chili")
        w._stop()
        assert ctls["chili"].calls == ["stop"] and ctls["senior"].calls == [] and ctls["default"].calls == []
        assert ctls["senior"].cloud_live_active and not ctls["chili"].cloud_live_active
        pick(w, "senior")
        w._stop()
        assert ctls["senior"].calls == ["stop"] and ctls["chili"].calls == ["stop"]
    finally:
        w.destroy()


def test_main_window_shows_each_live_channel(root):
    from app.ui import MainWindow
    w, store, ctls = make_window(root)
    try:
        ctls["default"].go_live(5.0)
        w._controller_for("senior").go_live(6136.0)
        fake = SimpleNamespace(_live_window=lambda: w)
        fake._live_kind = lambda: MainWindow._live_kind(fake)
        text = MainWindow._live_state_text(fake)
        assert text.startswith("실시간 LIVE ") and "기본 채널 ●" in text and "시니어 채널 ● 01:42:16" in text
    finally:
        w.destroy()


def test_close_dialog_lists_live_channels_and_pc_only_keeps_them(root, monkeypatch):
    import app.live_ui as live_ui
    w, store, ctls = make_window(root)
    asked = []
    monkeypatch.setattr(live_ui, "ask_cloud_close", lambda parent, msg: asked.append(msg) or "pc")
    try:
        w._controller_for("senior").go_live()
        w._controller_for("chili").go_live()
        done = []
        w.confirm_close(lambda: done.append(1), for_app=True)
        assert done and "시니어 채널" in asked[0] and "일본 채널" in asked[0]
        assert ctls["senior"].calls == [] and ctls["chili"].calls == []  # PC만 종료: Cloud에 아무 명령 없음
    finally:
        w.destroy()


def test_channel_dialog_add_link_confirm_and_delete(root, quiet, monkeypatch):
    from tkinter import messagebox

    from app import youtube_accounts as ya
    from app.live_channels_ui import LiveChannelsDialog
    profiles = ya.ProfileStore()
    acct = profiles.add(ya.ChannelProfile(ya.new_profile_id(), "칠리랩 계정", channel_id="UCchiliAAAA",
                                          channel_title="CHILI LAB", client_file="c.json"))
    store = LiveChannelStore()
    changed = []
    d = LiveChannelsDialog(root, store=store, on_change=lambda: changed.append(1), profiles=profiles,
                           is_live=lambda pid: pid == "busy")
    try:
        d.new_name.set("CHILI LAB 채널")
        d.new_id.set("chili")
        assert d.add() and store.get("chili") is not None and changed
        d.new_name.set("이름만")
        d.new_id.set("Bad ID!")
        assert not d.add()
        d.tree.selection_set("chili")
        d._on_select()
        d.cmb_link.current(d._link_ids.index(acct.profile_id))
        asked = []
        monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: asked.append(a[1]) or True)
        assert d.apply_link()
        assert "CHILI LAB" in asked[0]  # 실제 YouTube 채널 이름을 확인하고 승인
        ch = store.get("chili")
        assert ch.oauth_profile_id == acct.profile_id and ch.youtube_channel_id == "UCchiliAAAA"
        assert d.tree.set("chili", "youtube").startswith("CHILI LAB")
        d.tree.selection_set("default")
        d._on_select()
        assert str(d.btn_delete.cget("state")) == "disabled"
        d.tree.selection_set("chili")
        d._on_select()
        assert d.delete_selected() and store.get("chili") is None
    finally:
        d.destroy()


def test_schedule_window_channel_picker_uses_channel_profile(root, monkeypatch):
    from datetime import datetime, timezone

    from app import youtube_accounts as ya
    from app.youtube_live_schedule_ui import LiveScheduleWindow
    from youtube_fakes import FakeClock
    profiles = ya.ProfileStore()
    acct = profiles.add(ya.ChannelProfile(ya.new_profile_id(), "일본 계정", channel_id="UCjpBBBB",
                                          channel_title="CHILI LAB", client_file="c.json", stream_id="st_jp"))
    profiles.token_store(acct.profile_id).save("refresh-not-real", client_id="cid")
    store = LiveChannelStore()
    store.add("일본 채널", profile_id="japan", oauth_profile_id=acct.profile_id)
    store.select("japan")
    clock = FakeClock(datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc).timestamp())
    w = LiveScheduleWindow(root, api_factory=lambda: None, connected=lambda: False, clock=clock,
                           playlist_source=lambda: [], tools=lambda: (Path("ffmpeg"), Path("ffprobe")),
                           cloud_factory=lambda: None, cloud_configured=lambda: True, analyze=lambda p, f: [])
    try:
        assert w.channel_id == "japan" and w.channel_row.winfo_manager() == "pack"
        ok, title, stream = w._channel_youtube()
        assert ok and title == "CHILI LAB" and stream == "st_jp"
        assert w.account.get() == "✓ YouTube 연결됨 · 채널: CHILI LAB"
        w.cmb_channel.current(0)
        w._on_channel()
        assert w.channel_id == "default" and "연결되지 않았습니다" in w.account.get()  # 기본 채널 = 기존 연결(없음)
    finally:
        w.destroy()
