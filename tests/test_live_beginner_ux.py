"""LIVE 2채널 초보자 UX — 채널 A/B 카드 · 서버 주소 실수 방지 · 빠른 시작 · 준비 체크리스트 · 방송 정보 안내.
가짜 controller만 사용 (실제 YouTube/OCI 접속 없음)."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cloud_client import CloudLiveController, CloudStatus
from app.cloud_model import CONCURRENT_BUSY
from app.live_channels import LiveChannelStore
from app.live_readiness import (
    KEY_IS_URL, SERVER_BAD_FORMAT, SERVER_EMPTY, SERVER_HAS_KEY, ST_LIVE, ST_READY, ST_UNSET, card_state, channel_checklist,
    channel_label, check_server_and_key, missing, summary_line,
)
from app.live_secrets import SessionStreamKeyStore

KEY_A, KEY_B, KEY_C = "aaaa-bbbb-cccc-dddd-eeee", "ffff-gggg-hhhh-iiii-jjjj", "kkkk-llll-mmmm-nnnn-oooo"  # 가짜


# ---------------- 계산 (Tk 없음) ----------------

def test_server_and_key_confusion_checks():
    assert check_server_and_key("youtube", "", KEY_A).ok  # 기본 서버: 문제 없음
    r = check_server_and_key("custom", "", KEY_A)
    assert r.errors == [SERVER_EMPTY] and "YouTube 기본 서버 사용" in SERVER_EMPTY
    assert check_server_and_key("custom", "a.rtmp.youtube.com/live2", KEY_A).errors == [SERVER_BAD_FORMAT]
    assert check_server_and_key("custom", "https://a.rtmp.youtube.com/live2", KEY_A).errors == [SERVER_BAD_FORMAT]
    ok = check_server_and_key("custom", "rtmp://a.rtmp.youtube.com/live2", KEY_A)
    assert ok.ok and not ok.warnings
    for url in (f"rtmp://a.rtmp.youtube.com/live2/{KEY_A}", "rtmp://a.rtmp.youtube.com/live2/whatever",
                f"rtmp://my.server.example/app/{KEY_B}"):
        r = check_server_and_key("custom", url, KEY_A)
        assert r.ok and r.warnings == [SERVER_HAS_KEY], url  # 경고 (사용자가 확인하면 시작 가능)
    assert check_server_and_key("youtube", "", "rtmp://a.rtmp.youtube.com/live2").errors == [KEY_IS_URL]
    assert check_server_and_key("youtube", "", "abc def").errors and check_server_and_key("youtube", "", "a/b").errors
    assert check_server_and_key("custom", "", "", api_mode=True).ok  # API 모드: YouTube API가 관리
    for msgs in (SERVER_EMPTY, SERVER_BAD_FORMAT, SERVER_HAS_KEY, KEY_IS_URL):
        assert KEY_A not in msgs  # Key 값은 문구에 넣지 않음


def test_labels_checklist_and_states():
    assert channel_label(0, "기본 채널", "default") == "채널 A (기본)"
    assert channel_label(0, "시니어 채널", "default") == "시니어 채널 (채널 A)"  # v1.1: 실제 이름 우선
    assert channel_label(1, "일본 CHILI LAB", "chili") == "일본 CHILI LAB"
    assert channel_label(2, "", "x") == "채널 C"
    base = dict(media_count=0, ready=None, location_cloud=True, cloud_configured=False, key_present=False,
                server_mode="youtube", custom_url="", stream_key="", api_mode=False, yt_connected=False)
    items = channel_checklist(**base)
    assert [i.label for i in items] == ["영상 선택", "LIVE READY 확인", "Cloud/내 PC 선택", "Stream Key 입력",
                                        "서버 주소 확인", "YouTube 연결 필요 여부"]
    assert [i.key for i in missing(items)] == ["media", "ready", "location", "key"]
    assert items[-1].ok and "필요 없음" in items[-1].hint  # Stream Key 방식은 YouTube 연결 없이 송출 가능
    full = channel_checklist(**{**base, "media_count": 2, "ready": True, "cloud_configured": True, "key_present": True,
                                "stream_key": KEY_A})
    assert not missing(full) and summary_line(full).startswith("✓ 시작 가능")
    bad = channel_checklist(**{**base, "media_count": 1, "ready": False, "cloud_configured": True, "key_present": True,
                               "server_mode": "custom", "custom_url": ""})
    assert [i.key for i in missing(bad)] == ["ready", "server"] and "부족" in summary_line(bad)
    api = channel_checklist(**{**base, "media_count": 1, "ready": True, "cloud_configured": True, "api_mode": True})
    assert [i.key for i in missing(api)] == ["youtube"]  # API 자동 세션은 연결 필요
    assert card_state(live=True, busy=False, failed=False, ready=False) == ST_LIVE
    assert card_state(live=False, busy=False, failed=False, ready=True) == ST_READY
    assert card_state(live=False, busy=False, failed=False, ready=False) == ST_UNSET


# ---------------- 화면 ----------------

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


@pytest.fixture
def shown_boxes(monkeypatch):
    from tkinter import messagebox
    shown = []
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(messagebox, name, lambda *a, _n=name, **k: shown.append((_n, a[0] if a else "", a[1] if len(a) > 1 else "")))
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: shown.append(("askyesno", a[0], a[1])) or True)
    return shown


class Spy(CloudLiveController):
    def __init__(self, pid):
        super().__init__(lambda: None, poll_seconds=3600, profile_id=pid)
        self.pid, self.calls = pid, []

    def stop_async(self):
        self.calls.append("stop")
        self.status = CloudStatus(reachable=True, installed=True, state="STOPPED")
        return True

    def go_live(self, seconds=61.0):
        self.status = CloudStatus(reachable=True, installed=True, service_active=True, state="RUNNING",
                                  runtime_seconds=seconds, bitrate="6300.0kbits/s")


def make(root, *, channels=(("senior", "일본 CHILI LAB"),), rename_default=None):
    from app.live_ui import LiveWindow
    store = LiveChannelStore()
    for pid, name in channels:
        store.add(name, profile_id=pid)
    if rename_default:
        d = store.get("default")
        d.display_name = rename_default
        store.save(d)
    ctls = {"default": Spy(None)}

    def factory(pid):
        ctls[pid] = Spy(pid)
        return ctls[pid]
    w = LiveWindow(root, tools=lambda: (None, None), key_store=SessionStreamKeyStore(), cloud=ctls["default"],
                   channels=store, cloud_factory=factory)
    w._cloud_cfg = True  # 무료 Cloud 준비됨 (가짜)
    return w, store, ctls


def shown(widget) -> bool:
    x = widget
    while x is not None and x.winfo_class() not in ("Tk", "Toplevel"):
        if not x.winfo_manager():
            return False
        x = x.master
    return True


def make_ready(w, tmp_path, key, name="a_LIVE_READY.mp4"):
    """지금 채널을 '시작 가능' 상태로 (가짜 LIVE READY 결과, 실제 FFmpeg 없음)."""
    p = tmp_path / name
    p.write_bytes(b"x")
    w.source_mode.set("single")
    w._on_source_mode()
    w.input_path.set(str(p))
    w.ready_report = SimpleNamespace(ready=True, summary_lines=lambda: ["LIVE READY (테스트)"], duration=10.0)
    w.key_var.set(key)
    w._cloud_cfg = True  # 채널을 바꾸면 설정에서 다시 읽으므로 (테스트에는 Cloud 설정이 없음) 매번 가짜로 준비됨


def test_beginner_mode_default_hides_server_input(root, shown_boxes):
    w, store, ctls = make(root)
    try:
        assert not w.advanced.get() and "초보자 모드" in w.title_text.get()
        assert shown(w.server_auto) and not shown(w.server_adv) and not shown(w.ent_custom) and not shown(w.rb_custom)
        assert w.server_mode.get() == "youtube"
        assert "Stream Key 방식이라" in w.yt_status.get()  # 쉬운 말: 연결 없이도 송출 가능
        w.show_server_help()
        assert any("서버 주소와 Stream Key는 서로 다른 값" in m for _, _, m in shown_boxes)
    finally:
        w.destroy()


def test_advanced_mode_enables_server_input_with_warnings(root, shown_boxes, tmp_path):
    from app.settings import load_settings
    w, store, ctls = make(root)
    try:
        w.advanced.set(True)
        w._on_advanced()
        assert load_settings()["live_advanced_mode"] is True and "고급 모드" in w.title_text.get()
        assert shown(w.server_adv) and not shown(w.server_auto) and not shown(w.custom_box)
        w.server_mode.set("custom")
        w._sync_widgets()
        assert shown(w.ent_custom) and str(w.ent_custom.cget("state")) == "normal"
        assert "서버 주소만 넣습니다" in str(w.custom_box.winfo_children()[0].cget("text"))
        w.custom_url.set(f"rtmp://a.rtmp.youtube.com/live2/{KEY_A}")
        w.key_var.set(KEY_A)
        w._sync_widgets()
        assert "Stream Key가 같이 들어간" in w.server_check_text.get() and KEY_A not in w.server_check_text.get()
        # 시작 전 검사: 경고 → [아니요] 이면 시작하지 않음
        make_ready(w, tmp_path, KEY_A)
        from tkinter import messagebox
        import app.live_ui as live_ui
        asked = []
        live_ui.messagebox.askyesno = lambda *a, **k: asked.append(a[1]) or False
        started = []
        w.cloud.start_async = lambda **kw: started.append(kw) or True
        w._start()
        assert asked and SERVER_HAS_KEY in asked[0] and not started
        # 빈 주소 → 시작 차단 (쉬운 말)
        w.custom_url.set("")
        w._start()
        assert ("showerror", "시작할 수 없습니다", SERVER_EMPTY) in shown_boxes and not started
        # Key 칸에 서버 주소 → 차단
        w.server_mode.set("youtube")
        w.key_var.set("rtmp://a.rtmp.youtube.com/live2")
        w._start()
        assert ("showerror", "시작할 수 없습니다", KEY_IS_URL) in shown_boxes and not started
        # 초보자 모드로 돌아가면 기본 서버
        w.server_mode.set("custom")
        w.custom_url.set("rtmp://x.example/live2")
        live_ui.messagebox.askyesno = lambda *a, **k: True
        w.advanced.set(False)
        w._on_advanced()
        assert w.server_mode.get() == "youtube" and shown(w.server_auto) and not shown(w.ent_custom)
        del messagebox
    finally:
        w.destroy()


def test_channel_cards_show_a_and_b(root, shown_boxes, monkeypatch):
    w, store, ctls = make(root, channels=(), rename_default="시니어 채널")
    try:
        w._tick()
        a, b = w.channel_cards
        assert a.title.get() == "시니어 채널 (채널 A)" and a.state.get().startswith("● 미설정")
        assert "Stream Key: 없음" in a.lines.get() and "서버 주소: 자동 (YouTube 기본)" in a.lines.get()
        assert "실행 위치: 무료 Cloud" in a.lines.get() and "부족" in a.ready.get()
        assert b.pid is None and shown(b.btn_create) and str(b.btn_start.cget("state")) == "disabled"
        assert str(w.btn_quick_b.cget("state")) == "disabled" and "시니어 채널만 시작" in w.btn_quick_a.cget("text")
        import app.live_ui_beginner as lub
        monkeypatch.setattr(lub.simpledialog, "askstring", lambda *a, **k: "일본 CHILI LAB")
        b.create()
        w._tick()
        assert b.pid is not None and b.title.get() == "두 번째 송출 채널" and not shown(b.btn_create)
        assert b.secondary_var.get() == "일본 CHILI LAB"  # 두 번째 카드는 고른 채널 이름을 보여 줌
        assert "일본 CHILI LAB만 시작" in w.btn_quick_b.cget("text") and str(w.btn_quick_both.cget("state")) == "normal"
        assert "시니어 채널 (채널 A)" in w.cmb_channel.cget("values")
        # 카드 [이 채널 설정하기] → 지금 설정하는 채널이 바뀜
        b.select()
        w._tick()
        assert w.channel_id == b.pid and shown(b.lbl_now) and not shown(a.lbl_now)
        assert "일본 CHILI LAB" in w.check_title.get() and len(w.check_vars) == 6
    finally:
        w.destroy()


def test_start_a_b_both_third_blocked_and_independent_stop(root, shown_boxes, tmp_path):
    w, store, ctls = make(root, channels=(("chili", "일본 CHILI LAB"), ("third", "세번째 채널")))
    starts = []
    w._start_real = w._start

    def fake_start(api_stream=None):  # 실제 송출 대신: 어느 채널에서 시작했는지만 기록 → 가짜 LIVE
        starts.append(w.channel_id)
        ctls[w.channel_id if w.channel_id != "default" else "default"].go_live()
    try:
        # 준비 안 됨 → 쉬운 말로 부족한 것 안내, 시작 안 함
        assert not w.start_channel("default")
        assert shown_boxes[-1][1] == "아직 시작할 수 없습니다" and "Stream Key 입력" in shown_boxes[-1][2]
        w._start = fake_start
        make_ready(w, tmp_path, KEY_A, "a_LIVE_READY.mp4")
        assert w.start_channel("default") is not None and starts == ["default"]  # 채널 A만
        w._switch_channel("chili")
        make_ready(w, tmp_path, KEY_B, "b_LIVE_READY.mp4")
        w.start_channel("chili")  # 채널 B (A와 동시)
        w._tick()
        assert starts == ["default", "chili"] and w.channel_is_live("default") and w.channel_is_live("chili")
        a, b = w.channel_cards
        assert a.state.get().startswith(f"● {ST_LIVE}") and b.state.get().startswith(f"● {ST_LIVE}")
        assert str(a.btn_stop.cget("state")) == "normal" and str(a.btn_start.cget("state")) == "disabled"
        assert "실행 중 채널 2 / 최대 2" in w.overall_text.get()
        # 세 번째 채널: 실제 _start의 동시 송출 제한 검사에서 거부
        w._start = w._start_real
        w._switch_channel("third")
        make_ready(w, tmp_path, KEY_C, "c_LIVE_READY.mp4")
        w.start_channel("third")
        assert ("showwarning", "Cloud LIVE", CONCURRENT_BUSY) in shown_boxes and not w.channel_is_live("third")
        # 채널 A 중지 (지금 보는 채널이 아님) → B 유지
        a.stop()
        assert ctls["default"].calls == ["stop"] and ctls["chili"].calls == [] and w.channel_is_live("chili")
        # 채널 B 중지 → (A는 이미 중지) 다른 채널에 명령 없음
        b.stop()
        assert ctls["chili"].calls == ["stop"] and ctls["default"].calls == ["stop"]
        assert not any(k in str(shown_boxes) for k in (KEY_A, KEY_B, KEY_C))
    finally:
        w.destroy()


def test_quick_start_wizard_two_channels(root, shown_boxes, tmp_path):
    w, store, ctls = make(root)
    started = []

    def fake_start(api_stream=None):
        started.append(w.channel_id)
        ctls[w.channel_id].go_live()
    try:
        w._start = fake_start
        wiz = w.open_quick_start(["A", "B"])
        root.update()
        assert wiz.head.get().startswith("STEP 1 / 5 · 채널 확인") and "(1/2 채널)" in wiz.head.get()
        assert "채널 A (기본)" in wiz.body.get() and wiz.todo.get().startswith("지금 할 일")
        wiz.next(); assert "영상" in wiz.head.get() and "[이 단계 하러 가기]" in wiz.todo.get()
        make_ready(w, tmp_path, KEY_A)
        wiz.render(); assert wiz.todo.get() == "지금 할 일: [다음 ▶]"
        wiz.next(); wiz.next(); wiz.next()
        assert wiz.head.get().startswith("STEP 5 / 5 · 시작") and wiz.btn_next.cget("text") == "▶ 이 채널 시작"
        wiz.btn_next.invoke()
        assert started == ["default"] and wiz.btn_next.cget("text") == "다음 채널 ▶"
        wiz.btn_next.invoke()
        assert w.channel_id == "senior" and "(2/2 채널)" in wiz.head.get()
        for _ in range(4):
            wiz.next()
        assert str(wiz.btn_next.cget("state")) == "disabled"  # B는 아직 영상/Key 없음
        make_ready(w, tmp_path, KEY_B, "b_LIVE_READY.mp4")
        wiz.render()
        wiz.btn_next.invoke()
        assert started == ["default", "senior"] and wiz.btn_next.cget("text") == "닫기"
        wiz.destroy()
        # 이미 2채널 LIVE면 빠른 시작 자체를 막음
        assert w.open_quick_start(["A"]) is not None  # A는 이미 송출 중 → 새로 필요한 자리 0
        store.add("세번째 채널", profile_id="third")
        w._refresh_channel_list()
    finally:
        w.destroy()


def test_small_window_scrolls_and_cards_stack(root, shown_boxes):
    w, store, ctls = make(root)
    try:
        w.geometry("640x420")
        w.deiconify()
        w.update()
        w._tick()
        w.update()
        assert w._cards_stacked is True  # 좁은 화면: 카드를 세로로 쌓음
        canvas = w._canvas
        box = canvas.bbox("all")
        assert box[3] - box[1] > canvas.winfo_height()  # 스크롤이 필요한 높이
        before = canvas.yview()[0]
        w.scroll_to(w.btn_start)
        assert canvas.yview()[0] > before  # 시작 버튼까지 스크롤로 접근 가능
        w.geometry("1100x700")
        w.update()
        assert w._cards_stacked is False
    finally:
        w.destroy()


def test_metadata_info_and_placeholders(root, shown_boxes):
    w, store, ctls = make(root)
    try:
        assert not shown(w.meta_frame)
        w.btn_meta.invoke()
        assert shown(w.meta_frame) and set(w.meta_widgets) == {"title", "description", "privacy", "thumbnail",
                                                                 "category", "playlist"}
        assert all(str(e.cget("state")) == "disabled" for e in w.meta_widgets.values())
        assert w.meta_widgets["playlist"].get() == "추후 지원 예정 · API 연결 시 사용"
    finally:
        w.destroy()


def test_main_window_quick_links(root, shown_boxes):
    from app.ui import MainWindow
    w, store, ctls = make(root)
    try:
        assert MainWindow._quick_live_text("A") == "▶ 채널 A 시작"
        assert MainWindow._quick_live_text("B") == "▶ 일본 CHILI LAB 시작"
        fake = SimpleNamespace(_open_live=lambda: None, _live_window=lambda: w)
        wiz = MainWindow._live_quick(fake, ["B"])
        assert wiz is not None and w.channel_id == "senior" and wiz.targets == ["senior"]
        wiz.destroy()
    finally:
        w.destroy()


def test_usage_help_mentions_two_channels_and_server_key():
    from app import help_content as hc
    title, steps = hc.FEATURE_USAGE["live"]
    assert len(steps) == 5 and title == "실시간 LIVE 사용법" and "채널 카드" in steps[0] and "Stream Key만" in steps[2]
    body = hc.topic("live").body
    for s in ("채널 1개 시작하기", "2채널 동시 시작하기", "서버 주소와 Stream Key는 다릅니다", "rtmp://a.rtmp.youtube.com/live2"):
        assert s in body
