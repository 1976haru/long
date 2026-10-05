"""3-in-1 GUI smoke: 메인 3개 모드 카드/상태 요약, 채널 관리 창, ③ 예약 업로드 창, [예약 업로드로 보내기], 예약 LIVE 창.
fake 서버/주입 함수만 사용, 실제 Google/YouTube 접속 없음."""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.youtube_accounts import ChannelProfile, ProfileStore, new_profile_id
from app.youtube_api import YouTubeApiClient
from app.youtube_upload_queue import BLOCKED, COMPLETE
from test_youtube_upload_queue import JP, KR, Env, jpeg
from youtube_fakes import FAKE_ACCESS, FakeYouTube


@pytest.fixture
def fake():
    f = FakeYouTube()
    yield f
    f.close()


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
        monkeypatch.setattr(messagebox, name, lambda *a, **k: shown.append(a))
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    return shown


def pump(root, cond, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.03)
    return cond()


def client_json(tmp_path, name="client_secret_kr.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"installed": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "GOCSPX-fake",
                                           "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                                           "token_uri": "https://oauth2.googleapis.com/token"}}))
    return p


# ---------------- 채널 관리 ----------------

def test_channel_manager_add_connect_disconnect_delete(root, tmp_path, _isolated_settings, quiet):
    from app.youtube_channels_ui import ChannelManagerWindow
    ps = ProfileStore()
    connected = []

    def fake_connect(profiles, p, path, open_browser):
        connected.append((p.alias, path))
        ch = KR if "한국" in p.alias else JP
        p.client_file, p.channel_id, p.channel_title = path, ch["id"], ch["title"]
        profiles.token_store(p.profile_id).save("refresh-" + p.profile_id, client_id="cid.apps.googleusercontent.com")
        return profiles.save(p)
    w = ChannelManagerWindow(root, profiles=ps, connect=fake_connect, open_browser=lambda u: None)
    w.new_profile()
    w.alias.set("🇰🇷 한국 시니어")
    w.client_file.set(str(client_json(tmp_path)))
    assert w.save_form() is not None
    w.new_profile()
    w.alias.set("🇯🇵 CHILI LAB")
    w.language.set("日本語 (ja)")
    w.timezone.set("Asia/Tokyo")
    w.client_file.set(str(client_json(tmp_path, "client_secret_jp.json")))
    w.start_connect()
    assert pump(root, lambda: "연결됨" in w.message.get()), w.message.get()
    jp = next(p for p in ps.all() if p.alias == "🇯🇵 CHILI LAB")
    assert (jp.language, jp.timezone, jp.channel_id) == ("ja", "Asia/Tokyo", JP["id"])
    assert w.tree.set(jp.profile_id, "channel") == JP["title"] and "연결됨" in w.tree.set(jp.profile_id, "state")
    # 같은 별칭은 거부
    w.new_profile()
    w.alias.set("🇯🇵 CHILI LAB")
    assert w.save_form() is None and "별칭" in w.message.get()
    # 잘못된 OAuth JSON이면 브라우저를 열지 않음
    kr = next(p for p in ps.all() if p.alias == "🇰🇷 한국 시니어")
    w.tree.selection_set(kr.profile_id)
    w._on_select()
    bad = tmp_path / "web.json"
    bad.write_text(json.dumps({"web": {"client_id": "x", "client_secret": "y"}}))
    w.client_file.set(str(bad))
    w.start_connect()
    assert "OAuth JSON" in w.message.get() and len(connected) == 1
    # 연결 해제 / 삭제
    w.tree.selection_set(jp.profile_id)
    w._on_select()
    w.disconnect_selected()
    assert ps.get(jp.profile_id).channel_id == "" and not ps.token_store(jp.profile_id).has_saved()
    w.delete_selected()
    assert ps.get(jp.profile_id) is None and [p.alias for p in ps.all()] == ["🇰🇷 한국 시니어"]
    text = (_isolated_settings / "settings.json").read_text(encoding="utf-8")
    assert "GOCSPX" not in text and "refresh-" not in text
    w.destroy()


# ---------------- ③ 예약 업로드 창 ----------------

@pytest.fixture
def studio(fake, tmp_path):
    ps = ProfileStore()
    kr = ps.add(ChannelProfile(new_profile_id(), "🇰🇷 한국 시니어", channel_id=KR["id"], channel_title=KR["title"],
                               language="ko", timezone="Asia/Seoul"))
    jp = ps.add(ChannelProfile(new_profile_id(), "🇯🇵 CHILI LAB", channel_id=JP["id"], channel_title=JP["title"],
                               language="ja", timezone="Asia/Tokyo"))
    env = Env(fake, ps, kr, jp)
    video = tmp_path / "LONG_FINAL.mp4"
    video.write_bytes(bytes(range(256)) * (4 * 1024 + 7))
    return env, ps, kr, jp, video


def upload_window(root, env, ps, **kw):
    from app.youtube_upload_ui import MultiChannelUploadWindow
    q = env.queue(ps)
    return MultiChannelUploadWindow(root, upload_queue=q, clock=env.clock, **kw), q


def fill(w, profile, video, title, day, at="18:00"):
    w.select_profile(profile.profile_id)
    w.clear_items()
    w.add_videos([str(video)])
    w.title_template.set(title)
    w.pub_date.set(day)
    w.pub_time.set(at)


def add_via_preview(root, w):
    """[미리보기] → 실제 채널 확인이 끝날 때까지 기다림 → [N개 대기열에 추가]."""
    dlg = w.preview()
    if dlg is None:
        return None
    assert pump(root, lambda: dlg.verify_state != "pending", timeout=20)
    return dlg.confirm()


def test_upload_window_kr_jp_schedule_and_run(root, studio, fake, tmp_path, quiet):
    env, ps, kr, jp, video = studio
    w, q = upload_window(root, env, ps)
    assert len(w.cb_profile.cget("values")) == 2
    tomorrow = (datetime.fromtimestamp(env.clock(), timezone.utc) + timedelta(days=1)).date().isoformat()
    fill(w, kr, video, "한국 영상", tomorrow, "18:00")
    w.tags.set("샹송, 올드팝")
    w.items[0].thumb.path, w.items[0].thumb.status = str(jpeg(tmp_path / "t.jpg")), "manual"
    assert add_via_preview(root, w)
    fill(w, jp, video, "日本の動画", tomorrow, "18:00")  # 같은 영상 → 경고만 (차단 아님)
    assert add_via_preview(root, w)
    a, b = q.snapshot()
    assert a.publish_local_text().endswith("18:00 Asia/Seoul") and b.publish_local_text().endswith("18:00 Asia/Tokyo")
    assert a.publish_at_utc == b.publish_at_utc == tomorrow + "T09:00:00Z"  # KST/JST 모두 UTC+9 · 채널 시간대 기준 18:00
    assert (a.title, b.title) == ("한국 영상", "日本の動画") and a.tags == ["샹송", "올드팝"]
    assert w.items == []  # 추가 후 다음 영상 입력 준비
    # 잘못된 날짜 / 지난 시각 → 미리보기 전에/미리보기에서 차단
    fill(w, kr, video, "x", "2026/10/06")
    assert w.preview() is None and "형식" in str(quiet[-1])
    fill(w, kr, video, "x", "2020-01-01")
    dlg = w.preview()
    assert dlg.errors and str(dlg.btn_add.cget("state")) == "disabled" and dlg.confirm() is None
    dlg.destroy()
    w.start()
    assert pump(root, lambda: all(j.status == COMPLETE for j in q.snapshot()) and not q.running, timeout=30)
    assert pump(root, lambda: str(w.btn_start.cget("state")) == "normal")
    rows = [w.tree.item(i, "values") for i in w.tree.get_children()]
    assert [r[4] for r in rows] == ["예약 완료", "예약 완료"] and rows[0][5] == "100%"
    assert "예약 완료 2" in w.summary.get()
    vids = [j.video_id for j in q.snapshot()]
    assert fake.videos[vids[0]]["channel"] == KR["id"] and fake.videos[vids[1]]["channel"] == JP["id"]
    assert fake.thumbnails[vids[0]]
    w.clear_done()
    assert w.tree.get_children() == ()
    w.destroy()


def test_upload_window_shows_blocked_wrong_channel(root, studio, fake):
    env, ps, kr, jp, video = studio
    w, q = upload_window(root, env, ps)
    w.publish_kind.set("now")
    w._on_kind()
    assert str(w.ent_date.cget("state")) == "disabled"
    fill(w, kr, video, "차단될 영상", "")
    job = add_via_preview(root, w)[0]
    env.accounts[kr.profile_id] = (JP, "tok-wrong")  # 미리보기 뒤에 계정이 바뀌어도 업로드 직전 확인에서 차단
    w.start()
    assert pump(root, lambda: q.snapshot()[0].status == BLOCKED and not q.running, timeout=20)
    assert pump(root, lambda: w.tree.set(job.job_id, "state") == "차단 (채널 불일치)")
    w.tree.selection_set(job.job_id)
    w._on_select()
    assert "채널 불일치" in w.detail.get() and fake.sessions == {}
    w.destroy()


def test_upload_window_opens_channel_manager_and_refreshes(root, studio):
    env, ps, kr, jp, video = studio
    w, q = upload_window(root, env, ps)
    cm = w.open_channels()
    assert cm.winfo_exists() and w.open_channels() is cm  # 두 번 눌러도 창 하나
    cm.new_profile()
    cm.alias.set("🇫🇷 Chanson")
    cm.save_form()
    assert len(w.cb_profile.cget("values")) == 3
    w.destroy()
    assert not cm.winfo_exists()


# ---------------- 메인 3-in-1 ----------------

@pytest.fixture
def app(tmp_path, monkeypatch):
    import app.ui as ui
    monkeypatch.setattr(ui, "discover_ffmpeg", lambda root: None)
    try:
        a = ui.MainWindow(tmp_path)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no display: {e}")
    a.withdraw()
    yield a
    try:
        a._finish_close()
    except Exception:
        pass


def test_main_mode_cards_and_summary(app):
    assert set(app.mode_cards) == {"long", "live", "upload"}
    def labels(w):
        out = [w.cget("text")] if "text" in w.keys() else []
        for c in w.winfo_children():
            out += labels(c)
        return out
    texts = [labels(card) for card in app.mode_cards.values()]
    assert {"① 영상 늘리기", "SET 영상을 장시간 MP4로 제작", "● 현재 화면"} <= set(texts[0])
    assert {"② 실시간 스트리밍", "Cloud / 내 PC에서 Playlist LIVE", "예약 LIVE"} <= set(texts[1])
    assert {"③ 예약 업로드", "한국·일본 등 여러 채널에 자동 예약"} <= set(texts[2])
    assert str(app.mode_cards["long"].cget("highlightbackground")) != str(app.mode_cards["upload"].cget("highlightbackground"))
    app.update()
    assert pump(app, lambda: "영상 제작 대기 0" in app.mode_summary.get())
    s = app.mode_summary.get()
    assert "LIVE 창 닫힘" in s and "예약 업로드 대기 0" in s and "예약 완료 0" in s
    # 기존 ① 본문은 그대로
    assert app.start.cget("text") == "▶ 대기열 자동 시작" and app.qsummary.get().startswith("대기열 0/5")


def test_main_cards_open_windows(app):
    assert all(app.mode_cards[k].bind("<Button-1>") for k in ("live", "upload"))
    assert not app.mode_cards["long"].bind("<Button-1>")  # ①은 현재 화면
    w = app._open_upload()
    assert w is not None and w.winfo_exists() and app.upload_queue is w.q
    assert app._open_upload() is w  # 다시 눌러도 같은 창
    w.destroy()
    w2 = app._open_upload()
    assert w2 is not w and w2.q is app.upload_queue  # 창을 닫아도 대기열은 메인이 유지
    ls = app._open_live_schedule()
    assert ls.winfo_exists() and "연결되지 않았습니다" in ls.account.get()
    assert str(ls.btn_create.cget("state")) == "disabled"


def test_send_finished_video_to_upload(app, tmp_path, quiet):
    import app.ui as ui
    out = tmp_path / "LONG_OUTPUT" / "SET_10회차_FINAL.mp4"
    out.parent.mkdir()
    out.write_bytes(b"x" * 100)
    app.jobs = [ui.QueueJob([str(tmp_path / "a.mp4")], "rounds", 10, 10, 0, str(out), "대기"),
                ui.QueueJob([str(tmp_path / "a.mp4")], "rounds", 10, 10, 0, str(out), "완료")]
    app._refresh_q()
    items = app.qtree.get_children()
    app.qtree.selection_set(items[0])
    assert app._send_to_upload() is None and "완료된 영상만" in str(quiet[-1])
    app.qtree.selection_set(items[1])
    w = app._send_to_upload()
    assert w is not None and [i.video_path for i in w.items] == [str(out)]
    assert w.title_template.get() == "{filename}"  # 제목 기본값 = 파일 이름 (SET_10회차_FINAL)
    assert [j.status for j in app.jobs] == ["대기", "완료"]  # 제작 대기열은 그대로


# ---------------- 예약 LIVE ----------------

def test_live_schedule_window_creates_daily_reservations(root, fake, tmp_path, _isolated_settings):
    from app.youtube_config import save_youtube_settings
    from app.youtube_live_schedule_ui import LiveScheduleWindow, load_rules
    from youtube_fakes import FakeClock
    clock = FakeClock(datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc).timestamp())
    save_youtube_settings(client_file="x.json", channel_id="UCfake0001", channel_title="Old Pop Lounge")
    api = YouTubeApiClient(lambda force_refresh=False: FAKE_ACCESS, base_url=fake.api_base, sleep=lambda s: None)
    w = LiveScheduleWindow(root, api_factory=lambda: api, connected=lambda: True, clock=clock)
    assert "Old Pop Lounge" in w.account.get() and str(w.btn_topup.cget("state")) == "disabled"
    w.start_date.set("2026-10-06")
    w.start_time.set("07:00")
    w.tags.set("샹송, LIVE")
    w.thumbnail.set(str(jpeg(tmp_path / "t.jpg")))
    w.create()
    assert pump(root, lambda: "예약 7개" in w.message.get(), timeout=30), w.message.get()
    assert len(fake.broadcasts) == 7 and len(w.tree.get_children()) == 7
    first = w.tree.item(w.tree.get_children()[0], "values")
    assert first[0] == "2026-10-06 07:00" and first[1].startswith("2026.10.06 (화) 24H LIVE #01")
    assert len(load_rules()) == 1 and str(w.btn_topup.cget("state")) == "normal"
    w.top_up_saved()
    assert pump(root, lambda: "새로 만들 회차가 없습니다" in w.message.get(), timeout=20)
    assert len(fake.broadcasts) == 7  # 중복 예약 없음
    # 잘못된 입력은 API 호출 전에 차단
    w.start_time.set("25:99")
    w.create()
    assert len(fake.broadcasts) == 7
    w.remove_selected()  # 선택 없음 → 아무 일 없음
    w.tree.selection_set(w.tree.get_children()[0])
    w.remove_selected()
    assert len(w.tree.get_children()) == 6 and len(fake.broadcasts) == 7  # YouTube 예약은 지우지 않음
    w.destroy()
