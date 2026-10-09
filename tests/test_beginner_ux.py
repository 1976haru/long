"""Studio v1.3 초보자 UX: 첫 실행 Welcome, 처음 설정 Wizard, 초보자/고급 모드, 쉬운 오류, 도움말 센터, 오프라인 매뉴얼,
진단 정보(비밀값 제거), 설정 점검, 종료 안내, 작은 화면, 키보드. fake만 사용 — 실제 YouTube/OCI 접속 없음."""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import help_content as hc
from app.diagnostics import FAIL, OK, WARN, build_report, check_environment, check_settings
from app.settings import load_settings
from app.ui_text import (
    A_FFMPEG, A_PICK_FILE, A_RECONNECT, A_REPICK_TIME, A_RESELECT, A_RETRY_LATER, first_run_done, friendly_error,
    friendly_status, is_beginner, redact, set_beginner,
)
from app.youtube_accounts import ChannelProfile, ProfileStore, new_profile_id
from app.youtube_api import YouTubeApiError
from test_youtube_upload_queue import JP, KR, Env
from youtube_fakes import FakeYouTube

ROOT = Path(__file__).resolve().parent.parent


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


def texts(widget):
    out = [str(widget.cget("text"))] if "text" in widget.keys() else []
    for c in widget.winfo_children():
        out += texts(c)
    return out


def client_json(tmp_path):
    p = tmp_path / "client_secret_desktop.json"
    p.write_text(json.dumps({"installed": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "GOCSPX-fake",
                                           "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                                           "token_uri": "https://oauth2.googleapis.com/token"}}))
    return p


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


# ================= 첫 실행 / Wizard =================

def test_first_run_welcome_and_persistence(app, quiet):
    from app.help_ui import WelcomeDialog
    assert not first_run_done()
    app._maybe_welcome()
    w = app.welcome_win
    assert isinstance(w, WelcomeDialog)
    labels = texts(w)
    assert {"처음부터 설정하기", "5분 빠른 사용법", "나중에 하기", "처음 사용하시나요?"} <= set(labels)
    assert w.bind("<Return>") and w.bind("<Escape>")  # Enter = 처음부터 설정하기, Esc = 나중에 하기
    w._choose(None)  # [나중에 하기]
    assert first_run_done() and load_settings()["first_run_completed"] is True
    app.welcome_win = None
    app._maybe_welcome()
    assert app.welcome_win is None  # 다음부터 자동 표시 안 함
    assert "? 처음 사용 가이드" in texts(app) and app._open_welcome().winfo_exists()  # 언제든 다시
    w2 = app.welcome_win
    w2._choose(lambda: app._open_help("quick"))  # [5분 빠른 사용법]
    assert app.help_win is not None and app.help_win.current == "quick"


def test_setup_wizard_four_steps_kr_preset_and_connect(root, tmp_path, quiet):
    from app.help_ui import SetupWizard
    ps = ProfileStore()
    connected = []

    def fake_connect(profiles, p, path, open_browser=None):
        connected.append((p.alias, path))
        p.client_file, p.channel_id, p.channel_title = path, KR["id"], KR["title"]
        return profiles.save(p)
    opened = []
    w = SetupWizard(root, profiles=ps, connect=fake_connect, ffmpeg_finder=lambda: ("ffmpeg", "ffprobe"),
                    internet=lambda: True, pick_file=lambda **kw: str(client_json(tmp_path)), guide=lambda parent: True,
                    on_open_upload=lambda: opened.append(1))
    assert w.step_title.get() == "STEP 1 / 4 · 기본 프로그램 점검"
    lines = texts(w.body)
    assert "✓ FFmpeg 준비됨" in lines and "✓ 프로그램 설정 폴더 정상" in lines and any("인터넷 연결됨" in x for x in lines)
    assert w.bind("<Return>") and w.bind("<Escape>")
    w.next()
    assert "STEP 2 / 4" in w.step_title.get() and "어떤 YouTube 채널을 연결할까요?" in texts(w.body)
    p = w.choose_preset("kr")
    assert (p.alias, p.language, p.timezone) == ("내 한국 채널", "ko", "Asia/Seoul")
    body = texts(w.body)
    assert "Google 계정 처음 연결하기" in body and "Google 연결 파일 선택" not in body  # 초보자: 파일부터 요구하지 않음
    assert w.btn_connect is None
    assert not any("OAuth" in x or "client_secret" in x or "scope" in x for x in body)  # 첫 화면에 기술 용어 없음
    w.pick_client_file()
    assert w.btn_connect is not None and str(w.btn_connect.cget("text")) == "Google 계정 연결"
    w.start_connect()
    assert pump(root, lambda: w.connect_msg.get() == "✓ 연결 완료")
    assert "실제 YouTube 채널: 한국 시니어" in w.result_text.get() and "시간대: Asia/Seoul" in w.result_text.get()
    assert "별칭: 내 한국 채널" in w.result_text.get()
    assert KR["id"] not in " ".join(texts(w.body))  # channel ID는 숨김
    w.toggle_advanced()
    assert any(KR["id"] in x for x in texts(w.body))  # [고급 정보 보기]
    jp = w.choose_preset("jp")
    assert (jp.alias, jp.language, jp.timezone) == ("내 일본 채널", "ja", "Asia/Tokyo")
    assert w.choose_preset("kr").alias == "내 한국 채널 2"
    w.next()
    assert "STEP 3 / 4" in w.step_title.get()
    w.default_time.set("21:00")
    w.next()
    assert load_settings()["upload_defaults"] == {"time": "21:00"} and "STEP 4 / 4" in w.step_title.get()
    w.finish(open_upload=True)
    assert first_run_done() and opened == [1] and connected == [("내 한국 채널", str(tmp_path / "client_secret_desktop.json"))]


@pytest.mark.parametrize("preset,default,custom,lang,tz", [
    ("kr", "내 한국 채널", "한국 시니어", "ko", "Asia/Seoul"),
    ("jp", "내 일본 채널", "CHILI LAB", "ja", "Asia/Tokyo"),
])
def test_wizard_preset_alias_editable_and_persists(root, tmp_path, preset, default, custom, lang, tz):
    from app.help_ui import SetupWizard
    ps = ProfileStore()
    got = []

    def fake_connect(profiles, p, path, open_browser=None):
        got.append(p.alias)
        p.client_file, p.channel_id, p.channel_title = path, KR["id"], "실제 채널 이름"
        return profiles.save(p)
    w = SetupWizard(root, profiles=ps, connect=fake_connect, ffmpeg_finder=lambda: ("f", "p"), internet=lambda: True,
                    pick_file=lambda **kw: str(client_json(tmp_path)), guide=lambda parent: True)
    w.next()
    p = w.choose_preset(preset)
    assert w.alias_var.get() == default and w.ent_alias.winfo_exists()
    body = texts(w.body)
    assert "1. YouTube 채널 이름(별칭)" in body
    assert any("실제 YouTube 채널 이름과 달라도 됩니다" in x for x in body)
    w.alias_var.set(f"  {custom}  ")
    assert w.apply_alias()
    saved = ps.get(p.profile_id)
    assert (saved.alias, saved.language, saved.timezone) == (custom, lang, tz)  # trim, 언어·시간대 유지
    assert w.alias_var.get() == custom
    w.pick_client_file()
    w.start_connect()
    assert pump(root, lambda: w.connect_msg.get() == "✓ 연결 완료")
    assert got == [custom]  # 연결에 쓴 이름 = 사용자가 정한 별칭
    assert f"별칭: {custom}" in w.result_text.get() and "실제 YouTube 채널: 실제 채널 이름" in w.result_text.get()
    assert ProfileStore().get(p.profile_id).alias == custom  # 저장 유지 (다시 읽어도)
    w.destroy()


def test_wizard_alias_blank_and_duplicate_blocked(root):
    from app.help_ui import SetupWizard
    ps = ProfileStore()
    ps.add(ChannelProfile(new_profile_id(), "한국 시니어", language="ko", timezone="Asia/Seoul"))
    w = SetupWizard(root, profiles=ps, ffmpeg_finder=lambda: ("f", "p"), internet=lambda: True)
    w.next()
    p = w.choose_preset("kr")
    w.alias_var.set("   ")
    assert not w.apply_alias() and "이름을 입력하세요" in w.alias_msg.get()
    w.next()
    assert "STEP 2" in w.step_title.get()  # 빈 이름이면 다음으로 가지 않음
    w.alias_var.set("한국 시니어")
    assert not w.apply_alias() and "같은 별칭" in w.alias_msg.get()
    assert ps.get(p.profile_id).alias == "내 한국 채널"  # 기존 값 유지
    w.start_connect()
    assert not w.busy  # 연결도 시작하지 않음
    w.alias_var.set("한국 시니어 2")
    w.next()
    assert "STEP 3" in w.step_title.get() and ps.get(p.profile_id).alias == "한국 시니어 2"
    w.destroy()


def test_wizard_ffmpeg_missing_shows_fix_buttons(root, quiet):
    from app.help_ui import SetupWizard
    picked = []
    w = SetupWizard(root, profiles=ProfileStore(), ffmpeg_finder=lambda: None, internet=lambda: False,
                    pick_ffmpeg=lambda: picked.append(1) or False)
    body = texts(w.body)
    assert "✗ FFmpeg를 찾지 못했습니다. — 영상 늘리기와 LIVE에 필요합니다." in body
    assert {"자동으로 다시 찾기", "직접 선택", "도움말"} <= set(body)
    assert any("인터넷 연결을 확인하지 못했습니다" in x for x in body)
    assert not any("Traceback" in x for x in body)
    w._pick_ffmpeg_now()
    assert picked == [1]
    w.destroy()


def test_check_environment_unit():
    items = check_environment(ffmpeg_finder=lambda: (_ for _ in ()).throw(RuntimeError("boom")), internet=lambda: True)
    assert items[0].status == FAIL and items[0].fix == "ffmpeg"  # 예외도 traceback 없이 ⚠
    assert any(i.label.startswith("시간대 정보 정상") for i in items)


# ================= 초보자 / 고급 =================

def test_beginner_mode_default_and_advanced(root, fake, tmp_path):
    from app.youtube_channels_ui import ChannelManagerWindow
    from app.youtube_upload_ui import MultiChannelUploadWindow
    assert is_beginner()
    ps = ProfileStore()
    kr = ps.add(ChannelProfile(new_profile_id(), "🇰🇷 한국 시니어", channel_id=KR["id"], channel_title=KR["title"]))
    cm = ChannelManagerWindow(root, profiles=ps)
    cm.tree.selection_set(kr.profile_id)
    cm._on_select()
    assert "채널 이름: 한국 시니어" in cm.channel_text.get() and KR["id"] not in cm.channel_text.get()
    labels = texts(cm)
    assert "Google 연결 파일" in labels and "OAuth JSON" not in labels
    cm.toggle_advanced_info()
    assert KR["id"] in cm.channel_text.get()
    cm.destroy()
    env = Env(fake, ps, kr, kr)
    w = MultiChannelUploadWindow(root, upload_queue=env.queue(ps), clock=env.clock)
    w.toggle_detail()
    assert not any(x.winfo_manager() for x in w.adv_row) and not w.lbl_usage.winfo_manager()
    w.toggle_advanced()
    assert all(x.winfo_manager() == "grid" for x in w.adv_row)
    set_beginner(False)
    w.apply_mode()
    assert w.lbl_usage.winfo_manager() == "pack" and not w.btn_adv.winfo_manager()
    assert not is_beginner()
    w.destroy()


def test_main_beginner_toggle_card_help_and_buttons(app, quiet):
    labels = texts(app)
    for name in ("? 처음 사용 가이드", "? 도움말", "⚙ 설정 점검", "초보자 모드", "댓글 관리"):
        assert name in labels
    assert not any("💬" in x for x in labels)  # Windows Tk에서 네모로 보이는 이모지 쓰지 않음
    assert all(k in app.card_help_links for k in ("long", "live", "upload"))
    app._card_help("upload", "③ 예약 업로드")
    assert hc.CARD_HELP["upload"] in str(quiet[-1])
    app.beginner.set(False)
    app._toggle_beginner()
    assert not is_beginner()


# ================= 쉬운 오류 =================

@pytest.mark.parametrize("err,problem,action", [
    (YouTubeApiError("x", kind="config", reason="insufficientPermissions", status=403), "Google 연결 권한이 부족합니다.", A_RECONNECT),
    (YouTubeApiError("채널 불일치로 업로드를 차단했습니다.", kind="config", reason="channelMismatch"), "YouTube 채널이 다릅니다.", A_RESELECT),
    (YouTubeApiError("x", kind="config", reason="quotaExceeded", status=403), "오늘 YouTube 사용량 한도에 도달했습니다.", A_RETRY_LATER),
    (YouTubeApiError("x", kind="transient", reason="network"), "인터넷 연결이 끊겼습니다.", A_RETRY_LATER),
    (YouTubeApiError("x", kind="config", reason="publishAtPast"), "예약 공개 시간이 이미 지났거나 너무 가깝습니다.", A_REPICK_TIME),
    (YouTubeApiError("x", kind="config", reason="fileChanged"), "예약 등록 후 영상 파일이 바뀌었습니다.", A_PICK_FILE),
])
def test_friendly_error_mapping(err, problem, action):
    fe = friendly_error(err)
    assert fe.problem == problem and fe.action_key == action
    assert "HTTP" not in fe.text() and "insufficientPermissions" not in fe.text()  # 초보자 화면에는 기술 내용 없음
    if err.status:
        assert f"HTTP {err.status}" in fe.detail  # [자세히 보기]에만
    assert friendly_error(message="ffmpeg.exe not found").action_key == A_FFMPEG
    assert "도움" in friendly_error(message="???").action


def test_friendly_status_sentences():
    assert friendly_status("VERIFYING_CHANNEL") == "업로드할 YouTube 채널을 확인하고 있습니다."
    assert friendly_status("CREATING_SESSION") == "업로드를 준비하고 있습니다."
    assert friendly_status("UPLOADING") == "영상을 YouTube에 올리고 있습니다."
    assert friendly_status("PROCESSING") == "YouTube가 영상을 처리하고 있습니다."
    assert friendly_status("VERIFYING_SCHEDULE") == "예약 시간이 제대로 등록됐는지 확인하고 있습니다."
    from app.youtube_upload_queue import STATE_LABELS
    assert STATE_LABELS["API_REVIEW_REQUIRED"] == "Google 설정 확인 필요"


def test_friendly_error_dialog_action_and_detail(root):
    from app.help_ui import show_friendly_error
    called = []
    fe = friendly_error(YouTubeApiError("x", kind="config", reason="insufficientPermissions", status=403))
    d = show_friendly_error(root, fe, actions={A_RECONNECT: lambda: called.append(1)})
    labels = texts(d)
    assert "무슨 문제가 생겼나요?" in labels and "무엇을 하면 되나요?" in labels
    assert str(d.action_button.cget("text")) == "채널 다시 연결" and not d.detail_label.winfo_manager()
    d.detail_button.invoke()
    assert d.detail_label.winfo_manager() == "pack" and "HTTP 403" in str(d.detail_label.cget("text"))
    d.action_button.invoke()
    assert called == [1]


# ================= 예약 업로드 흐름 =================

@pytest.fixture
def studio(fake, tmp_path):
    ps = ProfileStore()
    kr = ps.add(ChannelProfile(new_profile_id(), "🇰🇷 한국 시니어", channel_id=KR["id"], channel_title=KR["title"],
                               language="ko", timezone="Asia/Seoul"))
    jp = ps.add(ChannelProfile(new_profile_id(), "🇯🇵 CHILI LAB", channel_id=JP["id"], channel_title=JP["title"],
                               language="ja", timezone="Asia/Tokyo"))
    return Env(fake, ps, kr, jp), ps, kr, jp


def upload_window(root, env, ps, **kw):
    from app.youtube_upload_ui import MultiChannelUploadWindow
    q = env.queue(ps)
    return MultiChannelUploadWindow(root, upload_queue=q, clock=env.clock, **kw), q


def five_videos(tmp_path):
    d = tmp_path / "five"
    d.mkdir()
    for i in range(1, 6):
        (d / f"{i:03d}.mp4").write_bytes(bytes(range(256)) * 20)
    from test_youtube_upload_queue import jpeg
    jpeg(d / "001.jpg")
    jpeg(d / "003.jpg")
    return d


def test_scenario_b_folder_daily_preview_start_and_done(root, studio, fake, tmp_path, monkeypatch):
    import app.help_ui as help_ui
    env, ps, kr, jp = studio
    done = []
    monkeypatch.setattr(help_ui, "show_done", lambda master, **kw: done.append(kw) or SimpleNamespace(head="✓"))
    w, q = upload_window(root, env, ps)
    assert w.current_step() == 2  # 채널은 기본 선택됨 → 영상 선택
    assert w.pub_time.get() == "19:00" and w.pub_date.get()  # 기본: 내일 19:00
    w.select_profile(jp.profile_id)
    w.add_folder(str(five_videos(tmp_path)))
    assert w.current_step() == 4 and "▶ STEP 4" in str(w.step_labels[3].cget("text"))
    assert str(w.step_labels[0].cget("text")).startswith("✓")  # 지난 단계 표시 (색 + 글자)
    assert [w.items_tree.set(i, "thumb") for i in w.items_tree.get_children()][:3] == ["001.jpg ✓", "없음 ⚠", "003.jpg ✓"]
    dlg = w.preview()
    assert pump(root, lambda: dlg.verify_state == "ok")
    labels = texts(dlg)
    assert "이 채널에 업로드합니다" in labels and "🇯🇵 CHILI LAB" in labels and "일본 YouTube 채널" in labels
    assert any(re.match(r"\d+월 \d+일 오후 7:00", x) for x in labels) and "5개" in labels
    assert JP["id"] not in " ".join(labels)  # 초보자: channel ID 숨김
    assert str(dlg.btn_start.cget("text")) == "맞습니다. 예약 업로드 시작"
    jobs = dlg.confirm_and_start()
    assert len(jobs) == 5 and q.running or all(j.status != "PENDING" for j in q.snapshot())
    assert pump(root, lambda: not q.running and all(j.status == "COMPLETE" for j in q.snapshot()), timeout=40)
    assert pump(root, lambda: bool(done))
    assert done[0]["count"] == 5 and done[0]["failed"] == 0 and done[0]["alias"] == "🇯🇵 CHILI LAB"
    assert load_settings()["upload_last_profile"] == jp.profile_id and load_settings()["upload_last_time"] == "19:00"
    w.destroy()
    w2, _ = upload_window(root, env, ps)
    assert w2.selected_profile().profile_id == jp.profile_id  # 마지막 사용 채널
    w2.destroy()


def test_progress_panel_text(root, studio, tmp_path):
    env, ps, kr, jp = studio
    w, q = upload_window(root, env, ps)
    jobs = [q.add(q.make_job(profile_id=kr.profile_id, video_path=str(p), title=p.stem))
            for p in sorted(five_videos(tmp_path).glob("*.mp4"))]
    w.run_ids = [j.job_id for j in jobs]
    jobs[0].status, jobs[1].status, jobs[2].status, jobs[2].progress = "COMPLETE", "COMPLETE", "UPLOADING", 0.68
    q._thread = SimpleNamespace(is_alive=lambda: True)  # 업로드 중인 것처럼
    w._refresh_progress(jobs)
    assert w.progress_frame.winfo_manager() == "pack"
    assert w.progress_head.get() == "영상 3 / 5"
    assert "현재: 003.mp4   68%" in w.progress_text.get() and "영상을 YouTube에 올리고 있습니다." in w.progress_text.get()
    assert "남은 영상: 2개" in w.progress_text.get()
    q._thread = None
    w._refresh_progress(jobs)
    assert not w.progress_frame.winfo_manager()
    w.destroy()


def test_scenario_c_wrong_channel_preview_blocked_with_action(root, studio, tmp_path):
    env, ps, kr, jp = studio
    env.accounts[jp.profile_id] = (KR, "tok-kr-as-jp")
    w, q = upload_window(root, env, ps)
    w.select_profile(jp.profile_id)
    w.add_folder(str(five_videos(tmp_path)))
    dlg = w.preview()
    assert pump(root, lambda: dlg.verify_state == "mismatch")
    assert str(dlg.btn_start.cget("state")) == "disabled" and str(dlg.btn_add.cget("state")) == "disabled"
    assert any("YouTube 채널이 다릅니다." in e for e in dlg.errors)
    assert str(dlg.btn_reselect.cget("text")) == "▶ 채널 다시 선택"
    dlg.reselect()
    assert not dlg.winfo_exists() and q.snapshot() == []
    w.destroy()


def test_scenario_d_comment_permission_friendly_reconnect(root, studio):
    from app.youtube_comments import CommentService
    from app.youtube_comments_ui import CommentManagerWindow
    env, ps, kr, jp = studio
    svc = CommentService(ps, api_factory=env.api_factory, clock=env.clock, connected=lambda p: True)
    cs = svc.store.settings_for(jp)
    cs.needs_reauth = True
    svc.store.save_settings(cs)
    w = CommentManagerWindow(root, service=svc, profile_id=jp.profile_id)
    assert w.reauth_frame.winfo_manager() == "pack"
    assert "Google 연결 권한이 부족합니다." in w.reauth_text.get() and "채널 다시 연결" in texts(w.reauth_frame)
    assert "insufficientPermissions" not in w.reauth_text.get()
    cm = w.reconnect()
    assert cm.selected_id == jp.profile_id and cm.comment_scope.get()  # 그 채널만, 댓글 권한 함께 요청
    w.destroy()


def test_usage_buttons_and_tooltips(root, studio):
    from app.help_ui import InfoTip, show_usage
    env, ps, kr, jp = studio
    w, q = upload_window(root, env, ps)
    assert "? 사용법" in texts(w)
    d = show_usage(w, "upload")
    assert d.usage_title == "예약 업로드 사용법"
    body = " ".join(texts(d))
    assert body.count("\n") <= 5 and "5. " in body and "6. " not in body  # 5단계 이하
    d.close()
    tips = [x for x in _walk(w) if isinstance(x, InfoTip)]
    assert len(tips) >= 3
    t = tips[0].show()
    assert t.winfo_exists()
    tips[0].hide()
    w.destroy()


def _walk(w):
    yield w
    for c in w.winfo_children():
        yield from _walk(c)


# ================= 도움말 / 매뉴얼 / 진단 =================

def test_help_center_navigation_search_and_version(root):
    from app.help_ui import HelpWindow
    w = HelpWindow(root, topic="quick", diagnostics=lambda: "diag ya29.SECRETTOKEN")
    assert [t.key for t in w.topics] == [t.key for t in hc.TOPICS] and len(w.topics) == 10
    assert {"google_file", "playlists"} <= {t.key for t in w.topics}
    assert "5분 만에 예약 업로드하기" in w.text.get("1.0", "end")
    assert any("Manual version v1.3" in x for x in texts(w))
    for q, key in (("썸네일", "upload"), ("FFmpeg", "trouble"), ("댓글", "comments"), ("채널 연결", "channels")):
        w.query.set(q)
        w.refresh_list()
        assert key in [t.key for t in w.topics], q
    w.query.set("없는검색어123")
    w.refresh_list()
    assert "찾는 내용이 없습니다" in w.text.get("1.0", "end")
    w.query.set("")
    w.refresh_list(select="trouble")
    assert w.btn_diag.winfo_manager() == "pack"
    assert w.copy_diagnostics() == "diag ya29.SECRETTOKEN"  # 주입한 함수 그대로 (실제 함수는 redact 적용)
    w.show("upload")
    assert not w.btn_diag.winfo_manager()
    assert w.bind("<Escape>")
    w.destroy()


def test_manual_files_exist_offline_and_in_sync():
    from app.manual_html import FILES
    for name, fn in FILES.items():
        p = ROOT / "docs" / name
        assert p.is_file(), name
        text = p.read_text(encoding="utf-8")
        assert text == fn(), f"{name}: python -m app.manual_html 로 다시 만드세요"
        assert not re.search(r"<(script|link|img|iframe)[^>]+(src|href)=[\"']?https?:", text, re.I)  # 외부 CDN/글꼴 없음
        assert "https://" not in text and "http://" not in text
        assert "Manual version v1.3" in text
    manual = (ROOT / "docs" / "사용자_매뉴얼.html").read_text(encoding="utf-8")
    for heading in ("1. 프로그램 소개", "2. 처음 실행", "3. YouTube 채널 연결", "4. 영상 늘리기", "5. 예약 업로드",
                    "6. 여러 영상 한꺼번에 예약", "7. 썸네일 자동 연결", "8. LIVE 방송", "9. 예약 LIVE",
                    "10. 첫 댓글 자동등록", "11. 댓글 자동답글", "12. 안전하게 종료", "13. 오류 해결", "14. FAQ"):
        assert heading in manual


def test_manual_button_names_match_ui():
    src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "app").glob("*.py") if p.name != "help_content.py")
    missing = [v for v in hc.BUTTONS.values() if v not in src]
    assert missing == []  # 매뉴얼의 버튼 이름 = 실제 화면 문구


def test_diagnostic_report_redacts_secrets(studio):
    from app.settings import update_settings
    env, ps, kr, jp = studio
    secrets = ["ya29.a0AfH6SMC-real-looking-access", "1//0gLongRefreshTokenValue123456", "GOCSPX-AbCdEf123456",
               "abcd-efgh-ijkl-mnop-qrst"]
    update_settings(upload_queue=[{"job_id": "j", "status": "FAILED", "error":
                                   f"https://x/upload/youtube/v3/videos?upload_id=SESSIONSECRET {secrets[0]} {secrets[1]}"}],
                    comment_tasks=[{"task_id": "t", "error_kind": "INSUFFICIENT_PERMISSION"}])
    report = build_report(profiles=ps, ffmpeg_pair=("C:/ffmpeg/ffmpeg.exe", "C:/ffmpeg/ffprobe.exe"),
                          version_of=lambda p: f"ffmpeg version 7.0 {secrets[2]}")
    for s in secrets[:3] + ["SESSIONSECRET"]:
        assert s not in report
    assert "🇰🇷 한국 시니어" in report and "🇯🇵 CHILI LAB" in report and "Manual v1.3" in report
    assert "INSUFFICIENT_PERMISSION" in report and "FFmpeg: C:/ffmpeg/ffmpeg.exe" in report
    assert KR["id"] not in report  # 채널 ID도 넣지 않음
    sample = (f"access_token={secrets[0]} refresh_token: {secrets[1]} client_secret \"{secrets[2]}\" "
              f"rtmp://a.rtmp.youtube.com/live2/{secrets[3]} stream_key={secrets[3]}")
    red = redact(sample)
    assert all(s not in red for s in secrets) and red.count("[숨김]") >= 5


def test_settings_checker_and_window(root, studio, fake):
    from app.help_ui import SettingsCheckWindow
    from app.youtube_comments import CommentStore
    env, ps, kr, jp = studio
    store = CommentStore()
    cs = store.settings_for(jp)
    cs.needs_reauth = True
    store.save_settings(cs)
    ps.is_connected = lambda p: p.profile_id == kr.profile_id
    items = check_settings(profiles=ps, ffmpeg_ok=False, comment_store=store)
    lines = [i.line for i in items]
    assert lines[0].startswith("✗ FFmpeg")
    assert any(x.startswith("✓ 🇰🇷 한국 시니어 연결") for x in lines)
    assert any(x.startswith("⚠ 🇯🇵 CHILI LAB 연결 안 됨") for x in lines)
    assert any("🇯🇵 CHILI LAB 댓글 권한 미설정" in x for x in lines)
    assert any(x.startswith("✓ 예약 업로드 준비") for x in lines)
    fixed = []
    w = SettingsCheckWindow(root, profiles=ps, ffmpeg_ok=lambda: False, comment_store=store,
                            fixes={"ffmpeg": lambda: fixed.append("ffmpeg"), "reconnect": lambda: fixed.append("re"),
                                   "channels": lambda: fixed.append("ch")})
    buttons = [b for b in _walk(w) if str(b.winfo_class()) == "TButton" and b.cget("text") == "수정"]
    assert len(buttons) == 3
    buttons[0].invoke()
    assert fixed == ["ffmpeg"]


# ================= 종료 안내 / 작은 화면 / LIVE =================

def test_exit_warning_when_upload_or_comments_running(app, monkeypatch):
    import app.ui as ui
    asked = []
    monkeypatch.setattr(ui, "ask_exit", lambda master, lines: asked.append(lines) or False)
    snap = [SimpleNamespace(status="COMPLETE"), SimpleNamespace(status="UPLOADING")] + [SimpleNamespace(status="PENDING")] * 8
    app.upload_queue = SimpleNamespace(running=True, snapshot=lambda: snap, stop=lambda timeout=0: None)
    destroyed = []
    orig = app.destroy
    app.destroy = lambda: (destroyed.append(1), orig())
    app._close_after_live()
    assert asked and asked[0][0] == "영상 업로드: 진행 중 (1 / 10)" and "댓글 자동 확인: OFF" in asked[0] and "LIVE: OFF" in asked[0]
    assert not destroyed  # [취소]
    monkeypatch.setattr(ui, "ask_exit", lambda master, lines: True)
    app._close_after_live()
    assert destroyed  # [종료]


def test_no_exit_warning_when_idle(app, monkeypatch):
    import app.ui as ui
    monkeypatch.setattr(ui, "ask_exit", lambda master, lines: pytest.fail("idle에서는 묻지 않음"))
    assert app._running_work_lines() == []


@pytest.mark.parametrize("which", ["upload", "comments", "channels", "help"])
def test_small_screen_800px_reaches_key_buttons(root, studio, which):
    from app.help_ui import HelpWindow
    from app.youtube_channels_ui import ChannelManagerWindow
    from app.youtube_comments import CommentService
    from app.youtube_comments_ui import CommentManagerWindow
    env, ps, kr, jp = studio
    if which == "upload":
        w, _ = upload_window(root, env, ps)
        target = w.btn_start
    elif which == "comments":
        w = CommentManagerWindow(root, service=CommentService(ps, api_factory=env.api_factory), profile_id=kr.profile_id)
        target = w.task_tree
    elif which == "channels":
        w = ChannelManagerWindow(root, profiles=ps)
        target = w.btn_connect
    else:
        w = HelpWindow(root)
        target = w.listbox
    w.geometry("1000x800+0+0")
    w.deiconify()
    w.update()
    if hasattr(w, "scroll"):
        w.scroll.canvas.yview_moveto(1.0)
        w.update()
        cv = w.scroll.canvas
        top, bottom = cv.winfo_rooty(), cv.winfo_rooty() + cv.winfo_height()
    else:
        top, bottom = w.winfo_rooty(), w.winfo_rooty() + w.winfo_height()
    assert top <= target.winfo_rooty() and target.winfo_rooty() + target.winfo_height() <= bottom + 1, which
    w.destroy()


def test_live_beginner_preset(root, quiet):
    from app.cloud_client import CloudLiveController
    from app.live_secrets import SessionStreamKeyStore
    from app.live_session import SESSION_ARCHIVE_SAFE
    from app.live_ui import SEND_AUTO, LiveWindow
    w = LiveWindow(root, tools=lambda: (None, None), key_store=SessionStreamKeyStore(),
                   cloud=CloudLiveController(lambda: None, poll_seconds=3600))
    assert "초보자 추천 설정" in texts(w) and "? 사용법" in texts(w)
    w.session_mode.set("continuous")
    w.confirm_stop.set(False)
    assert w.apply_beginner_preset()
    assert (w.send_mode.get(), w.session_mode.get(), w.confirm_stop.get()) == (SEND_AUTO, SESSION_ARCHIVE_SAFE, True)
    assert "적용했습니다" in str(quiet[-1])
    d = w._show_usage()
    assert d.usage_title == "실시간 LIVE 사용법"
    d.close()
    w.destroy()


def test_exe_self_test_sequence_from_source(tmp_path, monkeypatch):
    """--self-test: Welcome → Wizard → 도움말 → 매뉴얼 → 예약 업로드 → 채널 연결 창이 열리고 결과 JSON (네트워크 없음)."""
    import app.ui as ui
    from app import self_test
    monkeypatch.setattr(ui, "discover_ffmpeg", lambda root: None)
    try:
        a = ui.MainWindow(tmp_path)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no display: {e}")
    out = tmp_path / "self_test.json"
    self_test.run(a, str(out), step_ms=100)
    end = time.monotonic() + 30
    while not out.exists() and time.monotonic() < end:
        try:
            a.update()
        except Exception:
            break
        time.sleep(0.02)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["all_ok"], data
    assert {"welcome", "setup_wizard", "help", "manual", "diagnostics", "upload_window", "channel_manager",
            "comment_manager", "live_window", "live_schedule", "japanese_assistant"} <= set(data)


def test_terminology_unified_in_ui_sources():
    """화면 문구에서 '채널 프로필'·'OAuth JSON' 같은 표현을 쓰지 않는다 (docstring/주석 제외)."""
    import ast
    bad = []
    for f in (ROOT / "app").glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
                and n.body and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                for word in ("채널 프로필", "OAuth JSON", "OAuth Client JSON"):
                    if word in node.value and f.name not in ("ui_text.py",):
                        bad.append(f"{f.name}: {node.value[:60]}")
    assert bad == []
