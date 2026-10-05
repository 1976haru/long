"""Studio v1.1 UX: 작은 화면 스크롤, 채널별 템플릿, 폴더 일괄 추가, 썸네일 매칭, 일괄 예약, 미리보기 안전장치,
Local LIVE 대역폭 확인, Retry-After, 파일 fingerprint, 최근 폴더, 채널 복제, DnD 없음 fallback.
fake 서버/주입 함수만 사용 — 실제 YouTube/OCI 접속 없음."""
import os
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.youtube_accounts import ChannelProfile, ProfileStore, new_profile_id
from app.youtube_api import YouTubeApiClient
from app.youtube_batch import (
    DAILY, EVERY_2_DAYS, EVERY_N_DAYS, WEEKDAYS, WEEKLY, BatchItem, ThumbMatch, UploadTemplateStore, build_plan,
    clone_profile, episode_from_filename, estimate_seconds, last_folder, match_all, match_thumbnail, remember_folder,
    scan_folder, schedule_times,
)
from app.youtube_metadata import THUMB_FIXED, THUMB_FOLDER, MetadataTemplate, render_template
from app.youtube_upload import ResumableUploader, file_signature, http_transport, parse_retry_after, signature_matches
from app.youtube_upload_queue import COMPLETE, FAILED, PENDING
from app.youtube_usage import today_usage
from test_youtube_upload_queue import JP, KR, Env, jpeg, png
from youtube_fakes import FAKE_ACCESS, FakeYouTube

KB256 = 256 * 1024


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


def mp4(path, n=1):
    path.write_bytes(bytes(range(256)) * (1024 * n + 3))
    return path


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "CapCut_Export"
    d.mkdir()
    for name in ("002.mp4", "001.mp4", "010.mp4", "003.mp4"):
        mp4(d / name)
    jpeg(d / "001.jpg")
    png(d / "002.png")
    jpeg(d / "010_thumbnail.jpg")
    (d / "004.part.mp4").write_bytes(b"x")  # 제작 중 임시 파일
    (d / "notes.txt").write_text("x")
    sub = d / "sub"
    sub.mkdir()
    mp4(sub / "005.mp4")
    return d


@pytest.fixture
def studio(fake, tmp_path):
    ps = ProfileStore()
    kr = ps.add(ChannelProfile(new_profile_id(), "🇰🇷 한국 시니어", channel_id=KR["id"], channel_title=KR["title"],
                               language="ko", timezone="Asia/Seoul"))
    jp = ps.add(ChannelProfile(new_profile_id(), "🇯🇵 CHILI LAB", channel_id=JP["id"], channel_title=JP["title"],
                               language="ja", timezone="Asia/Tokyo", category_id="24"))
    return Env(fake, ps, kr, jp), ps, kr, jp


def window(root, env, ps, **kw):
    from app.youtube_upload_ui import MultiChannelUploadWindow
    q = env.queue(ps)
    return MultiChannelUploadWindow(root, upload_queue=q, clock=env.clock, **kw), q


def env_tomorrow(env, tz="Asia/Seoul"):
    from zoneinfo import ZoneInfo
    return (datetime.fromtimestamp(env.clock(), timezone.utc).astimezone(ZoneInfo(tz)).date() + timedelta(days=1))


# ================= 폴더 / 썸네일 (순수 로직) =================

def test_scan_folder_natural_order_no_subfolders_and_files_untouched(folder):
    before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in folder.iterdir() if p.is_file()}
    assert [p.name for p in scan_folder(folder)] == ["001.mp4", "002.mp4", "003.mp4", "010.mp4"]
    assert [p.name for p in scan_folder(folder, recursive=True)][-1] == "005.mp4"
    after = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in folder.iterdir() if p.is_file()}
    assert before == after  # 이동/삭제/이름 변경 없음
    with pytest.raises(ValueError):
        scan_folder(folder / "missing")


def test_thumbnail_exact_suffix_missing(folder):
    vids = scan_folder(folder)
    labels = [m.label for m in match_all(vids)]
    assert labels == ["001.jpg ✓", "002.png ✓", "없음 ⚠", "010_thumbnail.jpg ✓"]
    assert match_thumbnail(folder / "001.mp4").status == "exact"
    assert match_thumbnail(folder / "010.mp4").status == "suffix"


def test_thumbnail_ambiguous_is_not_auto_selected(folder):
    png(folder / "001.png")  # 001.jpg + 001.png
    m = match_thumbnail(folder / "001.mp4")
    assert m.status == "ambiguous" and m.path == "" and len(m.candidates) == 2
    assert m.label == "썸네일 후보 2개 - 하나를 선택하세요"
    assert m.candidates[0].endswith("001.jpg")  # 표시 순서: jpg → jpeg → png


def test_thumbnail_template_fallback_fixed_and_folder(folder, tmp_path):
    vids = scan_folder(folder)
    fixed = jpeg(tmp_path / "default.jpg")
    t = MetadataTemplate("t", "{filename}", thumbnail_mode=THUMB_FIXED, thumbnail_paths=[str(fixed)])
    assert match_all(vids, t)[2].path == str(fixed) and match_all(vids, t)[2].status == "template"
    tf = tmp_path / "thumbs"
    tf.mkdir()
    for n in ("a.jpg", "b.jpg", "c.jpg", "d.jpg"):
        jpeg(tf / n)
    t2 = MetadataTemplate("t", "{filename}", thumbnail_mode=THUMB_FOLDER, thumbnail_folder=str(tf))
    got = match_all(vids, t2)
    assert got[0].status == "exact" and got[2].path.endswith("c.jpg")  # 같은 이름이 우선, 없으면 순서대로 (3번째)


def test_episode_and_template_variables():
    assert episode_from_filename("여_003.mp4") == "3" and episode_from_filename("cafe.mp4") == ""
    from zoneinfo import ZoneInfo
    t = datetime(2026, 10, 6, 19, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    out = render_template("秋の夜に聴きたいChill Rap｜{date} {yyyy}-{mm}-{dd} ({weekday}) {series} EP {episode} #{n} {filename}",
                          local_start=t, session=2, n=2, series="Tokyo night", episode="3", filename="여_003",
                          language="ja")
    assert out == "秋の夜に聴きたいChill Rap｜2026.10.06 2026-10-06 (火) Tokyo night EP 3 #2 여_003"
    # 기존 LIVE 템플릿 변수/요일(한국어)은 그대로
    assert render_template("{date} ({weekday}) #{session}", local_start=t, session=3) == "2026.10.06 (화) #03"


# ================= 일괄 예약 =================

@pytest.mark.parametrize("interval,days,expect", [
    (DAILY, 1, ["2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12"]),
    (WEEKDAYS, 1, ["2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14"]),  # 금 → 월
    (EVERY_2_DAYS, 1, ["2026-10-09", "2026-10-11", "2026-10-13", "2026-10-15"]),
    (WEEKLY, 1, ["2026-10-09", "2026-10-16", "2026-10-23", "2026-10-30"]),
    (EVERY_N_DAYS, 3, ["2026-10-09", "2026-10-12", "2026-10-15", "2026-10-18"]),
])
def test_batch_schedule_intervals(interval, days, expect):
    out = schedule_times(date(2026, 10, 9), dtime(19, 0), "Asia/Seoul", 4, interval, days)
    from zoneinfo import ZoneInfo
    assert [d.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat() for d in out] == expect
    assert all(d.astimezone(ZoneInfo("Asia/Seoul")).hour == 19 for d in out)


def test_batch_schedule_uses_channel_timezone():
    kr = schedule_times(date(2026, 10, 6), dtime(19, 0), "Asia/Seoul", 1)[0]
    jp = schedule_times(date(2026, 10, 6), dtime(19, 0), "Asia/Tokyo", 1)[0]
    paris = schedule_times(date(2026, 10, 6), dtime(19, 0), "Europe/Paris", 1)[0]
    assert kr.isoformat() == jp.isoformat() == "2026-10-06T10:00:00+00:00"  # KST/JST 모두 UTC+9
    assert paris.isoformat() == "2026-10-06T17:00:00+00:00"
    assert round(estimate_seconds(10 * 1024 ** 3, 100) / 3600, 2) == 0.24


def plan_for(profile, videos, *, times, now, template=None, **kw):
    items = [BatchItem(str(v), m, episode_from_filename(v.name)) for v, m in zip(videos, match_all(videos))]
    return build_plan(profile, items, template or MetadataTemplate("t", "여_{episode}"), times=times,
                      privacy_now="private", now=now, **kw)


def test_build_plan_errors_and_warnings(folder):
    p = ChannelProfile(new_profile_id(), "🇯🇵 CHILI LAB", channel_id=JP["id"], channel_title=JP["title"], language="ja",
                       timezone="Asia/Tokyo")
    vids = scan_folder(folder)
    now = datetime(2026, 10, 5, tzinfo=timezone.utc)
    times = schedule_times(date(2026, 10, 6), dtime(19, 0), p.timezone, len(vids))
    plan = plan_for(p, vids, times=times, now=now)
    assert plan.ok and plan.thumb_count == 3 and len(plan.all_warnings) == 1
    assert [i.title for i in plan.items] == ["여_1", "여_2", "여_3", "여_10"]
    assert plan.items[0].local_text == "2026-10-06 (火) 19:00" and plan.items[0].privacy == "private"
    past = plan_for(p, vids, times=schedule_times(date(2026, 10, 1), dtime(19, 0), p.timezone, 4), now=now)
    assert not past.ok and "지났거나" in past.all_errors[0]
    long = plan_for(p, vids, times=times, now=now, template=MetadataTemplate("t", "x" * 101))
    assert not long.ok
    dup = build_plan(p, [BatchItem(str(vids[0])), BatchItem(str(vids[0]))], MetadataTemplate("t", "{filename}"),
                     times=times[:2], privacy_now="private", now=now)
    assert any("두 번" in e for e in dup.all_errors)
    unconnected = plan_for(ChannelProfile(new_profile_id(), "x"), vids, times=times, now=now)
    assert any("연결되지 않았습니다" in e for e in unconnected.all_errors)


# ================= 템플릿 / 복제 =================

def test_template_store_pick_order_and_isolation(studio):
    env, ps, kr, jp = studio
    store = UploadTemplateStore()
    a = store.save(kr.profile_id, MetadataTemplate("한국 시니어 / 가을 샹송", "{filename}", tags=["샹송"]))
    b = store.save(kr.profile_id, MetadataTemplate("한국 시니어 / 올드팝", "{filename}"))
    j = store.save(jp.profile_id, MetadataTemplate("CHILI LAB / 女", "{filename}"))
    assert store.pick_for(kr)[0] == a  # 첫 템플릿
    kr.default_template_id = b
    assert store.pick_for(kr)[0] == b  # 채널 기본 템플릿
    store.remember(kr.profile_id, a)
    assert store.pick_for(kr)[0] == a  # 마지막으로 쓴 템플릿
    store.remember(kr.profile_id, j)  # 다른 채널 템플릿은 절대 쓰지 않음
    assert store.pick_for(kr)[0] == b
    assert store.save(kr.profile_id, MetadataTemplate("한국 시니어 / 올드팝", "{n}")) == b  # 같은 이름 → 덮어쓰기
    assert len(store.for_profile(kr.profile_id)) == 2


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_clone_profile_copies_settings_not_oauth(studio):
    env, ps, kr, jp = studio
    store = UploadTemplateStore()
    tid = store.save(jp.profile_id, MetadataTemplate("CHILI LAB / 女", "{filename}", tags=["chill"]))
    jp.client_file = "C:/x/client_jp.json"
    jp.default_template_id = tid
    ps.save(jp)
    ps.token_store(jp.profile_id).save("refresh-jp", client_id="cid")
    new = clone_profile(ps, store, ps.get(jp.profile_id), "🇯🇵 CHILI LAB 남자")
    got = ps.get(new.profile_id)
    assert (got.language, got.timezone, got.category_id, got.privacy) == ("ja", "Asia/Tokyo", "24", "private")
    assert got.channel_id == got.channel_title == got.client_file == ""
    assert not ps.token_store(new.profile_id).has_saved()
    assert got.default_template_id and got.default_template_id != tid
    assert store.get(got.default_template_id)[0] == new.profile_id


# ================= Retry-After / fingerprint / 사용량 =================

def test_retry_after_header_is_used(fake, tmp_path):
    api = YouTubeApiClient(lambda force_refresh=False: FAKE_ACCESS, base_url=fake.api_base, sleep=lambda s: None)
    video = mp4(tmp_path / "v.mp4", 4)
    slept = []
    clock = lambda: 1_800_000_000.0  # noqa: E731
    up = ResumableUploader(api, transport=http_transport, chunk_size=KB256, sleep=slept.append, clock=clock)
    when = datetime.fromtimestamp(1_800_000_000 + 42, timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    fake.upload_fail.extend([("chunk", 429, False, {"Retry-After": "7"}), ("chunk", 503, False, {"Retry-After": when}),
                             ("chunk", 503, False)])
    from app.youtube_upload import build_video_body
    from app.youtube_metadata import BroadcastMetadata
    res = up.upload(video, build_video_body(BroadcastMetadata(title="x"), None))
    assert fake.videos[res["id"]]["bytes"] == video.read_bytes()
    assert slept == [7.0, 42.0, 4.0]  # Retry-After 초 / HTTP 날짜 우선, 없으면 기존 backoff (3번째 → 4초)
    assert parse_retry_after("99999") == 900.0 and parse_retry_after("soon") is None and parse_retry_after("") is None


def test_fingerprint_detects_change_with_same_size_and_mtime(tmp_path):
    v = mp4(tmp_path / "v.mp4", 12)  # 3MB+ : 앞 1MB·뒤 1MB
    sig = file_signature(v)
    assert sig.startswith("v2:") and signature_matches(v, sig)
    st = v.stat()
    data = bytearray(v.read_bytes())
    data[-1] ^= 0xFF
    v.write_bytes(bytes(data))
    os.utime(v, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert v.stat().st_size == st.st_size and not signature_matches(v, sig)
    legacy = f"{v.stat().st_size}:{int(v.stat().st_mtime)}"  # 이전 버전으로 저장된 작업
    assert signature_matches(v, legacy) and signature_matches(v, "")


def test_changed_file_blocks_upload_and_usage_counter(studio, fake, tmp_path):
    env, ps, kr, jp = studio
    q = env.queue(ps)
    v = mp4(tmp_path / "LONG.mp4", 8)
    ok = mp4(tmp_path / "OK.mp4", 2)
    job = q.add(q.make_job(profile_id=kr.profile_id, video_path=str(v), title="x"))
    q.add(q.make_job(profile_id=kr.profile_id, video_path=str(ok), title="y"))
    st = v.stat()
    data = bytearray(v.read_bytes())
    data[0] ^= 0xFF
    v.write_bytes(bytes(data))
    os.utime(v, ns=(st.st_atime_ns, st.st_mtime_ns))
    q.run_pending()
    assert job.status == FAILED and "예약 등록 후 영상 파일이 변경되었습니다" in job.error
    assert q.jobs[1].status == COMPLETE and len(fake.sessions) == 1
    u = today_usage(env.clock)
    assert u["uploads"] == 1 and u["units"] >= 1600


# ================= GUI =================

def test_main_window_small_screen_scrolls_to_start_stop(tmp_path, monkeypatch):
    import app.ui as ui
    monkeypatch.setattr(ui, "discover_ffmpeg", lambda root: None)
    try:
        app = ui.MainWindow(tmp_path)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no display: {e}")
    try:
        assert app.title().startswith("YouTube Playlist Studio")
        app.geometry("1080x800+0+0")
        app.update()
        cv = app.scroll.canvas

        def visible(w):
            top, bottom = cv.winfo_rooty(), cv.winfo_rooty() + cv.winfo_height()
            return top <= w.winfo_rooty() and w.winfo_rooty() + w.winfo_height() <= bottom
        assert app.scroll.scrollable and not visible(app.start)  # 800px: 아래 버튼은 처음엔 안 보임
        cv.yview_moveto(1.0)
        app.update()
        assert visible(app.start) and visible(app.stop)
        cv.yview_moveto(0.0)
        app.update()
        # 마우스 휠: 일반 위젯 위 → 페이지 스크롤, 빈 Treeview 위 → 페이지 스크롤, 값 위젯 위 → 무시
        ev = SimpleNamespace(widget=app.tool_text, delta=-120, num=0)
        assert app.scroll._on_wheel(ev) == "break"
        app.update()
        assert cv.yview()[0] > 0
        assert app.scroll._on_wheel(SimpleNamespace(widget=app.qtree, delta=-120, num=0)) == "break"
        def find(w, cls):
            if w.winfo_class() == cls:
                return w
            for c in w.winfo_children():
                got = find(c, cls)
                if got is not None:
                    return got
            return None
        spin = find(app.scroll.body, "TSpinbox")
        assert spin is not None and app.scroll._on_wheel(SimpleNamespace(widget=spin, delta=-120, num=0)) is None
        # 큰 화면: 스크롤 필요 없음, 기존처럼 꽉 차게
        app.geometry("1080x1060+0+0")
        app.update()
        assert visible(app.start)
    finally:
        app._finish_close()


def test_template_auto_apply_and_channel_switch_clears(root, studio):
    env, ps, kr, jp = studio
    store = UploadTemplateStore()
    store.save(kr.profile_id, MetadataTemplate("한국 시니어 / 가을 샹송", "가을 샹송 {date}", "가을에 듣기 좋은 샹송",
                                               tags=["샹송", "가을"], default_language="ko"))
    w, q = window(root, env, ps, templates=store)
    assert not w.detail_open.get() and not w.detail_frame.winfo_manager()  # 상세 설정은 기본 접힘
    w.select_profile(kr.profile_id)
    assert w.txt_desc.get("1.0", "end").strip() == "가을에 듣기 좋은 샹송" and w.tags.get() == "샹송, 가을"
    assert w.template_choice.get() == "한국 시니어 / 가을 샹송"
    w.select_profile(jp.profile_id)  # 일본 채널: 템플릿 없음 → 한국 설명/태그가 남지 않음
    assert w.txt_desc.get("1.0", "end").strip() == "" and w.tags.get() == "" and w.title_template.get() == "{filename}"
    assert w.category.get().endswith("(24)") and w.language.get().endswith("(ja)")
    w.toggle_detail()
    assert w.detail_frame.winfo_manager() == "pack"
    w.template_name.set("CHILI LAB / 女")
    w.txt_desc.insert("1.0", "Tokyo night playlist.\nEP {episode}")
    w.tags.set("chill, tokyo")
    tid = w.save_template()
    assert tid and store.get(tid)[0] == jp.profile_id
    w.select_profile(kr.profile_id)
    assert "샹송" in w.tags.get()
    w.select_profile(jp.profile_id)  # 마지막으로 쓴 일본 템플릿 자동 적용
    assert w.tags.get() == "chill, tokyo" and "EP {episode}" in w.txt_desc.get("1.0", "end")
    w.destroy()


def test_folder_import_preview_and_enqueue_jp(root, studio, folder):
    env, ps, kr, jp = studio
    w, q = window(root, env, ps)
    w.select_profile(jp.profile_id)
    assert w.add_folder(str(folder)) == 4
    rows = [w.items_tree.item(i, "values") for i in w.items_tree.get_children()]
    assert [(r[1], r[2], r[3]) for r in rows] == [("001.mp4", "001.jpg ✓", "1"), ("002.mp4", "002.png ✓", "2"),
                                                  ("003.mp4", "없음 ⚠", "3"), ("010.mp4", "010_thumbnail.jpg ✓", "10")]
    assert "4개" in w.items_summary.get() and "참고용" in w.items_summary.get()
    w.items_tree.selection_set(w.items_tree.get_children()[2])
    w.episode_var.set("30")
    w.assign_episode()
    assert w.items[2].episode == "30"
    day = env_tomorrow(env, "Asia/Tokyo")
    w.pub_date.set(day.isoformat())
    w.pub_time.set("21:00")
    w.interval.set(DAILY)
    w.title_template.set("女_{episode}")
    dlg = w.preview()
    assert pump(root, lambda: dlg.verify_state == "ok")
    assert "CHILI LAB" in dlg.verify_text.get()
    vals = [dlg.tree.item(i, "values") for i in dlg.tree.get_children()]
    assert [v[2] for v in vals] == ["女_1", "女_2", "女_30", "女_10"]
    assert vals[1][1].startswith((day + timedelta(days=1)).isoformat()) and vals[1][1].endswith("21:00")
    assert "썸네일" in dlg.problems.get() and dlg.errors == [] and len(dlg.warnings) == 1
    assert str(dlg.btn_add.cget("text")) == "4개 대기열에 추가"
    jobs = dlg.confirm()
    assert len(jobs) == 4 and all(j.channel_id == JP["id"] and j.timezone == "Asia/Tokyo" for j in jobs)
    assert jobs[0].publish_local_text() == f"{day.isoformat()} 21:00 Asia/Tokyo"
    assert jobs[2].thumbnail_path == "" and jobs[0].thumbnail_path.endswith("001.jpg")
    assert w.items == [] and len(w.tree.get_children()) == 4
    # 최근 폴더 다시 불러오기 (새 창)
    w.destroy()
    assert last_folder() == str(folder)
    w2, _ = window(root, env, ps)
    assert w2.reload_last_folder() == 4 and len(w2.items) == 4
    w2.destroy()


def test_preview_blocks_wrong_channel(root, studio, tmp_path):
    env, ps, kr, jp = studio
    env.accounts[jp.profile_id] = (KR, "tok-kr-as-jp")  # 일본 프로필 연결이 실제로는 한국 채널
    w, q = window(root, env, ps)
    w.select_profile(jp.profile_id)
    w.add_videos([str(mp4(tmp_path / "001.mp4"))])
    w.pub_date.set(env_tomorrow(env, "Asia/Tokyo").isoformat())
    dlg = w.preview()
    assert pump(root, lambda: dlg.verify_state == "mismatch")
    assert "채널 불일치" in dlg.verify_text.get() and str(dlg.btn_add.cget("state")) == "disabled"
    assert dlg.confirm() is None and q.snapshot() == []
    dlg.destroy()
    w.destroy()


@pytest.mark.parametrize("kind,answer,started,asked", [("local", False, False, True), ("local", True, True, True),
                                                       ("cloud", False, True, False), ("", False, True, False)])
def test_local_live_bandwidth_guard(root, studio, tmp_path, monkeypatch, kind, answer, started, asked):
    import app.youtube_upload_preview as pv
    env, ps, kr, jp = studio
    calls = []
    monkeypatch.setattr(pv, "ask_bandwidth", lambda parent: calls.append(1) or answer)
    w, q = window(root, env, ps, live_guard=lambda: kind)
    q.add(q.make_job(profile_id=kr.profile_id, video_path=str(mp4(tmp_path / "v.mp4")), title="x"))
    assert w.start() is started and bool(calls) is asked
    if started:
        assert pump(root, lambda: not q.running and q.snapshot()[0].status == COMPLETE)
    else:
        assert not q.running and q.snapshot()[0].status == PENDING and "LIVE" in w.summary.get()
    w.destroy()


def test_queue_bulk_actions_and_status_colors(root, studio, tmp_path):
    env, ps, kr, jp = studio
    w, q = window(root, env, ps)
    for n in range(3):
        q.add(q.make_job(profile_id=kr.profile_id, video_path=str(mp4(tmp_path / f"v{n}.mp4")), title=f"t{n}"))
    q.jobs[0].status, q.jobs[1].status = COMPLETE, FAILED
    q.save()
    w.refresh_jobs()
    ids = w.tree.get_children()
    assert w.tree.item(ids[0], "tags") == (COMPLETE,) and w.tree.set(ids[0], "state") == "예약 완료"
    assert w.tree.item(ids[1], "tags") == (FAILED,) and w.tree.set(ids[1], "state") == "실패"
    w.select_all()
    assert len(w.tree.selection()) == 3
    w.retry_failed()
    assert [j.status for j in q.snapshot()] == [COMPLETE, PENDING, PENDING]
    w.clear_done()
    assert len(q.snapshot()) == 2
    w.select_all()
    w.remove_selected()
    assert q.snapshot() == []
    w.destroy()


def test_channel_manager_clone_and_default_template(root, studio):
    from app.youtube_channels_ui import ChannelManagerWindow
    env, ps, kr, jp = studio
    store = UploadTemplateStore()
    tid = store.save(kr.profile_id, MetadataTemplate("한국 시니어 / 올드팝", "{filename}"))
    cm = ChannelManagerWindow(root, profiles=ps, templates=store)
    cm.tree.selection_set(kr.profile_id)
    cm._on_select()
    assert "한국 시니어 / 올드팝" in cm.cb_default_template.cget("values")
    cm.default_template.set("한국 시니어 / 올드팝")
    cm.save_form()
    assert ps.get(kr.profile_id).default_template_id == tid
    new = cm.clone_selected()
    got = ps.get(new.profile_id)
    assert got.alias == "🇰🇷 한국 시니어 복사본" and got.channel_id == "" and got.language == "ko"
    assert cm.selected_id == new.profile_id and "연결 안 됨" in cm.channel_text.get()
    assert got.default_template_id and got.default_template_id != tid
    cm.destroy()


def test_dnd_optional_fallback_keeps_pickers(root, studio, tmp_path):
    import importlib.util
    env, ps, kr, jp = studio
    picked = [str(mp4(tmp_path / "a.mp4"))]
    w, q = window(root, env, ps, pick_files=lambda **kw: picked, pick_dir=lambda **kw: "")
    assert w.dnd_enabled is False  # v1.1: 끌어놓기 꺼 둠 (tkinterdnd2 설치 여부와 무관하게 정상 동작)
    assert importlib.util.find_spec("tkinterdnd2") is None or w.dnd_enabled is False
    w.pick_videos()
    assert [i.video_path for i in w.items] == picked
    w.pick_folder()  # 취소 → 아무 일 없음
    assert len(w.items) == 1
    w.destroy()
