"""④ 내 영상으로 배경 만들기 — 계획/분석 단위 테스트 + 실제 FFmpeg로 만든 시험 MP4 통합 테스트."""
import hashlib
import shutil
import subprocess
import time
from pathlib import Path
from threading import Event

import pytest

from app import bg_analysis as A
from app import bg_core as B
from app.core import BuildCancelled, BuildError, VideoInfo, probe_video

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")


def vi(name="a.mp4", dur=30.0, w=1920, h=1080, fps=30.0):
    return VideoInfo(Path(name).resolve(), dur, 1000, w, h, fps, "h264", "aac", "yuv420p", "High", 48000, 2)


# ---------------- 출력 규격 ----------------

def test_output_spec_defaults_to_fhd_h264():
    s = B.choose_output_spec([vi(w=3840, h=2160), vi("b.mp4")])
    assert (s.width, s.height, s.codec, s.fps) == (1920, 1080, "h264", 30.0)


def test_output_spec_never_upscales_needlessly():
    s = B.choose_output_spec([vi(w=1920, h=1080)], B.QUALITY_4K)
    assert s.height == 1080 and any("확대하지 않고" in n for n in s.notes)
    s = B.choose_output_spec([vi(w=1280, h=720)], B.QUALITY_FHD)
    assert (s.width, s.height) == (1280, 720)


def test_output_spec_4k_uses_hevc_when_available():
    s = B.choose_output_spec([vi(w=3840, h=2160)], B.QUALITY_4K, hevc_available=True)
    assert (s.height, s.codec, s.encoder) == (2160, "hevc", "libx265")
    s = B.choose_output_spec([vi(w=3840, h=2160)], B.QUALITY_4K, hevc_available=False)
    assert s.codec == "h264"


def test_output_spec_fps_majority_and_notes():
    s = B.choose_output_spec([vi(dur=60, fps=25), vi("b.mp4", dur=20, fps=60), vi("c.mp4", w=1080, h=1920)])
    assert s.fps == 25.0
    assert any("FPS" in n for n in s.notes) and any("16:9" in n for n in s.notes)
    assert B.fps_expr(29.97) == "30000/1001" and B.fps_expr(30.0) == "30"


# ---------------- 계획 ----------------

def test_plan_cycle_math_and_wraparound():
    spec = B.choose_output_spec([vi()])
    clips = [B.Clip(vi("a.mp4", 30)), B.Clip(vi("b.mp4", 20)), B.Clip(vi("c.mp4", 10))]
    p = B.build_bg_plan(clips, 600, 1.0, spec)
    assert len(p.pieces) == 6 and [x.kind for x in p.pieces] == ["body", "xfade"] * 3
    assert p.cycle_duration == pytest.approx(60 - 3 * 1.0)
    assert p.repeats == 11
    last = p.pieces[-1]  # 마지막 장면 → 첫 장면 머리로 디졸브 (cycle 이음새)
    assert last.a.path.name == "c.mp4" and last.b.path.name == "a.mp4" and last.b.start == 0
    assert p.pieces[0].a.start == pytest.approx(1.0)  # 첫 본문은 머리(디졸브 구간) 뒤부터


def test_plan_uses_ranges_skips_disabled_and_rejects_short():
    spec = B.choose_output_spec([vi()])
    clips = [B.Clip(vi("a.mp4", 30), start=5, end=25), B.Clip(vi("b.mp4", 20), enabled=False)]
    p = B.build_bg_plan(clips, 180, 1.0, spec)
    assert p.clip_count == 1 and p.cycle_duration == pytest.approx(19.0)
    assert p.pieces[0].a.start == pytest.approx(6.0)
    assert any("영상 1개" in w for w in p.warnings)
    with pytest.raises(ValueError, match="너무 짧"):
        B.build_bg_plan([B.Clip(vi("x.mp4", 2.0))], 180, 1.0, spec)
    with pytest.raises(ValueError):
        B.build_bg_plan([B.Clip(vi(), enabled=False)], 180, 1.0, spec)


def test_piece_command_reencodes_only_new_path_without_audio():
    spec = B.choose_output_spec([vi()])
    p = B.build_bg_plan([B.Clip(vi("a.mp4")), B.Clip(vi("b.mp4"))], 180, 1.0, spec)
    cmd = B.piece_command(Path("ffmpeg"), p.pieces[1], spec, Path("o.mp4"))
    s = " ".join(cmd)
    assert "xfade=transition=fade" in s and "-an" in cmd and "copy" not in cmd
    assert cmd.index("-ss") < cmd.index("-i")  # 입력 앞 정확 탐색
    assert "settb=AVTB" in s and "setsar=1" in s and "format=yuv420p" in s


def test_core_lossless_path_untouched():
    import inspect
    from app import core
    src = inspect.getsource(core.run_concat_copy)
    assert '"-c", "copy"' in src and "libx264" not in src


def test_busy_message_names_bg_owner():
    from app.live_supervisor import busy_message
    assert "배경" in busy_message("bg")


# ---------------- 분석 (작은 격자 숫자만) ----------------

def _an(grids, sf=4.0, camera=A.CAMERA_STATIC):
    return A.ClipAnalysis(path=Path("x.mp4"), duration=len(grids) / sf, sample_fps=sf, grids=grids, camera=camera)


def test_loop_good_when_start_and_end_match():
    g = [bytes([100] * 144)] * 80
    adv = A.recommend_loop(_an(g), 1.0)
    assert adv.verdict == A.VERDICT_GOOD and adv.start == 0 and adv.end == pytest.approx(20.0)


def test_loop_bad_on_brightness_ramp_and_moving_camera_never_good():
    ramp = [bytes([min(255, 40 + i * 2)] * 144) for i in range(80)]
    assert A.recommend_loop(_an(ramp), 1.0).verdict == A.VERDICT_BAD
    flat = [bytes([100] * 144)] * 80
    assert A.recommend_loop(_an(flat, camera=A.CAMERA_MOVING), 1.0).verdict != A.VERDICT_GOOD


def test_loop_recommends_trim_when_tail_differs():
    g = [bytes([100] * 144)] * 70 + [bytes([200] * 144)] * 10  # 마지막 2.5초만 다름
    adv = A.recommend_loop(_an(g), 1.0)
    assert adv.end < 18 and adv.score < adv.full_score and adv.verdict == A.VERDICT_GOOD


def test_camera_classification():
    assert A.classify_camera(1.0, 2.0) == A.CAMERA_STATIC
    assert A.classify_camera(5.0, 10.0) == A.CAMERA_GENTLE
    assert A.classify_camera(15.0, 60.0) == A.CAMERA_MOVING


def test_duplicates_exact_and_similar():
    a = A.ClipAnalysis(Path("a.mp4"), 30, 4, [], sha256="x", dhash=(1, 2, 3), mean_rgb=(10, 10, 10))
    b = A.ClipAnalysis(Path("b.mp4"), 30, 4, [], sha256="x", dhash=(1, 2, 3), mean_rgb=(10, 10, 10))
    c = A.ClipAnalysis(Path("c.mp4"), 28, 4, [], sha256="y", dhash=(1, 2, 7), mean_rgb=(12, 10, 10))
    d = A.ClipAnalysis(Path("d.mp4"), 28, 4, [], sha256="z", dhash=(2**63 - 1, 2**62, 2**61), mean_rgb=(200, 0, 0))
    kinds = {(f.a.name, f.b.name): f.kind for f in A.find_duplicates([a, b, c, d])}
    assert kinds[("a.mp4", "b.mp4")] == "동일 파일"
    assert kinds[("a.mp4", "c.mp4")] == "매우 유사"
    assert not any("d.mp4" in k for k in kinds)


def test_quality_warnings_heuristics():
    an = A.ClipAnalysis(Path("a.mp4"), 30, 4, [], brightness=20, contrast=2, motion=40, black_seconds=3, sharpness=5)
    w = " ".join(A.quality_warnings(an))
    assert "어둡" in w and "검은 화면" in w and "움직임" in w
    ok = A.ClipAnalysis(Path("a.mp4"), 30, 4, [], brightness=120, contrast=40, motion=3, sharpness=400)
    assert A.quality_warnings(ok) == []


# ---------------- 실제 FFmpeg ----------------

def make_clip(path: Path, src: str, dur: float, audio=True):
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", src]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}"]
    cmd += ["-t", str(dur), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-shortest"] if audio else []
    subprocess.run(cmd + [str(path)], check=True)
    return path


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def pixel(path: Path, t: float):
    raw = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1",
                          "-vf", "scale=1:1:flags=area,format=rgb24", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    return tuple(raw[:3])


def leftovers(folder: Path):
    return [p.name for p in folder.iterdir() if p.name.startswith(B.TEMP_PREFIX) or p.name.endswith(".part.mp4")]


@pytest.fixture
def clips_dir(tmp_path):
    d = tmp_path / "한글 경로" / "원본 영상"
    d.mkdir(parents=True)
    return d


@needs_ffmpeg
def test_real_mixed_resolution_fps_dissolve_and_length(clips_dir, tmp_path):
    red = make_clip(clips_dir / "빨강_640x360_30.mp4", "color=c=red:s=640x360:r=30", 6)
    blue = make_clip(clips_dir / "파랑_480x360_25.mp4", "color=c=blue:s=480x360:r=25", 5)
    green = make_clip(clips_dir / "초록_854x480_60.mp4", "color=c=green:s=854x480:r=60", 4, audio=False)
    before = {p: sha(p) for p in (red, blue, green)}
    infos = [probe_video(p, Path(FFPROBE)) for p in (red, blue, green)]
    spec = B.choose_output_spec(infos)
    assert spec.height == 480  # 가장 큰 원본(854×480) 이상으로 키우지 않음 (FHD 선택이어도)
    plan = B.build_bg_plan([B.Clip(i) for i in infos], 40, 1.0, spec)
    assert plan.cycle_duration == pytest.approx(15 - 3)
    out = tmp_path / "출력" / "배경.mp4"
    seen = []
    r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out,
                               cancel_event=Event(), progress_cb=lambda f, t: seen.append(f))
    assert r.output == out and out.exists()
    assert abs(r.duration - 40) < 0.2 and (r.width, r.height) == (spec.width, 480)
    streams = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(out)],
                             capture_output=True, text=True).stdout.split()
    assert streams == ["video"]  # 원본 오디오 제거
    assert seen and seen[-1] == 1.0 and all(a <= b + 1e-9 for a, b in zip(seen, seen[1:]))
    # 장면 순서: 빨강 본문(0~4) → 디졸브(4~5) → 파랑 본문(5~8) → 디졸브 → 초록 → 디졸브(11~12, 초록→빨강)
    assert pixel(out, 2.0)[0] > 200 and pixel(out, 6.5)[2] > 200
    mid = pixel(out, 4.5)
    assert 60 < mid[0] < 200 and 60 < mid[2] < 200  # 빨강+파랑이 섞인 디졸브 프레임
    wrap = pixel(out, 11.5)
    assert wrap[0] > 50 and wrap[1] > 40  # 초록→빨강 이음새도 디졸브
    assert pixel(out, 13.0)[0] > 200  # 두 번째 바퀴 빨강
    assert {p: sha(p) for p in (red, blue, green)} == before  # 원본 그대로
    assert leftovers(out.parent) == []


@needs_ffmpeg
def test_real_three_minutes_single_clip(clips_dir, tmp_path):
    src = make_clip(clips_dir / "고정.mp4", "testsrc2=s=320x180:r=30", 30)
    info = probe_video(src, Path(FFPROBE))
    spec = B.choose_output_spec([info])
    plan = B.build_bg_plan([B.Clip(info)], 180, 1.0, spec)
    out = tmp_path / "3분.mp4"
    r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out, cancel_event=Event())
    assert abs(r.duration - 180) < 0.3 and r.repeats == 7 and r.cycle_duration == pytest.approx(29.0, abs=0.05)
    assert r.size == out.stat().st_size > 0


@needs_ffmpeg
def test_real_cancel_cleans_up(clips_dir, tmp_path):
    a = make_clip(clips_dir / "a.mp4", "testsrc2=s=640x360:r=30", 8)
    b = make_clip(clips_dir / "b.mp4", "mandelbrot=s=640x360:r=30", 8)
    infos = [probe_video(p, Path(FFPROBE)) for p in (a, b)]
    plan = B.build_bg_plan([B.Clip(i) for i in infos], 60, 1.0, B.choose_output_spec(infos))
    out_dir = tmp_path / "out"
    cancel = Event()

    def prog(frac, text):
        if frac > 0.05:
            cancel.set()
    with pytest.raises(BuildCancelled):
        B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out_dir / "x.mp4",
                               cancel_event=cancel, progress_cb=prog)
    assert not (out_dir / "x.mp4").exists() and leftovers(out_dir) == []


@needs_ffmpeg
def test_real_disk_shortage_and_overwrite_protection(clips_dir, tmp_path, monkeypatch):
    a = make_clip(clips_dir / "a.mp4", "testsrc2=s=320x180:r=30", 6)
    info = probe_video(a, Path(FFPROBE))
    plan = B.build_bg_plan([B.Clip(info)], 60, 1.0, B.choose_output_spec([info]))
    with pytest.raises(BuildError, match="원본"):
        B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=a,
                               cancel_event=Event())
    monkeypatch.setattr(B, "enough_disk_space", lambda folder, size: (False, 10 * 1024**2, size))
    out = tmp_path / "o" / "x.mp4"
    with pytest.raises(BuildError, match="저장 공간 부족"):
        B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out, cancel_event=Event())
    assert not out.exists() and leftovers(out.parent) == []
    assert sha(a)  # 원본 존재


@needs_ffmpeg
def test_real_broken_mp4_rejected(clips_dir):
    good = make_clip(clips_dir / "good.mp4", "testsrc2=s=320x180:r=30", 4)
    bad = clips_dir / "손상.mp4"
    bad.write_bytes(good.read_bytes()[:2000])
    with pytest.raises(BuildError):
        probe_video(bad, Path(FFPROBE))
    junk = clips_dir / "가짜.mp4"
    junk.write_bytes(b"not a video" * 100)
    with pytest.raises(BuildError):
        probe_video(junk, Path(FFPROBE))


@needs_ffmpeg
def test_real_analysis_loop_duplicates_quality(clips_dir):
    ff = Path(FFMPEG)
    static = make_clip(clips_dir / "고정.mp4",
                       "smptehdbars=s=640x360:r=30[bg];color=c=white:s=60x60:r=30[b];"
                       "[bg][b]overlay=x='300+150*sin(t*2*PI/6)':y=150", 12, audio=False)
    pan = make_clip(clips_dir / "이동.mp4", "testsrc=s=1920x360:r=30,crop=640:360:x='t*90':y=0", 12, audio=False)
    copy = clips_dir / "고정_복사.mp4"
    shutil.copyfile(static, copy)
    reenc = clips_dir / "고정_재인코딩.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(static), "-vf", "scale=480:270", "-c:v", "libx264",
                    "-crf", "32", str(reenc)], check=True)
    dark = make_clip(clips_dir / "어두움.mp4", "color=c=0x0a0a0a:s=320x180:r=30", 6, audio=False)
    ans = {p.name: A.analyze_clip(ff, p, probe_video(p, Path(FFPROBE)).duration) for p in (static, pan, copy, reenc, dark)}
    a_static = A.recommend_loop(ans["고정.mp4"], 1.0)
    a_pan = A.recommend_loop(ans["이동.mp4"], 1.0)
    assert ans["고정.mp4"].camera != A.CAMERA_MOVING and a_static.verdict == A.VERDICT_GOOD
    assert ans["이동.mp4"].camera == A.CAMERA_MOVING and a_pan.verdict == A.VERDICT_BAD
    kinds = {frozenset((f.a.name, f.b.name)): f.kind for f in A.find_duplicates(list(ans.values()))}
    assert kinds[frozenset(("고정.mp4", "고정_복사.mp4"))] == "동일 파일"
    assert kinds[frozenset(("고정.mp4", "고정_재인코딩.mp4"))] == "매우 유사"
    assert frozenset(("고정.mp4", "이동.mp4")) not in kinds
    assert any("어둡" in w for w in ans["어두움.mp4"].quality_warnings)
    assert A.thumbnail_png(ff, static, 12).startswith(b"\x89PNG")


# ---------------- 화면 (실제 FFmpeg) ----------------

def pump(root, cond, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.03)
    return cond()


@pytest.fixture
def tkroot():
    tk = pytest.importorskip("tkinter")
    from app.ui_theme import ensure
    try:
        r = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    r.withdraw()
    ensure(r)
    yield r
    r.destroy()


@pytest.fixture
def quiet(monkeypatch):
    from tkinter import messagebox
    shown = []
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(messagebox, name, lambda *a, **k: shown.append(a))
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    return shown


@needs_ffmpeg
def test_window_load_analyze_build_and_send(tkroot, clips_dir, tmp_path, quiet):
    from app.bg_ui import BackgroundMakerWindow
    a = make_clip(clips_dir / "숲.mp4", "testsrc2=s=320x180:r=30", 8)
    b = make_clip(clips_dir / "바다.mp4", "mandelbrot=s=320x180:r=25", 8)
    sent = []
    w = BackgroundMakerWindow(tkroot, tools=lambda: (FFMPEG, FFPROBE), send_to_long=sent.append)
    try:
        w.withdraw()
        assert w.add_paths([a, b])
        assert pump(w, lambda: not w.loading and len(w.analyses) == 2)
        assert len(w.tree.get_children()) == 2 and len(w.thumbs) == 2
        assert all(w.tree.set(i, "loop") in (A.VERDICT_GOOD, A.VERDICT_CHECK, A.VERDICT_BAD) for i in w.tree.get_children())
        w.tree.selection_set("1")
        w._move(-1)
        assert w.clips[0].path.name == "바다.mp4"
        w.minutes.set(3)
        w.outdir.set(str(tmp_path / "BG"))
        w._options_changed()
        assert w.plan is not None and w.plan.target == 180 and "360" in w.plan_text.get()
        assert w.start(confirm=False)
        assert pump(w, lambda: not w.running, timeout=240)
        assert w.last_output is not None and w.last_output.exists(), quiet
        info = probe_video(w.last_output, Path(FFPROBE))
        assert abs(info.duration - 180) < 0.3
        assert str(w.btn_send.cget("state")) == "normal"
        w._send_to_long()
        assert sent == [w.last_output]
    finally:
        w.destroy()


@needs_ffmpeg
def test_window_respects_ffmpeg_guard_and_rejects_broken(tkroot, clips_dir, quiet):
    from app.bg_ui import BackgroundMakerWindow
    from app.tooling import FFMPEG_GUARD
    a = make_clip(clips_dir / "a.mp4", "testsrc2=s=320x180:r=30", 6)
    broken = clips_dir / "깨짐.mp4"
    broken.write_bytes(b"\x00" * 5000)
    w = BackgroundMakerWindow(tkroot, tools=lambda: (FFMPEG, FFPROBE))
    try:
        w.withdraw()
        w.add_paths([a, broken])
        assert pump(w, lambda: not w.loading and len(w.analyses) == 1)
        assert [c.path.name for c in w.clips] == ["a.mp4"] and any("깨짐.mp4" in str(x) for x in quiet)
        assert FFMPEG_GUARD.try_acquire("long")
        try:
            assert not w.start(confirm=False) and not w.running
            assert any("장시간" in str(x) for x in quiet)
        finally:
            FFMPEG_GUARD.release("long")
    finally:
        w.destroy()


def test_main_window_opens_bg_and_receives_output(tmp_path, monkeypatch, quiet):
    import app.ui as ui
    monkeypatch.setattr(ui, "discover_ffmpeg", lambda root: None)
    try:
        app = ui.MainWindow(tmp_path)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no display: {e}")
    app.withdraw()
    try:
        assert app.mode_cards["bg"].bind("<Button-1>")
        w = app._open_bg()
        assert w.winfo_exists() and app._open_bg() is w
        added = []
        monkeypatch.setattr(app, "add_set_paths", lambda paths: added.extend(paths))
        app._receive_from_bg(tmp_path / "x.mp4")
        assert added == [str(tmp_path / "x.mp4")]
    finally:
        app._finish_close()
