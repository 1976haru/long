"""LIVE 창 ⑦ YouTube 방송 정보 화면 — 채널별 저장/전환, 연결 없음=로컬만, 명시적 방송 선택 적용, 계정 확인, 재시도, Tk 정리.
가짜 YouTube만 사용 (실제 Google 로그인/YouTube 쓰기 없음)."""
import json

import pytest

from app import settings as app_settings
from app.youtube_metadata_control import LOCAL_ONLY_TEXT, NO_PLAYLIST, SETTINGS_KEY, STEP_THUMBNAIL, load_metadata
from tests.test_live_beginner_ux import make, root, shown, shown_boxes  # noqa: F401 (fixtures)
from tests.test_live_metadata_control import WRITE_OPS, api_for, png
from tests.youtube_fakes import FAKE_ACCESS, FAKE_REFRESH, FakeYouTube


@pytest.fixture
def fake():
    f = FakeYouTube()
    yield f
    f.close()


def type_in(p, *, title="", desc="", tags="", privacy=None):
    p.title_var.set(title)
    p.txt_desc.delete("1.0", "end")
    p.txt_desc.insert("1.0", desc)
    p.txt_tags.delete("1.0", "end")
    p.txt_tags.insert("1.0", tags)
    if privacy:
        p.privacy_var.set(privacy)


def connect(w, fake, *, channel_id=None, title="Old Pop Lounge", token=FAKE_ACCESS):
    """가짜 연결: 이 창의 YouTube 연결/ API만 가짜로 바꾼다 (실제 token/로그인 없음)."""
    w._yt_connected = lambda: True
    w._yt_ok = True
    w._yt_api = lambda: api_for(fake, token)
    w._yt_expected_channel_id = lambda: channel_id or fake.channel["id"]
    w._yt_channel_title = lambda: title
    w.meta_panel.sync()


def writes(fake):
    return [c for c in fake.ops() if c in WRITE_OPS]


def test_per_channel_fields_isolated_drafts_and_restore(root, shown_boxes):
    w, store, ctls = make(root)
    try:
        p = w.meta_panel
        assert p.pid == "default" and p.title_var.get() == "" and p.playlist_var.get() == NO_PLAYLIST
        assert p.privacy_var.get() == "일부공개" and p.category_var.get() == "음악"
        type_in(p, title="시니어 추억의 가요 LIVE", desc="1줄\n2줄", tags="트로트, 가요\n트로트", privacy="비공개")
        assert p.title_count.get() == "15 / 100자"  # 현재 글자 수 / 최대
        assert p.save() and p.txt_tags.get("1.0", "end-1c") == "트로트, 가요"  # 정리된 값으로 다시 표시
        w._switch_channel("senior")
        assert p.pid == "senior" and p.title_var.get() == "" and p.txt_desc.get("1.0", "end-1c") == ""  # 섞이지 않음
        type_in(p, title="Tokyo Chill LIVE", tags="Tokyo Chill, R&B")
        p.category_var.set("엔터테인먼트")
        p._on_category()
        assert p.save()
        type_in(p, title="저장 안 한 입력")  # 저장 전 입력은 채널별로 보관 (다른 채널로 새지 않음)
        w._switch_channel("default")
        assert p.title_var.get() == "시니어 추억의 가요 LIVE" and p.txt_desc.get("1.0", "end-1c") == "1줄\n2줄"
        assert p.privacy_var.get() == "비공개"
        w._switch_channel("senior")
        assert p.title_var.get() == "저장 안 한 입력"
        assert load_metadata("senior").title == "Tokyo Chill LIVE" and load_metadata("senior").category_id == "24"
        assert load_metadata("default").tags == ["트로트", "가요"]
    finally:
        w.destroy()
    w2, _, _ = make(root, channels=())  # 재실행: 마지막 채널(senior) + 저장된 값 복원 (저장 안 한 입력은 남지 않음)
    try:
        assert w2.channel_id == "senior" and w2.meta_panel.title_var.get() == "Tokyo Chill LIVE"
        w2._switch_channel("default")
        assert w2.meta_panel.title_var.get() == "시니어 추억의 가요 LIVE"
    finally:
        w2.destroy()


def test_empty_title_blocked_on_save(root, shown_boxes):
    w, store, ctls = make(root)
    try:
        type_in(w.meta_panel, title="   ")
        assert not w.meta_panel.save()
        assert shown_boxes[-1][0] == "showerror" and "방송 제목" in shown_boxes[-1][2]
        assert SETTINGS_KEY not in app_settings.load_settings()
    finally:
        w.destroy()


def test_manual_key_without_oauth_is_local_only(root, shown_boxes, fake):
    w, store, ctls = make(root)
    try:
        p = w.meta_panel
        called = []
        w._yt_api = lambda: called.append(1) or api_for(fake)
        p.sync()
        assert p.mode_text.get() == LOCAL_ONLY_TEXT
        assert str(p.btn_apply.cget("state")) == "disabled" and str(p.btn_refresh.cget("state")) == "disabled"
        type_in(p, title="Tokyo Chill LIVE")
        assert p.save() and "로컬에 저장됨" in p.result_text.get()
        p.apply()  # 버튼이 꺼져 있어도 직접 불러 보기: 여전히 YouTube 쓰기 없음
        p.refresh_lists()
        assert shown_boxes[-1][0] == "showinfo" and "YouTube 연결이 필요" in shown_boxes[-1][2]
        assert called == [] and fake.calls == [] and writes(fake) == []
    finally:
        w.destroy()


def test_manual_key_with_oauth_applies_only_explicitly_selected_broadcast(root, shown_boxes, fake, tmp_path):
    w, store, ctls = make(root)
    try:
        api = api_for(fake)
        first = api.insert_broadcast(title="시니어 방송 (예정)", privacy="unlisted").id
        chosen = api.insert_broadcast(title="Old Pop LIVE #03", privacy="unlisted").id
        fake.broadcasts[chosen]["status"]["lifeCycleStatus"] = "live"
        pl = fake.add_playlist("Old Pop LIVE", fake.channel["id"])
        before_first = json.dumps(fake.broadcasts[first]["snippet"], sort_keys=True)
        connect(w, fake)
        p = w.meta_panel
        assert "Old Pop Lounge" in p.mode_text.get() and str(p.btn_apply.cget("state")) == "normal"
        p.refresh_lists()
        p.wait_idle()
        assert "Old Pop LIVE" in list(p.cmb_playlist.cget("values"))
        type_in(p, title="Old Pop 24H", desc="설명\n둘째 줄", tags="oldpop, lounge", privacy="공개")
        p.playlist_var.set("Old Pop LIVE")
        p.thumb_var.set(png(tmp_path / "t.png"))
        seen = {}

        def cancel(channel_title, broadcasts, changes):
            seen["cancel"] = [b.id for b in broadcasts]
            return None
        p.on_choose_target = cancel
        n = len(writes(fake))
        p.apply()
        p.wait_idle()
        assert seen["cancel"] == [chosen, first] and len(writes(fake)) == n  # 취소 = 쓰기 0 (자동 선택 없음)

        def pick(channel_title, broadcasts, changes):
            seen.update(channel=channel_title, changes=changes)
            return next(b for b in broadcasts if b.id == chosen)
        p.on_choose_target = pick
        p.apply()
        p.wait_idle()
        assert seen["channel"] == "Old Pop Lounge" and "제목: Old Pop 24H" in seen["changes"]
        b = fake.broadcasts[chosen]
        assert b["snippet"]["title"] == "Old Pop 24H" and b["snippet"]["tags"] == ["oldpop", "lounge"]
        assert b["status"]["privacyStatus"] == "public" and fake.thumbnails[chosen] and fake.playlists[pl]["items"] == [chosen]
        assert json.dumps(fake.broadcasts[first]["snippet"], sort_keys=True) == before_first  # 다른 방송 그대로
        assert first not in fake.thumbnails
        assert "✓ 제목/설명" in p.result_text.get() and "Old Pop LIVE #03" in p.result_text.get()
        assert not p.btn_retry.winfo_manager()
        for secret in (FAKE_ACCESS, FAKE_REFRESH):
            assert secret not in p.result_text.get() and secret not in p.mode_text.get()
            assert all(secret not in str(x) for x in shown_boxes)
    finally:
        w.destroy()


def test_wrong_account_blocked_before_any_write(root, shown_boxes, fake):
    w, store, ctls = make(root)
    try:
        api_for(fake).insert_broadcast(title="x", privacy="unlisted")
        connect(w, fake, channel_id="UCtokyo000001")  # 이 채널은 도쿄칠 계정이어야 하는데 token은 다른 채널
        type_in(w.meta_panel, title="Tokyo Chill LIVE")
        w.meta_panel.on_choose_target = lambda *a: pytest.fail("방송 선택까지 가면 안 됨")
        n = len(writes(fake))
        w.meta_panel.apply()
        w.meta_panel.wait_idle()
        assert shown_boxes[-1][0] == "showerror" and "채널 불일치" in shown_boxes[-1][2]
        assert len(writes(fake)) == n
    finally:
        w.destroy()


def test_no_broadcast_to_apply(root, shown_boxes, fake):
    w, store, ctls = make(root)
    try:
        connect(w, fake)
        type_in(w.meta_panel, title="t")
        w.meta_panel.apply()
        w.meta_panel.wait_idle()
        assert shown_boxes[-1][0] == "showinfo" and "적용할 방송이 없습니다" in shown_boxes[-1][2]
        assert writes(fake) == []
    finally:
        w.destroy()


def test_partial_failure_then_retry_failed_only_from_ui(root, shown_boxes, fake, tmp_path):
    w, store, ctls = make(root)
    try:
        bid = api_for(fake).insert_broadcast(title="LIVE", privacy="unlisted").id
        connect(w, fake)
        p = w.meta_panel
        type_in(p, title="Old Pop 24H")
        p.thumb_var.set(png(tmp_path / "t.png"))
        p.on_choose_target = lambda c, bs, ch: bs[0]
        fake.fail.extend([("upload/thumbnails", 500, "backendError")] * 5)
        p.apply()
        p.wait_idle()
        assert "✗ 썸네일" in p.result_text.get() and "✓ 제목/설명" in p.result_text.get()
        assert shown(p.btn_retry)
        fake.fail.clear()
        n_put = fake.ops().count(("PUT", "videos"))
        p.btn_retry.invoke()
        p.wait_idle()
        assert bid in fake.thumbnails and "✓ 썸네일" in p.result_text.get()
        assert fake.ops().count(("PUT", "videos")) == n_put  # 성공했던 항목은 다시 보내지 않음
        assert not p.btn_retry.winfo_manager()
    finally:
        w.destroy()


def test_api_session_uses_channel_metadata_and_shows_result(root, shown_boxes, fake):
    from app.youtube_metadata_control import ApplyResult
    w, store, ctls = make(root)
    try:
        p = w.meta_panel
        w.yt_title.set("③ 기존 제목")
        t = w._yt_template()
        assert t.title == "③ 기존 제목" and w._yt_session_meta is None  # ⑦ 제목 비어 있음 → 기존 그대로
        type_in(p, title="⑦ 채널 제목", desc="⑦ 설명", privacy="공개")
        t = w._yt_template()
        assert (t.title, t.description, t.privacy) == ("⑦ 채널 제목", "⑦ 설명", "public")
        assert w._yt_session_meta[0] == "default" and w._yt_session_meta[1].title == "⑦ 채널 제목"
        r = ApplyResult(video_id="b1", broadcast_title="⑦ 채널 제목")
        r.steps.update({"title_description": True, "privacy": True, "thumbnail": False})
        r.errors["thumbnail"] = "썸네일 실패"
        p.set_session_result("default", object(), w._yt_session_meta[1], r, "UC1")
        assert "✗ 썸네일" in p.result_text.get() and shown(p.btn_retry)
        w._switch_channel("senior")
        assert p.result_text.get() == "" and not p.btn_retry.winfo_manager()  # 다른 채널 결과는 안 보임
    finally:
        w.destroy()


def test_apply_target_dialog_requires_explicit_choice(root):
    from app.live_metadata_ui import ApplyTargetDialog
    from app.youtube_api import YouTubeBroadcastInfo
    bs = [YouTubeBroadcastInfo("b1", "Tokyo Chill Live #03", "live", "public"),
          YouTubeBroadcastInfo("b2", "다른 방송", "ready", "unlisted", scheduled_start="2026-10-11T12:00:00Z")]
    d = ApplyTargetDialog(root, "Tokyo Chill", bs, ["제목: x", "공개 상태: 공개"])
    try:
        assert str(d.btn_ok.cget("state")) == "disabled" and d.cmb.current() == -1  # 미리 골라 두지 않음
        d.ok()
        assert d.result is None
        d.select(0)
        d.ok()
        assert d.result.id == "b1"
    finally:
        if d.winfo_exists():
            d.destroy()


def test_tk_variables_released_on_destroy(root, shown_boxes):
    import tkinter as tk
    w, store, ctls = make(root)
    p = w.meta_panel
    w.destroy()
    tk_vars = [v for v in vars(p).values() if isinstance(v, tk.Variable)]
    assert tk_vars and all(v._tk is None for v in tk_vars) and p._poll_job is None
