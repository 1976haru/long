"""② 예약 LIVE 창 — 초보자 빠른 예약 + Cloud 자동 송출 준비 화면 (가짜 YouTube/SSH 서버만, 실제 접속 없음)."""
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.youtube_api import YouTubeApiClient
from app.youtube_metadata import PRIVACY_LABELS
from cloud_fakes import make_client
from cloud_sched_fakes import SchedRemote
from test_scheduled_live import report
from youtube_fakes import FAKE_ACCESS, FAKE_STREAM_NAME, FakeClock, FakeYouTube


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


def pump(root, cond, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.03)
    return cond()


@pytest.fixture
def env(tmp_path, fake):
    from app.youtube_config import save_youtube_settings
    save_youtube_settings(client_file="x.json", channel_id="UCfake0001", channel_title="Old Pop Lounge")
    remote = SchedRemote(tmp_path / "server")
    client = make_client(tmp_path, remote)
    api = YouTubeApiClient(lambda force_refresh=False: FAKE_ACCESS, base_url=fake.api_base, sleep=lambda s: None)
    paths = []
    for n, name in enumerate(("girl 01_LIVE_READY.mp4", "man_001_LIVE_READY.mp4")):
        p = tmp_path / name
        p.write_bytes(f"clip-{n}".encode() * 3000)
        paths.append(p)
    media = [(paths[0], report(paths[0], duration=2716.0)), (paths[1], report(paths[1], duration=2693.0))]
    return remote, client, api, media


def window(root, env, *, configured=True):
    from app.youtube_live_schedule_ui import LiveScheduleWindow
    remote, client, api, media = env
    clock = FakeClock(datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc).timestamp())
    return LiveScheduleWindow(root, api_factory=lambda: api, connected=lambda: True, clock=clock,
                              playlist_source=lambda: list(media), tools=lambda: (Path("ffmpeg"), Path("ffprobe")),
                              cloud_factory=lambda: client, cloud_configured=lambda: configured,
                              analyze=lambda paths, ffprobe: [report(p) for p in paths])


def test_beginner_quick_cloud_schedule_shows_pc_off_card(root, env, fake):
    remote, client, api, media = env
    w = window(root, env)
    try:
        assert w.execution.get() == "cloud" and "현재 LIVE Playlist 사용" in w.media_text.get()
        assert "총 2개 / 총 길이 01:30:09" in w.media_text.get() and "girl 01_LIVE_READY.mp4" in w.media_text.get()
        assert "DIRECT COPY 가능" in w.media_text.get()
        w.beginner_quick()
        assert w.repeat.get() == "한 번만" and w.duration.get() == 120 and w.privacy.get() == PRIVACY_LABELS["unlisted"]
        assert not w.advanced.get() and "PC 꺼도 됨" in w.btn_create.cget("text")
        w.start_date.set("2026-10-10")
        w.start_time.set("19:00")
        w.create()
        assert pump(root, lambda: w.card.winfo_ismapped(), timeout=60), w.message.get()
        card = w.card_text.get()
        for text in ("✓ LIVE 예약 준비 완료", "2026-10-10 19:00", PRIVACY_LABELS["unlisted"], "방송 2시간", "Playlist 2개",
                     "무료 Cloud", "✓ 영상 Cloud 저장 완료", "✓ YouTube 예약 완료", "✓ 자동 시작 등록 완료", "PC를 종료해도 됩니다."):
            assert text in card, card
        assert all(v.get().startswith("✓") for v in w.step_vars.values())
        (row,) = w.tree.get_children()
        assert w.tree.item(row, "values")[4] == "✓ 자동 시작 준비"
        assert fake.broadcasts[row]["body"]["contentDetails"]["enableAutoStart"] is True
        assert FAKE_STREAM_NAME not in w.message.get() + card
        assert len(list(remote.jobs_dir.glob("*.json"))) == 1 and remote.scheduler_enabled
    finally:
        w.destroy()


def test_partial_failure_card_retry_and_cancel(root, env, fake, monkeypatch):
    import app.live_ui as live_ui
    remote, client, api, media = env
    remote.fail_add = True
    w = window(root, env)
    try:
        w.beginner_quick()
        w.create()
        assert pump(root, lambda: w.card.winfo_ismapped(), timeout=60)
        card = w.card_text.get()
        assert "⚠ YouTube 예약은 만들어졌지만" in card and "Cloud 자동 시작 준비에 실패했습니다." in card
        assert "PC를 끄지 마세요" in card and "PC를 종료해도" not in card
        assert w.btn_card_retry.winfo_ismapped() and len(fake.broadcasts) == 1
        remote.fail_add = False
        w._retry_card()
        assert pump(root, lambda: not w.busy and "Cloud 자동 시작 준비 완료" in w.message.get(), timeout=30), w.message.get()
        (row,) = w.tree.get_children()
        assert w.tree.item(row, "values")[4] == "✓ 자동 시작 준비"
        monkeypatch.setattr(live_ui, "ask_choice", lambda *a, **k: "cloud")
        w.tree.selection_set(row)
        w.cancel_selected()
        assert pump(root, lambda: not w.busy and "Cloud 자동 시작을 취소" in w.message.get(), timeout=30), w.message.get()
        assert w.tree.item(row, "values")[4] == "취소됨" and row in fake.broadcasts
    finally:
        w.destroy()


def test_cloud_unreachable_no_youtube_and_youtube_only_mode(root, env, fake):
    remote, client, api, media = env
    remote.reachable = False
    w = window(root, env)
    try:
        w.beginner_quick()
        w.create()
        assert pump(root, lambda: w.card.winfo_ismapped(), timeout=60)
        assert "✗ 예약 준비 실패 — Cloud 연결" in w.card_text.get() and "YouTube 예약은 만들지 않았습니다." in w.card_text.get()
        assert fake.broadcasts == {}
    finally:
        w.destroy()
    w2 = window(root, env, configured=False)
    try:
        assert w2.execution.get() == "youtube"
        w2.toggle_advanced(True)
        assert w2.adv_frame.winfo_manager() == "grid"
        w2.create()  # 기존 YouTube 예약만 (DAILY 7개, autoStart 없음)
        assert pump(root, lambda: "예약 7개" in w2.message.get(), timeout=30), w2.message.get()
        assert all(b["body"]["contentDetails"]["enableAutoStart"] is False for b in fake.broadcasts.values())
        assert all(v[4] == "-" for v in (w2.tree.item(i, "values") for i in w2.tree.get_children()))
    finally:
        w2.destroy()
