"""Studio v1.4: Google 연결 파일 만들기 도우미 + YouTube 재생목록. fake 서버만 사용 — 실제 YouTube/OCI 접속 없음.
재생목록 이름(그의 이야기 등)은 fake 데이터에서만 쓴다."""
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import help_content as hc
from app.youtube_accounts import ChannelProfile, ProfileStore, new_profile_id
from app.youtube_api import PlaylistOwnerError
from app.youtube_batch import UploadTemplateStore
from app.youtube_metadata import MetadataTemplate
from app.youtube_upload_queue import BLOCKED, COMPLETE, PARTIAL, PENDING, UploadQueue
from app.youtube_usage import today_usage
from test_youtube_upload_queue import JP, KR, Env
from youtube_fakes import FakeYouTube

ROOT = Path(__file__).resolve().parent.parent
HIS, HERS, BOTH = "그의 이야기", "그녀의 이야기", "그와 그녀의 이야기"


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


def client_json(tmp_path, name="client_secret_desktop.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"installed": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "GOCSPX-fake",
                                           "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                                           "token_uri": "https://oauth2.googleapis.com/token"}}))
    return p


@pytest.fixture
def world(fake):
    ps = ProfileStore()
    kr = ps.add(ChannelProfile(new_profile_id(), "한국 시니어", channel_id=KR["id"], channel_title=KR["title"],
                               language="ko", timezone="Asia/Seoul"))
    jp = ps.add(ChannelProfile(new_profile_id(), "CHILI LAB", channel_id=JP["id"], channel_title=JP["title"],
                               language="ja", timezone="Asia/Tokyo"))
    env = Env(fake, ps, kr, jp)
    pls = {t: fake.add_playlist(t, JP["id"]) for t in (HIS, HERS, BOTH)}
    pls["KR"] = fake.add_playlist("가을 샹송", KR["id"])
    return env, ps, kr, jp, pls


def mp4(path, n=2):
    path.write_bytes(bytes(range(256)) * (1024 * n + 3))
    return path


# ================= A. Google 연결 도우미 =================

def test_assistant_opens_with_steps_buttons_and_copy(root):
    from app.help_ui import GoogleConnectionAssistant, show_oauth_help
    opened, picked = [], []
    a = GoogleConnectionAssistant(root, on_pick=lambda: picked.append(1), open_url=opened.append)
    labels = texts(a)
    assert a.title() == "Google 연결 파일 만들기"
    for b in ("Google Cloud 열기", "Google 공식 설명 열기", "설정 순서 복사", "다운로드한 연결 파일 선택",
              "브라우저를 보는 동안 안내창을 위에 표시"):
        assert b in labels, b
    intro = " ".join(labels)
    assert "Google 비밀번호가 들어 있는 파일은 아닙니다" in intro and "GitHub" in intro
    steps = a.text.get("1.0", "end")
    for i in range(1, 7):
        assert f"STEP {i}." in steps
    assert "Desktop app" in steps and "YouTube Data API v3" in steps and "Test users" in steps
    next(w for w in _walk(a) if getattr(w, "cget", None) and "text" in w.keys() and w.cget("text") == "Google Cloud 열기").invoke()
    assert opened == [hc.GOOGLE_CLOUD_URL]
    copied = a.copy_steps()
    assert root.clipboard_get() == copied and "STEP 6." in copied and "✓" in a.copied.get()
    assert not int(a.attributes("-topmost"))  # 강제하지 않음
    a.keep_on_top.set(True)
    a._apply_top()
    assert int(a.attributes("-topmost"))
    a.pick()
    assert picked == [1] and not a.winfo_exists()
    b = show_oauth_help(root)  # [이 파일이 뭔가요?] → 같은 도우미
    assert isinstance(b, GoogleConnectionAssistant)
    b.destroy()


def _walk(w):
    yield w
    for c in w.winfo_children():
        yield from _walk(c)


def test_wizard_first_connect_has_file_flow(root, tmp_path):
    from app.help_ui import SetupWizard
    w = SetupWizard(root, profiles=ProfileStore(), ffmpeg_finder=lambda: ("f", "p"), internet=lambda: True,
                    pick_file=lambda **kw: str(client_json(tmp_path)), guide=lambda parent: True)
    w.next()
    w.choose_preset("kr")
    d = w.start_first_connect()
    assert "Google 연결 파일이 이미 있나요?" in texts(d)
    d.btn_have.invoke()  # [있어요 - 파일 선택]
    assert w.client_file.get().endswith("client_secret_desktop.json") and w.btn_connect is not None
    w.destroy()


def test_wizard_first_connect_first_time_flow(root, tmp_path):
    from app.help_ui import GoogleConnectionAssistant, SetupWizard
    w = SetupWizard(root, profiles=ProfileStore(), ffmpeg_finder=lambda: ("f", "p"), internet=lambda: True,
                    pick_file=lambda **kw: str(client_json(tmp_path)), guide=lambda parent: True)
    w.next()
    w.choose_preset("jp")
    w.alias_var.set("CHILI LAB")
    d = w.start_first_connect()
    d.btn_first.invoke()  # [처음이에요 - 만드는 방법 보기]
    assert isinstance(w.assistant, GoogleConnectionAssistant) and w.assistant.winfo_exists()
    w.assistant.btn_pick.invoke()  # [다운로드한 연결 파일 선택]
    assert w.client_file.get() and w.btn_connect is not None
    w.destroy()


def test_advanced_mode_keeps_direct_file_picker(root, tmp_path):
    from app.help_ui import SetupWizard
    from app.ui_text import set_beginner
    set_beginner(False)
    w = SetupWizard(root, profiles=ProfileStore(), ffmpeg_finder=lambda: ("f", "p"), internet=lambda: True)
    w.next()
    w.choose_preset("kr")
    body = texts(w.body)
    assert "Google 연결 파일 선택" in body and "이 파일이 뭔가요?" in body and w.btn_connect is not None
    w.destroy()


def test_bundled_client_provider_skips_file_step(root, tmp_path, monkeypatch):
    import app.help_ui as help_ui
    import app.youtube_client_provider as prov
    from app.help_ui import SetupWizard
    bundled = client_json(tmp_path, prov.BUNDLED_FILE_NAME)
    monkeypatch.setattr(prov, "_search_dirs", lambda: [tmp_path])
    assert prov.has_bundled_client() and prov.resolve_client(prov.BUNDLED_MARKER).client_id.startswith("cid")
    used = []

    def fake_connect(profiles, p, path, open_browser=None):
        used.append(path)
        p.client_file, p.channel_id, p.channel_title = path, KR["id"], KR["title"]
        return profiles.save(p)
    w = SetupWizard(root, profiles=ProfileStore(), connect=fake_connect, ffmpeg_finder=lambda: ("f", "p"),
                    internet=lambda: True, guide=lambda parent: True)
    w.next()
    w.choose_preset("kr")
    body = texts(w.body)
    assert "Google 연결 파일 선택" not in body and "Google 계정 처음 연결하기" not in body
    assert w.btn_connect is not None and any("들어 있는 Google 연결 정보" in x for x in body)
    w.start_connect()
    assert pump(root, lambda: w.connect_msg.get() == "✓ 연결 완료")
    assert used == [prov.BUNDLED_MARKER]  # 채널에는 표시만 저장 (파일 내용/secret 아님)
    assert bundled.is_file()
    w.destroy()
    monkeypatch.setattr(prov, "_search_dirs", lambda: [tmp_path / "none"])
    from app.youtube_oauth import OAuthError
    with pytest.raises(OAuthError, match="직접 선택"):
        prov.resolve_client(prov.BUNDLED_MARKER)


def test_manuals_contain_google_file_guide_and_playlists():
    for name in ("초보자_빠른시작.html", "사용자_매뉴얼.html", "문제해결.html"):
        text = (ROOT / "docs" / name).read_text(encoding="utf-8")
        assert "Google 연결 파일 만들기" in text and "Desktop app" in text, name
        assert "Google 계정 처음 연결하기" in text and "처음이에요 - 만드는 방법 보기" in text
    manual = (ROOT / "docs" / "사용자_매뉴얼.html").read_text(encoding="utf-8")
    assert "재생목록 사용하기" in manual and "+ 새로 만들기" in manual and "재생목록만 다시 추가" in manual


def test_oauth_json_never_committed():
    out = subprocess.run(["git", "-C", str(ROOT), "-c", "core.quotepath=off", "ls-files", "-z"], capture_output=True)
    if out.returncode != 0:
        pytest.skip("git not available")
    files = [f for f in out.stdout.decode("utf-8").split("\0") if f]
    for f in files:
        name = Path(f).name.lower()
        assert not name.startswith("client_secret") and name != "bundled_oauth_client.json", f
        if name.endswith(".json"):
            text = (ROOT / f).read_text(encoding="utf-8", errors="ignore")
            assert "client_secret" not in text and "GOCSPX" not in text, f
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "bundled_oauth_client.json" in gi and "client_secret*.json" in gi


# ================= B. 재생목록 API =================

def test_playlist_api_list_create_add_and_isolation(world, fake):
    env, ps, kr, jp, pls = world
    jp_api = env.api_factory(jp, ps)
    kr_api = env.api_factory(kr, ps)
    assert {p.title for p in jp_api.list_playlists()} == {HIS, HERS, BOTH}  # 내 채널 것만
    assert [p.title for p in kr_api.list_playlists()] == ["가을 샹송"]
    for i in range(60):
        fake.add_playlist(f"시리즈 {i}", KR["id"])
    assert len(kr_api.list_playlists()) == 61  # 50개 넘으면 다음 페이지까지
    new = jp_api.create_playlist("새로운 시리즈", "설명", "unlisted")
    assert new.channel_id == JP["id"] and fake.playlists[new.id]["privacy"] == "unlisted"
    vid = fake._id("vid")
    fake.videos[vid] = {"id": vid, "snippet": {}, "status": {"privacyStatus": "private"}, "channel": JP["id"], "bytes": b""}
    item = jp_api.add_video_to_playlist(pls[HIS], vid)
    assert item.startswith("PLI") and fake.playlists[pls[HIS]]["items"] == [vid]
    assert jp_api.add_video_to_playlist(pls[HIS], vid) == "already"  # videoAlreadyInPlaylist = 성공
    assert fake.playlists[pls[HIS]]["items"] == [vid]
    with pytest.raises(PlaylistOwnerError):
        kr_api.ensure_own_playlist(pls[HIS], KR["id"])  # 일본 채널 재생목록을 한국 채널에서
    assert jp_api.ensure_own_playlist(pls[HIS], JP["id"]).title == HIS
    from app.youtube_api import QUOTA_COSTS
    assert (QUOTA_COSTS["playlists.list"], QUOTA_COSTS["playlists.insert"], QUOTA_COSTS["playlistItems.insert"]) == (1, 50, 50)


# ================= B. 업로드 대기열 =================

def test_queue_adds_scheduled_video_to_playlist_and_counts_quota(world, fake, tmp_path):
    env, ps, kr, jp, pls = world
    q = env.queue(ps)
    publish = datetime.fromtimestamp(env.clock() + 86400, timezone.utc)
    before = today_usage(env.clock)["units"]
    job = q.add(q.make_job(profile_id=jp.profile_id, video_path=str(mp4(tmp_path / "女_001.mp4")), title="女_001",
                           publish_at=publish, playlists=[(pls[HIS], HIS)]))
    q.run_pending()
    assert job.status == COMPLETE and fake.videos[job.video_id]["status"]["privacyStatus"] == "private"
    assert fake.playlists[pls[HIS]]["items"] == [job.video_id]  # 공개 전(비공개 예약)이라도 추가
    assert job.playlist_items[pls[HIS]].startswith("PLI") and job.playlist_text().startswith("재생목록 추가 완료")
    assert today_usage(env.clock)["units"] - before >= 1600 + 50 + 2


def test_queue_blocks_wrong_owner_playlist_before_upload(world, fake, tmp_path):
    env, ps, kr, jp, pls = world
    q = env.queue(ps)
    job = q.add(q.make_job(profile_id=kr.profile_id, video_path=str(mp4(tmp_path / "v.mp4")), title="x",
                           playlists=[(pls[HIS], HIS)]))  # 일본 채널 재생목록
    q.run_pending()
    assert job.status == BLOCKED and "재생목록이 현재 YouTube 채널의 것이 아닙니다" in job.error
    assert fake.sessions == {} and not job.video_id  # 영상도 올리지 않음


def test_playlist_partial_failure_retry_only_and_no_duplicate(world, fake, tmp_path):
    env, ps, kr, jp, pls = world
    q = env.queue(ps)
    job = q.add(q.make_job(profile_id=jp.profile_id, video_path=str(mp4(tmp_path / "v.mp4")), title="x",
                           playlists=[(pls[HIS], HIS), (pls[BOTH], BOTH)]))
    fake.fail.extend([("playlistItems", 403, "forbidden")])  # 첫 재생목록 추가만 실패
    q.run_pending()
    assert job.status == PARTIAL and job.video_id in fake.videos  # 영상은 지우지 않음
    assert "재생목록 추가 실패" in job.error and pls[HIS] not in job.playlist_items and pls[BOTH] in job.playlist_items
    from app.ui_text import friendly_error
    assert friendly_error(message=job.error).problem == "업로드는 완료됐지만 재생목록에 넣지 못했습니다."
    sessions = len(fake.sessions)
    q.retry(job.job_id)  # [재생목록만 다시 추가]
    q.run_pending()
    assert job.status == COMPLETE and len(fake.sessions) == sessions  # 영상은 다시 올리지 않음
    assert fake.playlists[pls[HIS]]["items"] == [job.video_id] and fake.playlists[pls[BOTH]]["items"] == [job.video_id]


def test_restart_does_not_duplicate_playlist_item(world, fake, tmp_path):
    env, ps, kr, jp, pls = world
    q = env.queue(ps)
    job = q.add(q.make_job(profile_id=jp.profile_id, video_path=str(mp4(tmp_path / "v.mp4")), title="x",
                           playlists=[(pls[HERS], HERS)]))
    fake.fail.extend([("playlistItems", 403, "forbidden")])
    q.run_pending()
    assert job.status == PARTIAL
    fake.playlists[pls[HERS]]["items"].append(job.video_id)  # 실제로는 들어갔는데 저장 전에 꺼진 상황
    q2 = env.queue(ps)  # 재실행
    j2 = q2.jobs[0]
    assert j2.video_id == job.video_id
    q2.retry(j2.job_id)
    q2.run_pending()
    assert j2.status == COMPLETE and j2.playlist_items[pls[HERS]] == "already"
    assert fake.playlists[pls[HERS]]["items"] == [job.video_id]  # 중복 없음
    q3 = env.queue(ps)
    calls = len([c for c in fake.calls if c[1] == "playlistItems"])
    j3 = q3.jobs[0]
    j3.status = PENDING
    q3.run_pending()
    assert len([c for c in fake.calls if c[1] == "playlistItems"]) == calls  # 이미 끝난 재생목록은 다시 호출 안 함


def test_kr_jp_playlist_isolation_in_queue(world, fake, tmp_path):
    env, ps, kr, jp, pls = world
    q = env.queue(ps)
    a = q.add(q.make_job(profile_id=kr.profile_id, video_path=str(mp4(tmp_path / "kr.mp4")), title="kr",
                         playlists=[(pls["KR"], "가을 샹송")]))
    b = q.add(q.make_job(profile_id=jp.profile_id, video_path=str(mp4(tmp_path / "jp.mp4")), title="jp",
                         playlists=[(pls[BOTH], BOTH)]))
    q.run_pending()
    assert (a.status, b.status) == (COMPLETE, COMPLETE)
    assert fake.playlists[pls["KR"]]["items"] == [a.video_id] and fake.playlists[pls[BOTH]]["items"] == [b.video_id]
    assert fake.videos[a.video_id]["channel"] == KR["id"] and fake.videos[b.video_id]["channel"] == JP["id"]


# ================= B. 화면 =================

def window(root, env, ps, **kw):
    from app.youtube_upload_ui import MultiChannelUploadWindow
    q = env.queue(ps)
    return MultiChannelUploadWindow(root, upload_queue=q, clock=env.clock, **kw), q


def test_template_default_playlist_and_channel_switch_resets(root, world):
    from app.youtube_upload_ui import NO_PLAYLIST
    env, ps, kr, jp, pls = world
    store = UploadTemplateStore()
    store.save(jp.profile_id, MetadataTemplate(HIS, "{filename}", default_playlist_id=pls[HIS], default_playlist_title=HIS))
    w, q = window(root, env, ps, templates=store)
    w.select_profile(jp.profile_id)
    assert w.playlist_choice.get() == HIS and w.selected_playlist == (pls[HIS], HIS)  # 템플릿 → 재생목록 자동 선택
    assert "ⓘ" in texts(w) and "+ 새로 만들기" in texts(w) and "새로고침" in texts(w)
    w.select_profile(kr.profile_id)  # 다른 채널 → 이전 채널 재생목록이 남지 않음
    assert w.selected_playlist is None and w.playlist_choice.get() == NO_PLAYLIST
    w.destroy()


def test_refresh_and_create_playlist_ui_saves_template_default(root, world, fake):
    env, ps, kr, jp, pls = world
    store = UploadTemplateStore()
    tid = store.save(jp.profile_id, MetadataTemplate(BOTH, "{filename}"))
    w, q = window(root, env, ps, templates=store)
    w.select_profile(jp.profile_id)
    w.refresh_playlists()
    assert pump(root, lambda: jp.profile_id in w._playlists)
    values = list(w.cb_playlist.cget("values"))
    assert set(values) == {"(재생목록에 넣지 않음)", HIS, HERS, BOTH} and "가을 샹송" not in values  # 이 채널 것만
    d = w.new_playlist()
    assert {"재생목록 이름", "설명", "공개 상태", "이 템플릿의 기본 재생목록으로 저장", "만들기"} <= set(texts(d))
    d.name.set("새로운 시리즈")
    d.create()
    assert pump(root, lambda: w.selected_playlist and w.selected_playlist[1] == "새로운 시리즈")
    assert w.playlist_choice.get() == "새로운 시리즈" and "새로운 시리즈" in w.cb_playlist.cget("values")
    new_id = w.selected_playlist[0]
    assert fake.playlists[new_id]["channel"] == JP["id"]
    assert store.get(tid)[1].default_playlist_id == new_id  # 다음부터 자동 선택
    w.destroy()


def test_batch_preview_playlist_and_owner_block(root, world, fake, tmp_path):
    env, ps, kr, jp, pls = world
    store = UploadTemplateStore()
    store.save(jp.profile_id, MetadataTemplate(HIS, "{filename}", default_playlist_id=pls[HIS], default_playlist_title=HIS))
    w, q = window(root, env, ps, templates=store)
    w.select_profile(jp.profile_id)
    d = tmp_path / "ten"
    d.mkdir()
    for i in range(1, 11):
        mp4(d / f"{i:03d}.mp4", 1)
    w.add_folder(str(d))
    from zoneinfo import ZoneInfo
    w.pub_date.set((datetime.fromtimestamp(env.clock(), timezone.utc).astimezone(ZoneInfo("Asia/Tokyo")).date()
                    + timedelta(days=1)).isoformat())
    dlg = w.preview()
    assert pump(root, lambda: dlg.verify_state == "ok")
    labels = texts(dlg)
    assert "템플릿" in labels and HIS in labels and "재생목록" in labels and "10개" in labels
    assert dlg.plan.playlist_text == HIS
    jobs = dlg.confirm()
    assert len(jobs) == 10 and all(j.playlist_ids == [pls[HIS]] for j in jobs)
    # 다른 채널 재생목록이 선택된 상태 → 미리보기에서 차단
    w.select_profile(kr.profile_id)
    w.selected_playlist = (pls[HERS], HERS)
    w.add_videos([str(mp4(tmp_path / "kr.mp4"))])
    w.pub_date.set((datetime.fromtimestamp(env.clock(), timezone.utc) + timedelta(days=2)).date().isoformat())
    dlg2 = w.preview()
    assert pump(root, lambda: dlg2.verify_state == "playlist")
    assert str(dlg2.btn_add.cget("state")) == "disabled" and any("재생목록이 현재 YouTube 채널의 것이 아닙니다" in e for e in dlg2.errors)
    dlg2.destroy()
    w.destroy()


def test_playlist_optional_and_advanced_multi(root, world, fake, tmp_path):
    from app.ui_text import set_beginner
    env, ps, kr, jp, pls = world
    w, q = window(root, env, ps)
    w.select_profile(jp.profile_id)
    assert not w.btn_multi_pl.winfo_manager()  # 초보자: 영상당 재생목록 1개만
    w.add_videos([str(mp4(tmp_path / "a.mp4"))])
    w.publish_kind.set("now")
    plan = w.build_plan()
    assert plan.playlist_text == "재생목록에 넣지 않음" and plan.items[0].playlists == []  # 선택 사항
    set_beginner(False)
    w.apply_mode()
    assert w.btn_multi_pl.winfo_manager() == "pack"
    w.refresh_playlists()
    assert pump(root, lambda: jp.profile_id in w._playlists)
    w.cb_playlist.set(HIS)
    w._on_playlist()
    d = w.pick_extra_playlists()
    titles = [d.lb.get(i) for i in range(d.lb.size())]
    d.lb.selection_set(titles.index(BOTH))
    d.ok()
    assert w.current_playlists() == [(pls[HIS], HIS), (pls[BOTH], BOTH)]
    w.items_tree.selection_set(w.items_tree.get_children()[0])
    w.assign_item_playlist()
    assert w.items[0].playlists == [(pls[HIS], HIS), (pls[BOTH], BOTH)]
    w.destroy()
