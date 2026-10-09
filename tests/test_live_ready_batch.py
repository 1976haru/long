"""Playlist 일괄 LIVE READY — 변환 대상 계산, 순차 변환/취소, 원본 보관, Playlist 교체, LIVE 창 버튼 흐름."""
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.live_playlist import LivePlaylist, PlaylistError, validate_playlist
from app.live_ready import LiveReadyCancelled, LiveReadyIssue, LiveReadyReport, analyze_live_ready
from app.live_ready_batch import (
    SOURCE_KEPT, BulkResult, item_ok, output_fps, plan_bulk_conversion, run_bulk_live_ready, summary_lines,
)

FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")
real = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not installed")


def rep(name, *, keyframe=2.0, fps=30.0, sample_rate=44100, height=1080, vfr=False):
    r = LiveReadyReport(path=Path(name), width=height * 16 // 9, height=height, fps=fps, duration=2716.0,
                        video_codec="h264", audio_codec="aac", sample_rate=sample_rate, channels=2)
    if keyframe > 4:
        r.issues.append(LiveReadyIssue("keyframe", f"Keyframe 간격 {keyframe:.0f}초 (4초 이하 필요, 2초 권장)."))
    if vfr:
        r.issues.append(LiveReadyIssue("vfr", "가변 프레임", blocking=False))
    return r


def test_plan_picks_problem_items_and_mismatched_specs():
    girl, man = rep("girl 01.mp4", keyframe=5), rep("man_001.mp4", keyframe=5)
    assert plan_bulk_conversion([girl, man]) == [0, 1]  # 사용자 실제 상황: 둘 다 keyframe 5초
    assert plan_bulk_conversion([rep("a"), rep("b")]) == []
    assert plan_bulk_conversion([rep("a"), rep("b", keyframe=5)]) == [1]
    # 변환본은 44.1kHz → 48kHz 원본과 섞이면 그것도 변환
    assert plan_bulk_conversion([rep("a", sample_rate=48000), rep("b", keyframe=5)]) == [0, 1]
    assert plan_bulk_conversion([rep("a"), rep("b", sample_rate=48000)]) == [1]
    assert plan_bulk_conversion([rep("a"), rep("b", vfr=True)]) == [1] and not item_ok(rep("b", vfr=True))
    assert plan_bulk_conversion([rep("a"), None]) == []  # 분석 중
    assert plan_bulk_conversion([rep("a", fps=29.97), rep("b", keyframe=5)]) in ([0, 1], [1])
    assert output_fps(29.97) == 30 and output_fps(25) == 25 and output_fps(0) == 30 and output_fps(120) == 30


def test_bulk_runner_sequential_progress_failure_and_cancel(tmp_path):
    srcs = [tmp_path / "girl 01.mp4", tmp_path / "man_001.mp4", tmp_path / "c.mp4"]
    for s in srcs:
        s.write_bytes(b"original")
    calls, events = [], []

    def convert(**kw):
        calls.append(kw)
        assert kw["fps"] == 30 and kw["height"] == 1080 and not kw["cancel"].is_set()
        kw["progress_cb"](0.63, "변환 중")
        if kw["src"].name == "c.mp4":
            raise RuntimeError("LIVE READY 변환 실패\nboom")
        out = kw["src"].with_name(kw["src"].stem + "_LIVE_READY.mp4")
        out.write_bytes(b"ready")
        return out
    items = [(i, s, rep(s.name, keyframe=5)) for i, s in enumerate(srcs)]
    results, cancelled = run_bulk_live_ready(ffmpeg=Path("ffmpeg"), ffprobe=Path("ffprobe"), items=items,
                                             cancel=threading.Event(), progress=lambda *a: events.append(a),
                                             convert=convert)
    assert not cancelled and [r.ok for r in results] == [True, True, False] and "boom" not in results[2].error.splitlines()[0]
    assert [c["src"] for c in calls] == srcs  # 순서대로 하나씩
    assert (1, 3, "girl 01.mp4", 0.63, "변환 중") in events and (2, 3, "man_001.mp4", 1.0, "완료") in events
    assert all(s.read_bytes() == b"original" for s in srcs)  # 원본 그대로
    lines = summary_lines(results, cancelled)
    assert lines[0] == "✓ girl 01.mp4 → girl 01_LIVE_READY.mp4" and lines[-1] == SOURCE_KEPT

    cancel = threading.Event()

    def convert_cancel(**kw):
        cancel.set()
        raise LiveReadyCancelled("사용자가 변환을 중지했습니다.")
    results, cancelled = run_bulk_live_ready(ffmpeg=Path("f"), ffprobe=Path("p"), items=items, cancel=cancel,
                                             convert=convert_cancel)
    assert cancelled and results == [] and "중지" in summary_lines(results, cancelled)[0]


def test_playlist_replace_keeps_order_and_rejects_duplicates(tmp_path):
    pl = LivePlaylist()
    a, b = pl.add(tmp_path / "girl 01.mp4"), pl.add(tmp_path / "man_001.mp4")
    pl.set_report(a.path, rep("a"))
    new = pl.replace_path(0, tmp_path / "girl 01_LIVE_READY.mp4")
    assert [i.name for i in pl.items] == ["girl 01_LIVE_READY.mp4", "man_001.mp4"] and new.report is None
    with pytest.raises(PlaylistError):
        pl.replace_path(1, tmp_path / "girl 01_LIVE_READY.mp4")
    with pytest.raises(PlaylistError):
        pl.replace_path(5, tmp_path / "x.mp4")


def make_clip(path: Path, seconds: int, gop: int, freq: int):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc=s=640x360:r=30:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={seconds}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", str(gop), "-keyint_min", str(gop),
                    "-sc_threshold", "0", "-c:a", "aac", "-ac", "2", "-shortest", str(path)], check=True)


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


def pump(root, cond, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.03)
    return cond()


@real
def test_live_window_bulk_converts_5s_keyframe_playlist_and_replaces(root, tmp_path, monkeypatch):
    """실제 사례 재현: 1920x1080 대신 작은 해상도, 30fps, keyframe 5초 영상 2개 → 일괄 변환 → 바꾸기 → DIRECT COPY 가능."""
    from tkinter import messagebox

    import app.live_ui as live_ui
    from app.cloud_client import CloudLiveController
    from app.live_secrets import SessionStreamKeyStore
    shown, asked = [], []
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(messagebox, name, lambda *a, **k: shown.append(a))
    monkeypatch.setattr(live_ui, "ask_choice", lambda parent, title, msg, choices, default, cancel="":
                        (asked.append((title, msg, [c[0] for c in choices])), "replace")[1])
    girl, man = tmp_path / "girl 01.mp4", tmp_path / "man_001.mp4"
    make_clip(girl, 12, 150, 440)
    make_clip(man, 11, 150, 550)
    before = {p: p.read_bytes() for p in (girl, man)}
    w = live_ui.LiveWindow(root, tools=lambda: (Path(FFMPEG), Path(FFPROBE)), key_store=SessionStreamKeyStore(),
                           cloud=CloudLiveController(lambda: None, poll_seconds=3600))
    try:
        w.source_mode.set("playlist")
        w._on_source_mode()
        for p in (girl, man):
            w.playlist.add(p)
            w.playlist.set_report(p, analyze_live_ready(p, Path(FFPROBE)))
        w._refresh_playlist()
        w._sync_widgets()
        root.update()
        assert "Keyframe 간격 5초" in w.playlist_summary.get() and "문제 영상 모두 LIVE READY로 만들기" in w.playlist_summary.get()
        assert w.btn_pl_fix_all.winfo_ismapped() and w.pl_problem_indices() == [0, 1]
        w._pl_make_ready_all()
        assert w.converting
        assert pump(root, lambda: "변환 2 / 2" in w.pl_progress.get() or not w.converting, timeout=120)
        assert pump(root, lambda: not w.converting and asked, timeout=180), shown
        assert asked[0][2] == ["바꾸기 (추천)", "그대로 두기"] and "원본 파일은 그대로 보관됩니다." in asked[0][1]
        assert [i.name for i in w.playlist.items] == ["girl 01_LIVE_READY.mp4", "man_001_LIVE_READY.mp4"]
        assert pump(root, lambda: w.playlist.analyzed, timeout=60)
        v = validate_playlist([i.report for i in w.playlist.items])
        assert v.ok, v.messages
        r0 = w.playlist.items[0].report
        assert (r0.width, r0.height, round(r0.fps)) == (640, 360, 30) and r0.keyframe_max <= 2.05
        assert {p: p.read_bytes() for p in (girl, man)} == before  # 원본 그대로
        assert not list(tmp_path.glob("*.part.mp4"))
        root.update()
        assert "✓ 모두 LIVE READY" in w.playlist_summary.get()
        snap = w.playlist_snapshot()
        assert [p.name for p, _ in snap] == ["girl 01_LIVE_READY.mp4", "man_001_LIVE_READY.mp4"]
    finally:
        w._convert_cancel.set()
        w.destroy()


def test_bulk_result_summary_for_errors():
    r = [BulkResult(0, Path("a.mp4"), None, "LIVE READY 변환 실패\nstderr tail")]
    assert summary_lines(r, False)[0] == "✗ a.mp4: LIVE READY 변환 실패"
