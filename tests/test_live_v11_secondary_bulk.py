"""Beginner 2CH UX v1.1 — 두 번째 송출 채널 선택 · 채널 A 이름 · Playlist 일괄 LIVE READY (가짜만, 실제 FFmpeg/YouTube/OCI 없음)."""
import io
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cloud_model import DEFAULT_LIVE_PROFILE, profile_service
from app.live_channels import LiveChannelStore, key_store_for
from app.live_ready import LiveReadyCancelled, make_live_ready_file
from app.live_ready_batch import (
    KIND_CONVERT, KIND_MATCH, KIND_OK, OK_TEXT, default_output_path, find_reusable_output, plan_bulk_conversion,
    plan_items, plan_target_spec, run_bulk_live_ready, summary_lines,
)
from test_live_beginner_ux import Spy, make, root, shown, shown_boxes  # noqa: F401 (fixture 재사용)
from test_live_ready_batch import rep

SEC_KEY = "live_secondary_card_profile_id"
CHANNELS = (("oldpop", "oldpoplounge"), ("tokyo", "도쿄칠"))


def pick_secondary(card, pid):
    card.cmb_secondary.current(card._secondary_ids.index(pid))
    card._on_secondary()


# ---------------- 두 번째 송출 채널 ----------------

def test_cards_show_a_plus_selected_secondary_and_persist(root, shown_boxes):
    from app.settings import load_settings
    w, store, ctls = make(root, channels=CHANNELS, rename_default="시니어 채널")
    try:
        w._tick()
        a, b = w.channel_cards
        assert b.pid == "oldpop" and b._secondary_ids == ["oldpop", "tokyo"]  # 처음: 첫 추가 채널
        assert list(b.cmb_secondary.cget("values")) == ["oldpoplounge", "도쿄칠"]  # 내부 ID가 아니라 실제 이름
        pick_secondary(b, "tokyo")
        w._tick()
        assert (a.pid, b.pid) == (DEFAULT_LIVE_PROFILE, "tokyo") and b.secondary_var.get() == "도쿄칠"
        assert a.title.get() == "시니어 채널 (채널 A)" and b.title.get() == "두 번째 송출 채널"
        assert load_settings()[SEC_KEY] == "tokyo"  # 프로필 ID만 저장
        assert w.btn_quick_a.cget("text") == "▶ 시니어 채널만 시작"
        assert w.btn_quick_b.cget("text") == "▶ 도쿄칠만 시작"
        assert w.btn_quick_both.cget("text") == "▶▶ 시니어 채널 + 도쿄칠 동시 시작"
        assert store.get("oldpop") is not None  # 카드 표시만 바뀜: oldpoplounge 채널은 그대로
    finally:
        w.destroy()
    w2, _, _ = make(root, channels=())  # 다시 실행 (같은 설정)
    try:
        w2._tick()
        assert w2.channel_cards[1].pid == "tokyo" and w2.secondary_profile_id() == "tokyo"
    finally:
        w2.destroy()


def test_deleted_secondary_falls_back_safely(root, shown_boxes):
    from app.settings import update_settings
    LiveChannelStore().add("oldpoplounge", profile_id="oldpop")
    LiveChannelStore().add("도쿄칠", profile_id="tokyo")
    update_settings(**{SEC_KEY: "tokyo"})
    LiveChannelStore().delete("tokyo")
    w, store, _ = make(root, channels=())
    try:
        w._tick()
        assert w.channel_cards[1].pid == "oldpop"  # 지워진 채널 → 첫 추가 채널
        store.delete("oldpop")
        w._refresh_channel_list()
        w._tick()
        b = w.channel_cards[1]
        assert b.pid is None and shown(b.btn_create) and not shown(b.sec_row)  # → '선택 안 됨' 자리
        assert str(w.btn_quick_both.cget("state")) == "disabled"
        update_settings(**{SEC_KEY: "default"})  # 기본 채널 ID가 저장돼 있어도 두 번째 카드로 쓰지 않음
        assert w.secondary_profile_id() is None and not w.set_secondary("default")
    finally:
        w.destroy()


def test_changing_secondary_while_a_live_sends_nothing(root, shown_boxes):
    w, store, ctls = make(root, channels=CHANNELS)
    try:
        key_store_for("oldpop").set("oldpop-key-not-real")
        ctls["default"].go_live()
        w._tick()
        b = w.channel_cards[1]
        pick_secondary(b, "tokyo")
        w._tick()
        assert ctls["default"].calls == [] and w.channel_is_live(DEFAULT_LIVE_PROFILE)  # A에 아무 명령 없음
        assert all(c.calls == [] for c in ctls.values())
        assert key_store_for("oldpop").get() == "oldpop-key-not-real" and store.get("oldpop") is not None
        assert w.channel_cards[0].state.get().startswith("● 송출중")
    finally:
        w.destroy()


def test_quick_start_and_main_links_use_selected_pair(root, shown_boxes):
    from app.ui import MainWindow
    w, store, ctls = make(root, channels=CHANNELS, rename_default="시니어 채널")
    try:
        w._tick()
        pick_secondary(w.channel_cards[1], "tokyo")
        wiz = w.open_quick_start(["A", "B"])
        assert wiz.targets == [DEFAULT_LIVE_PROFILE, "tokyo"]  # A + 고른 채널 (첫 추가 채널 아님)
        wiz.destroy()
        wiz = w.open_quick_start(["B"])
        assert wiz.targets == ["tokyo"] and w.channel_id == "tokyo"
        wiz.destroy()
        assert MainWindow._quick_live_text("A") == "▶ 시니어 채널 시작"
        assert MainWindow._quick_live_text("B") == "▶ 도쿄칠 시작"
        fake = SimpleNamespace(_open_live=lambda: None, _live_window=lambda: w)
        wiz = MainWindow._live_quick(fake, ["A", "B"])
        assert wiz.targets == [DEFAULT_LIVE_PROFILE, "tokyo"]
        wiz.destroy()
    finally:
        w.destroy()


def test_default_channel_rename_keeps_backend_identity(root, shown_boxes):
    w, store, ctls = make(root, channels=CHANNELS)
    try:
        w._tick()
        a = w.channel_cards[0]
        assert a.title.get() == "채널 A (기본)" and any("[이름 바꾸기]" in x for x in a.lines.get().splitlines())
        ctls["default"].go_live()  # LIVE 중에도 이름만 바꿀 수 있다
        ctl_before, store_before = w.clouds[DEFAULT_LIVE_PROFILE], w._default_store
        assert w.rename_channel(DEFAULT_LIVE_PROFILE, "시니어 채널")
        w._tick()
        prof = store.get(DEFAULT_LIVE_PROFILE)
        assert prof.channel_profile_id == DEFAULT_LIVE_PROFILE and prof.display_name == "시니어 채널"
        assert prof.key_store_id == DEFAULT_LIVE_PROFILE and profile_service(DEFAULT_LIVE_PROFILE) == "long-live.service"
        assert w.clouds[DEFAULT_LIVE_PROFILE] is ctl_before and w._default_store is store_before
        assert ctls["default"].calls == [] and w.channel_is_live(DEFAULT_LIVE_PROFILE)
        assert a.title.get() == "시니어 채널 (채널 A)" and not any("[이름 바꾸기]" in x for x in a.lines.get().splitlines())
        assert [p.channel_profile_id for p in store.all()] == ["default", "oldpop", "tokyo"]  # 새 profile 없음
    finally:
        w.destroy()


# ---------------- Playlist 변환 계획 / 표시 ----------------

def five():
    return [rep("01.mp4"), rep("02.mp4"), rep("03.mp4", keyframe=5), rep("04.mp4", fps=25.0), rep("05.mp4")]


def test_plan_three_ready_two_problems():
    reports = five()
    assert plan_bulk_conversion(reports) == [2, 3]  # 이미 준비된 3개는 절대 포함하지 않음
    plans = plan_items(reports)
    assert [p.kind for p in plans] == [KIND_OK, KIND_OK, KIND_CONVERT, KIND_MATCH, KIND_OK]
    assert plans[0].status == OK_TEXT == "✓ LIVE READY · 변환 안 함"
    assert plans[2].status == "⚠ Keyframe 간격 5초 · 변환 필요"
    assert plans[3].status == "⚠ LIVE READY · FPS 불일치 · 규격 맞춤 필요"
    assert plans[3].reason.startswith("다른 영상은 30fps인데 이 영상은 25fps라") and "DIRECT COPY" in plans[3].reason
    assert plan_items([rep("a"), None])[1].status == "○ 분석 중"
    assert plan_target_spec(reports) == (30.0, "h264", "aac", 44100, 2)
    # 파일 이름이 *_LIVE_READY.mp4 이어도 분석 결과가 문제면 변환 대상, 이름과 상관없이 판단
    assert plan_bulk_conversion([rep("x_LIVE_READY.mp4", keyframe=5), rep("y.mp4")]) == [0]


def test_playlist_table_counts_and_selected_protection(root, shown_boxes, monkeypatch):
    import app.live_ui as live_ui
    w, store, ctls = make(root, channels=())
    called = []
    monkeypatch.setattr(w, "_pl_make_ready", lambda idx: called.append(list(idx)) or True)
    try:
        w.source_mode.set("playlist")
        w._on_source_mode()
        for r in five():
            w.playlist.add(r.path)
            w.playlist.set_report(r.path, r)
        w._refresh_playlist()
        rows = [w.ptree.item(x)["values"] for x in w.ptree.get_children()]
        assert [row[5] for row in rows] == [OK_TEXT, OK_TEXT, "⚠ Keyframe 간격 5초 · 변환 필요",
                                            "⚠ LIVE READY · FPS 불일치 · 규격 맞춤 필요", OK_TEXT]
        assert w.pl_counts.get() == "총 5개 · ✓ 그대로 사용 3개 · ⚠ 변환 필요 2개"
        assert w.btn_pl_fix_all.cget("text") == "문제 영상 2개 모두 자동 변환"
        assert "LIVE READY이지만 Playlist 규격 맞춤 필요" in w.playlist_summary.get()
        # 이미 LIVE READY + 호환 → 변환 호출 0, 쉬운 안내
        w.ptree.selection_set(w.ptree.get_children()[0])
        w._pl_make_ready_selected()
        assert called == [] and ("showinfo", "LIVE READY", live_ui.ALREADY_READY_TEXT) in shown_boxes
        # LIVE READY지만 FPS 불일치 → 변환 대상
        w.ptree.selection_set(w.ptree.get_children()[3])
        w._pl_make_ready_selected()
        assert called == [[3]]
        w._pl_make_ready_all()
        assert called[-1] == [2, 3]  # 문제 영상 2개만
    finally:
        w.destroy()


def test_bulk_ui_one_ffmpeg_at_a_time_and_progress_text(root, shown_boxes, monkeypatch, tmp_path):
    import app.live_ui as live_ui
    from app.tooling import FFMPEG_GUARD
    w, store, ctls = make(root, channels=())
    gate, seen = threading.Event(), []

    def fake_bulk(**kw):
        seen.append([str(p.name) for _, p, _ in kw["items"]])
        kw["progress"](1, 2, "03.mp4", 0.63, "변환 중")
        gate.wait(10)
        return [], True
    monkeypatch.setattr(live_ui, "run_bulk_live_ready", fake_bulk)
    w._tools = lambda: (Path("ffmpeg"), Path("ffprobe"))
    try:
        w.source_mode.set("playlist")
        w._on_source_mode()
        for r in five():
            w.playlist.add(tmp_path / r.path.name)
            w.playlist.set_report(tmp_path / r.path.name, r)
        assert w._pl_make_ready([2, 3])
        assert not w._pl_make_ready([2, 3])  # 변환 중에는 두 번째 FFmpeg를 시작하지 않음
        assert FFMPEG_GUARD.owner == "convert" and not FFMPEG_GUARD.try_acquire("other")
        end = time.monotonic() + 5
        while time.monotonic() < end and "현재: 03.mp4 · 63%" not in w.pl_progress.get():
            root.update()
            time.sleep(0.02)
        text = w.pl_progress.get()
        assert "LIVE READY 자동 변환 1 / 2" in text and "한 파일이 끝나면 다음 파일이 자동으로 시작됩니다" in text
        assert "남음: 04.mp4" in text and "완료: -" in text
        w._ui_q.put(("bulk_item", "✓ 03.mp4"))
        w._drain_ui()
        assert w._bulk_done == ["✓ 03.mp4"]
    finally:
        gate.set()
        w._convert_thread.join(10)
        w.destroy()
    assert FFMPEG_GUARD.owner is None and seen == [["03.mp4", "04.mp4"]]


# ---------------- 순차 변환 · 실패 · 취소 · 재사용 ----------------

def make_sources(tmp_path, n=3):
    srcs = []
    for i in range(n):
        s = tmp_path / f"{i + 1:02d}.mp4"
        s.write_bytes(f"original-{i}".encode())
        srcs.append(s)
    return srcs


def test_bulk_sequential_failure_continues_and_cancel_stops(tmp_path):
    srcs = make_sources(tmp_path)
    active, order, peak = [0], [], [0]
    lock = threading.Lock()

    def convert(**kw):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        order.append(kw["src"].name)
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        if kw["src"].name == "02.mp4":
            raise RuntimeError("변환 실패")
        out = kw["src"].with_name(kw["src"].stem + "_LIVE_READY.mp4")
        out.write_bytes(b"ready")
        return out
    done = []
    items = [(i, s, rep(s.name, keyframe=5)) for i, s in enumerate(srcs)]
    results, cancelled = run_bulk_live_ready(ffmpeg=Path("f"), ffprobe=Path("p"), items=items, cancel=threading.Event(),
                                             convert=convert, on_item=lambda r: done.append((r.source.name, r.ok)))
    assert order == ["01.mp4", "02.mp4", "03.mp4"] and peak[0] == 1  # 1→2→3, 동시 실행 최대 1
    assert done == [("01.mp4", True), ("02.mp4", False), ("03.mp4", True)] and not cancelled  # 실패 뒤에도 계속
    cancel, started = threading.Event(), []

    def convert_cancel(**kw):
        started.append(kw["src"].name)
        if len(started) == 2:
            cancel.set()
            raise LiveReadyCancelled("사용자가 변환을 중지했습니다.")
        out = kw["src"].with_name(kw["src"].stem + "_X.mp4")
        out.write_bytes(b"ready")
        return out
    results, cancelled = run_bulk_live_ready(ffmpeg=Path("f"), ffprobe=Path("p"), items=items, cancel=cancel,
                                             convert=convert_cancel)
    assert cancelled and started == ["01.mp4", "02.mp4"] and len(results) == 1  # 다음 항목은 시작하지 않음
    assert [s.read_bytes() for s in srcs] == [f"original-{i}".encode() for i in range(3)]  # 원본 그대로


def test_part_file_removed_on_failure_and_source_kept(tmp_path):
    tmp_path = tmp_path / "videos"  # conftest의 _settings 폴더와 분리
    tmp_path.mkdir()
    src = tmp_path / "song.mp4"
    src.write_bytes(b"original")

    class Proc:
        def __init__(self, cmd):
            Path(cmd[-1]).write_bytes(b"half")  # .part.mp4 를 만든 뒤 실패
            self.stdout, self.stderr, self.returncode = io.StringIO("out_time_us=1000000\n"), io.StringIO("boom"), 1

        def wait(self, *a):
            return 1

        def poll(self):
            return 1

        def kill(self):
            pass
    with pytest.raises(RuntimeError):
        make_live_ready_file(ffmpeg=Path("ffmpeg"), ffprobe=Path("ffprobe"), src=src, height=1080, duration=10.0,
                             cancel=threading.Event(), popen=lambda cmd, **k: Proc(cmd))
    assert not list(tmp_path.glob("*.part.mp4")) and src.read_bytes() == b"original"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["song.mp4"]


def test_existing_valid_output_reused_invalid_not_reused(tmp_path):
    src = tmp_path / "song.mp4"
    src.write_bytes(b"original")
    out = default_output_path(src)
    assert out.name == "song_LIVE_READY.mp4"
    out.write_bytes(b"converted-before")
    target = (30.0, "h264", "aac", 44100, 2)
    source_rep = rep("song.mp4", keyframe=5)
    good = rep("song_LIVE_READY.mp4")
    assert find_reusable_output(src, source_rep, target, lambda p: good) == out
    assert find_reusable_output(src, source_rep, target, lambda p: rep("x", keyframe=5)) is None  # LIVE READY 아님
    assert find_reusable_output(src, source_rep, target, lambda p: rep("x", fps=25.0)) is None  # 규격 불일치
    short = rep("x")
    short.duration = 100.0
    assert find_reusable_output(src, source_rep, target, lambda p: short) is None  # 길이가 다름 (불완전)

    def boom(p):
        raise OSError("ffprobe 실패")
    assert find_reusable_output(src, source_rep, target, boom) is None  # 불확실 → 새로 변환
    old = time.time() - 3600
    os.utime(out, (old, old))  # 변환본이 원본보다 오래됨 (원본이 나중에 바뀜)
    assert find_reusable_output(src, source_rep, target, lambda p: good) is None
    os.utime(out, None)
    assert find_reusable_output(tmp_path / "none.mp4", source_rep, target, lambda p: good) is None
    calls = []
    results, _ = run_bulk_live_ready(ffmpeg=Path("f"), ffprobe=Path("p"), items=[(0, src, source_rep)],
                                     cancel=threading.Event(), convert=lambda **kw: calls.append(kw),
                                     reuse=lambda s, r: find_reusable_output(s, r, target, lambda p: good))
    assert calls == [] and results[0].reused and results[0].output == out  # FFmpeg 실행 없이 재사용
    assert summary_lines(results, False)[0] == "↺ song.mp4 → song_LIVE_READY.mp4 (기존 변환본 재사용)"
    assert out.read_bytes() == b"converted-before" and src.read_bytes() == b"original"  # 덮어쓰기 없음
