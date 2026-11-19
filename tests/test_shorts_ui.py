"""⑧ 숏츠 만들기 — 메인 카드·창·FFmpeg 잠금·설정 (가짜 숏츠 엔진, 창 생성 가능 환경에서만)."""
import time
from pathlib import Path

import pytest

from app.settings import load_settings, update_settings
from app.tooling import FFMPEG_GUARD
from shorts_fakes import configure, make_engine


@pytest.fixture(scope="session")
def fake_engine(tmp_path_factory):
    return make_engine(tmp_path_factory.mktemp("fake_shorts_engine_ui"))


@pytest.fixture
def engine(fake_engine):
    configure(fake_engine)
    yield fake_engine
    configure(fake_engine)


@pytest.fixture
def tkroot():
    import tkinter as tk
    try:
        r = tk.Tk()
    except tk.TclError as e:  # pragma: no cover
        pytest.skip(f"no display: {e}")
    r.withdraw()
    yield r
    r.destroy()


@pytest.fixture
def main_app(tmp_path):
    import app.ui as ui
    try:
        a = ui.MainWindow(tmp_path)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no display: {e}")
    yield a
    try:
        a._finish_close()
    except Exception:
        pass


def _pump(w, cond, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        w.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


def _media(tmp_path, n=2):
    songs = []
    for k in range(1, n + 1):
        p = tmp_path / "곡" / f"{k:02d} - 노래 {k}.wav"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"RIFF")
        songs.append(p)
    bg = tmp_path / "배경" / "bg.mp4"
    bg.parent.mkdir(parents=True, exist_ok=True)
    bg.write_bytes(b"x")
    return songs, bg


def _window(master, engine, tmp_path, **kw):
    from app.shorts_ui import ShortsMakerWindow
    w = ShortsMakerWindow(master, **kw)
    w.quiet_errors = True
    w.engine_dir.set(str(engine))
    w.outdir.set(str(tmp_path / "out"))
    return w


# ---------------- 메인 화면 ----------------

def test_main_card_opens_single_window(main_app):
    a = main_app
    assert {"stock", "playlist", "shorts"} <= set(a.mode_cards) and "shorts" in a.card_help_links
    card = a.mode_cards["shorts"]
    info = card.grid_info()
    assert int(info["row"]) == 0 and int(info["column"]) == 1 and card.master is a._row78
    assert int(a._row78.grid_info()["row"]) == 3
    assert not a._row78.grid_slaves(column=0)  # ⑦ 자리는 비어 있음
    a.update()
    widths = {k: a.mode_cards[k].winfo_width() for k in ("stock", "playlist", "shorts")}
    assert abs(widths["shorts"] - widths["playlist"]) <= 2 and abs(widths["stock"] - widths["playlist"]) <= 2, widths
    card.event_generate("<Button-1>", x=5, y=5)
    a.update()
    w = a._alive(getattr(a, "shorts_win", None))
    assert w is not None and w.title() == "⑧ 숏츠 만들기"
    card.event_generate("<Button-1>", x=5, y=5)
    a.update()
    assert a._open_shorts() is w
    from app.shorts_ui import ShortsMakerWindow
    assert sum(isinstance(c, ShortsMakerWindow) for c in a.winfo_children()) == 1


def test_send_to_shorts_adds_once(main_app, tmp_path):
    songs, _ = _media(tmp_path, 1)
    a = main_app
    assert a.send_to_shorts(str(songs[0]), "AI 노래 제목") is True
    w = a.shorts_win
    assert len(w.songs) == 1 and w.songs[0].title == "AI 노래 제목"
    assert a.send_to_shorts(songs[0]) is True  # 같은 경로: 중복 무시, 여전히 1개
    assert len(w.songs) == 1
    assert a.send_to_shorts(tmp_path / "없는 곡.wav") is False and len(w.songs) == 1
    assert not w.running  # 창만 열리고 제작은 시작하지 않음


def test_upload_handoff_fills_video_path(main_app, engine, tmp_path, monkeypatch):
    """결과 → [③ 예약 업로드로 보내기] → 예약 업로드 창 set_video(경로, 제목). 실제 업로드는 하지 않는다."""
    a = main_app
    seen = {}
    monkeypatch.setattr(a, "_open_upload", lambda video_path="", title="": seen.update(path=video_path, title=title) or "up")
    songs, bg = _media(tmp_path, 1)
    a.send_to_shorts(songs[0])
    w = a.shorts_win
    w.quiet_errors = True
    w.engine_dir.set(str(engine))
    w.outdir.set(str(tmp_path / "out"))
    w.add_background(bg)
    assert w.start()
    assert _pump(w, lambda: not w.running, 30), w.status.get()
    w.result_tree.selection_set(w.result_tree.get_children()[0])
    w.update()
    assert w.send_selected_to_upload() == "up"
    assert Path(seen["path"]) == tmp_path / "out" / "01 - 노래 1_short.mp4" and seen["title"] == "01 - 노래 1_short"


# ---------------- 창 ----------------

def test_full_flow_with_fake_engine(tkroot, engine, tmp_path):
    configure(engine, fail=[2])
    songs, bg = _media(tmp_path, 3)
    w = _window(tkroot, engine, tmp_path, send_to_upload=lambda p: p)
    try:
        for s in songs:
            assert w.add_audio(s)
        assert w.add_background(bg) and w.add_background(bg) and len(w.bgs) == 1
        w.song_tree.selection_set(w.song_tree.get_children()[2])
        w._move("songs", -1)  # 3번 곡을 2번 자리로
        order = [s.path for s in w.songs]
        assert order == [songs[0], songs[2], songs[1]]
        assert w.start() and w.running and str(w.btn_start.cget("state")) == "disabled"
        assert FFMPEG_GUARD.owner == "⑧ 숏츠 만들기"
        assert not w.start()  # 제작 중 두 번 시작 안 됨
        assert _pump(w, lambda: not w.running, 30), w.status.get()
        assert FFMPEG_GUARD.owner is None  # 끝나면 잠금 해제
        assert [r["status"] for r in w.results] == ["ok", "failed", "ok"]
        assert w.results[1]["message"] == "fake failure message 2"  # 엔진 message 그대로
        rows = [w.result_tree.item(i)["values"] for i in w.result_tree.get_children()]
        assert rows[1][1] == "실패" and rows[1][2] == "fake failure message 2" and rows[0][1] == "완료"
        assert "일부 실패" in w.status.get() and w.last_exit["code"] == 6
        assert float(w.bar["value"]) >= 99.0
        logs = w.log.get(0, "end")
        assert any("plain text line from engine" in x for x in logs)
        import json
        job = json.loads((engine / "last_job.json").read_text(encoding="utf-8"))
        assert job["audio_files"] == [str(p) for p in order]  # 창에 보이는 순서 그대로
        assert (job["duration"], job["fit_mode"], job["assign"], job["use_nvenc"], job["name_template"]) == \
            (45, "blur", "random", True, "{stem}_short")
        w.result_tree.selection_set(w.result_tree.get_children()[1])  # 실패 항목: 보내기 불가
        w.update()
        assert str(w.btn_upload.cget("state")) == "disabled" and w.send_selected_to_upload() is None
        w.result_tree.selection_set(w.result_tree.get_children()[0])
        w.update()
        assert str(w.btn_upload.cget("state")) == "normal"
        assert w.send_selected_to_upload() == Path(w.results[0]["output"])
    finally:
        w.destroy()


def test_stop_cancels_and_releases_guard(tkroot, engine, tmp_path):
    configure(engine, mode="slow")
    songs, bg = _media(tmp_path, 3)
    w = _window(tkroot, engine, tmp_path)
    try:
        w.add_audio(songs[0]); w.add_background(bg)
        assert w.start()
        assert _pump(w, lambda: w.now_text.get() == "1/1 · 01 - 노래 1", 20), w.now_text.get()
        t0 = time.monotonic()
        w.stop()
        assert _pump(w, lambda: not w.running, 12), w.status.get()
        assert time.monotonic() - t0 < 10
        assert w.last_exit["code"] == 5 and "중지했습니다" in w.status.get()
        assert FFMPEG_GUARD.owner is None
        assert w.add_audio(songs[1]) and len(w.songs) == 2  # 끝난 뒤 다시 추가 가능
    finally:
        w.destroy()


def test_add_audio_while_running_does_not_autostart(tkroot, engine, tmp_path):
    configure(engine, mode="slow")
    songs, bg = _media(tmp_path, 2)
    w = _window(tkroot, engine, tmp_path)
    try:
        w.add_audio(songs[0]); w.add_background(bg)
        assert w.start()
        assert _pump(w, lambda: w.total == 1 and "1/1" in w.now_text.get(), 20)
        assert w.add_audio(songs[1]) and len(w.songs) == 2
        assert len(w.run_items) == 1  # 지금 제작에는 들어가지 않음
        w.stop()
        assert _pump(w, lambda: not w.running, 12)
        assert len((engine / "ran.txt").read_text(encoding="utf-8").split()) == 1  # 자동으로 다시 시작하지 않음
    finally:
        w.destroy()


def test_guard_busy_blocks_start(tkroot, engine, tmp_path, monkeypatch):
    import app.shorts_ui as U
    shown = []
    monkeypatch.setattr(U.messagebox, "showwarning", lambda title, msg, **kw: shown.append((title, msg)))
    songs, bg = _media(tmp_path, 1)
    w = _window(tkroot, engine, tmp_path)
    w.quiet_errors = False
    assert FFMPEG_GUARD.try_acquire("long")
    try:
        w.add_audio(songs[0]); w.add_background(bg)
        assert w.start() is False and not w.running
        w.update()
        time.sleep(0.5)
        w.update()
        assert shown and shown[0][0] == "FFmpeg 사용 중" and "장시간 영상 제작" in shown[0][1]
        assert not (engine / "ran.txt").exists() and w.run is None  # 가짜 엔진은 실행되지 않음
        assert FFMPEG_GUARD.owner == "long"  # 다른 작업의 잠금은 그대로
    finally:
        FFMPEG_GUARD.release("long")
        w.destroy()


def test_connection_check_text(tkroot, engine, tmp_path):
    w = _window(tkroot, engine, tmp_path)
    try:
        w.check_engine()
        assert _pump(w, lambda: "연결됨" in w.engine_text.get(), 20)
        assert w.engine_text.get() == "엔진 v0.8.0 연결됨"
        configure(engine, version="0.7.1")
        w.check_engine()
        assert _pump(w, lambda: "업데이트" in w.engine_text.get(), 20)
        w.engine_dir.set(str(tmp_path / "엔진 없음"))
        w.check_engine()
        assert _pump(w, lambda: "폴더가 없습니다" in w.engine_text.get(), 20)
    finally:
        w.destroy()


def test_settings_saved_and_restored_only_shorts_key(tkroot, engine, tmp_path):
    update_settings(playlist_maker={"quality": "4K"}, prevent_sleep=False, queue=[{"x": 1}])
    before = {k: v for k, v in load_settings().items()}
    w = _window(tkroot, engine, tmp_path)
    try:
        assert (w.duration.get(), w.fit_mode.get(), w.assign.get(), w.use_nvenc.get(), w.name_template.get()) == \
            (45, "blur", "random", True, "{stem}_short")
        w.duration.set(30); w.fit_mode.set("crop"); w.assign.set("rotate"); w.use_nvenc.set(False)
        w.name_template.set("{index}_{stem}")
        w._save_settings()
    finally:
        w.destroy()
    after = load_settings()
    assert {k: v for k, v in after.items() if k != "shorts_maker"} == before
    sm = after["shorts_maker"]
    assert (sm["duration"], sm["fit_mode"], sm["assign"], sm["use_nvenc"], sm["name_template"]) == \
        (30, "crop", "rotate", False, "{index}_{stem}")
    assert sm["output_dir"] == str(tmp_path / "out") and sm["engine_dir"] == str(engine)
    from app.shorts_ui import ShortsMakerWindow
    w2 = ShortsMakerWindow(tkroot)
    try:
        assert (w2.duration.get(), w2.fit_mode.get(), w2.assign.get(), w2.use_nvenc.get(), w2.name_template.get(),
                w2.outdir.get(), w2.engine_dir.get()) == \
            (30, "crop", "rotate", False, "{index}_{stem}", str(tmp_path / "out"), str(engine))
    finally:
        w2.destroy()


def test_close_while_running_asks_and_cancels(tkroot, engine, tmp_path, monkeypatch):
    import app.shorts_ui as U
    configure(engine, mode="slow")
    asked = []
    answer = {"v": False}
    monkeypatch.setattr(U.messagebox, "askyesno", lambda t, m, **kw: asked.append(m) or answer["v"])
    songs, bg = _media(tmp_path, 1)
    w = _window(tkroot, engine, tmp_path)
    w.add_audio(songs[0]); w.add_background(bg)
    assert w.start()
    assert _pump(w, lambda: w.run is not None and w.run.proc is not None, 20)
    w._on_close()  # 아니오 → 그대로
    assert asked == ["숏츠 제작을 중지하고 닫을까요?"] and w.winfo_exists() and w.running
    answer["v"] = True
    run = w.run
    w._on_close()
    assert not w.winfo_exists()
    assert run.wait(12) and run.proc.poll() == 5
    assert _pump(tkroot, lambda: FFMPEG_GUARD.owner is None, 5)


def test_main_close_asks_when_shorts_running(main_app, engine, tmp_path, monkeypatch):
    import app.ui as ui
    configure(engine, mode="slow")
    a = main_app
    songs, bg = _media(tmp_path, 1)
    a.send_to_shorts(songs[0])
    w = a.shorts_win
    w.quiet_errors = True
    w.engine_dir.set(str(engine)); w.outdir.set(str(tmp_path / "out")); w.add_background(bg)
    assert w.start()
    asked = []
    monkeypatch.setattr(ui.messagebox, "askyesno", lambda t, m, **kw: asked.append(m) or False)
    a._close_after_live()
    assert asked == ["⑧ 숏츠 제작 중 — 중단하고 종료할까요?"] and a.winfo_exists() and w.running
    run = w.run
    assert _pump(w, lambda: run.proc is not None, 20)
    a._finish_close()  # 종료: ⑧ 창도 닫고 엔진을 취소
    assert run.wait(12) and run.proc.poll() == 5
