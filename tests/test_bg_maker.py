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


def test_output_spec_4k_uses_hevc_only_when_chosen():
    s = B.choose_output_spec([vi(w=3840, h=2160)], B.QUALITY_4K, hevc_available=True)
    assert (s.height, s.codec, s.encoder) == (2160, "h264", "libx264")  # 기본은 LIVE 호환 H.264
    s = B.choose_output_spec([vi(w=3840, h=2160)], B.QUALITY_4K, hevc_available=True, codec=B.CODEC_HEVC)
    assert (s.height, s.codec, s.encoder) == (2160, "hevc", "libx265")
    s = B.choose_output_spec([vi(w=3840, h=2160)], B.QUALITY_4K, hevc_available=False, codec=B.CODEC_HEVC)
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


# ---------------- P0: 용량 예측 · 저장 공간 보호 ----------------

def test_profiles_set_crf_preset_and_size():
    infos = [vi()]
    best = B.choose_output_spec(infos)
    assert (best.crf, best.preset, best.profile) == (18, "fast", B.PROFILE_BEST)  # 기본은 기존과 같음
    small = B.choose_output_spec(infos, profile="small")
    assert (small.crf, small.preset) == (24, "medium") and "CRF 24" in small.label
    assert small.bytes_per_second < best.bytes_per_second
    assert B.choose_output_spec(infos, profile="balanced").bytes_per_second < best.bytes_per_second
    assert B.get_profile("slow").key == B.PROFILE_BEST  # 예전 '느림' 설정값은 고화질로 (C07에서 선택지 정리)
    k4 = B.choose_output_spec([vi(w=3840, h=2160)], B.QUALITY_4K, hevc_available=True, profile="balanced")
    assert (k4.codec, k4.crf) == ("h264", 20)  # 4K도 기본은 H.264 (H.265는 직접 고를 때만)
    k4h = B.choose_output_spec([vi(w=3840, h=2160)], B.QUALITY_4K, hevc_available=True, profile="balanced",
                               codec=B.CODEC_HEVC)
    assert (k4h.codec, k4h.crf) == ("hevc", B.HEVC_BASE_CRF + 1)
    assert B.get_profile("없는값").key == B.PROFILE_BEST
    for p in B.ENCODE_PROFILES:  # 화질 저하 설명이 있어야 함
        assert p.note
    assert "화질 저하" in B.get_profile("small").note
    cmd = B.piece_command(Path("ffmpeg"), B.build_bg_plan([B.Clip(vi())], 60, 1.0, small).pieces[0], small, Path("o.mp4"))
    assert cmd[cmd.index("-crf") + 1] == "24" and cmd[cmd.index("-preset") + 1] == "medium"


def test_size_estimate_is_conservative_for_fhd():
    spec = B.choose_output_spec([vi()])
    assert spec.bytes_per_second * 8 / 1e6 >= 21  # 실측 FHD 약 21~23 Mbps보다 작게 잡지 않는다


def test_required_bytes_is_peak_of_cycle_plus_final():
    spec = B.choose_output_spec([vi()])
    clips = [B.Clip(vi("a.mp4", 30)), B.Clip(vi("b.mp4", 30))]
    p = B.build_bg_plan(clips, 600, 1.0, spec)
    cyc = spec.bytes_per_second * p.cycle_duration
    assert p.work_size == pytest.approx(cyc, rel=1e-6)
    # 반복 연결 중: cycle.mp4 + 마지막 바퀴용으로 남긴 앞쪽 조각(최대 1바퀴) + 최종 파일
    assert p.required_bytes == pytest.approx(2 * cyc + p.expected_size, rel=1e-6)
    short = B.build_bg_plan(clips, 20, 1.0, spec)  # 최종이 한 바퀴보다 짧으면: cycle + 남긴 조각(≤최종) + 최종
    fin = spec.bytes_per_second * 20
    assert short.required_bytes == pytest.approx(max(2 * short.work_size, short.work_size + 2 * fin), rel=1e-6)
    assert B.peak_bytes(10, 100) == 120 and B.peak_bytes(10, 5) == 20 and B.peak_bytes(10, 8) == 26


def _two_clips(clips_dir):
    a = make_clip(clips_dir / "a.mp4", "testsrc2=s=640x360:r=30", 8, audio=False)
    b = make_clip(clips_dir / "b.mp4", "mandelbrot=s=640x360:r=30", 8, audio=False)
    return a, b, [probe_video(p, Path(FFPROBE)) for p in (a, b)]


def _assert_clean(out_dir: Path, srcs, before, keep: Path):
    assert leftovers(out_dir) == [] and [p.name for p in out_dir.iterdir()] == [keep.name]
    assert keep.read_bytes() == b"user file"  # 기존 파일 보존
    assert {p: sha(p) for p in srcs} == before  # 원본 보존


@needs_ffmpeg
def test_real_disk_watch_stops_safely_mid_build(clips_dir, tmp_path, monkeypatch):
    a, b, infos = _two_clips(clips_dir)
    before = {p: sha(p) for p in (a, b)}
    plan = B.build_bg_plan([B.Clip(i) for i in infos], 600, 1.0, B.choose_output_spec(infos))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    keep = out_dir / "keep.mp4"
    keep.write_bytes(b"user file")
    t0 = time.monotonic()
    monkeypatch.setattr(B, "DISK_POLL_SECONDS", 0.1)
    monkeypatch.setattr(B, "enough_disk_space", lambda folder, size: (True, 10 * 1024**4, size))  # PC 실제 잔량과 무관하게
    monkeypatch.setattr(B, "free_bytes", lambda p: 10 * 1024**4 if time.monotonic() - t0 < 1.0 else 100 * 1024**2)
    with pytest.raises(BuildError, match="안전하게 멈췄습니다|저장 공간 부족 예상"):
        B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out_dir / "x.mp4",
                               cancel_event=Event())
    _assert_clean(out_dir, (a, b), before, keep)


@needs_ffmpeg
def test_real_projection_from_measured_bitrate_stops_early(clips_dir, tmp_path, monkeypatch):
    a, b, infos = _two_clips(clips_dir)
    before = {p: sha(p) for p in (a, b)}
    plan = B.build_bg_plan([B.Clip(i) for i in infos], 600, 1.0, B.choose_output_spec(infos))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    keep = out_dir / "keep.mp4"
    keep.write_bytes(b"user file")
    monkeypatch.setattr(B, "DISK_POLL_SECONDS", 1e9)  # 감시 스레드 대신 조각별 재계산이 먼저 잡는지 확인
    monkeypatch.setattr(B, "enough_disk_space", lambda folder, size: (True, 10 * 1024**4, size))  # 시작 전 검사는 통과
    monkeypatch.setattr(B, "free_bytes", lambda p: 300 * 1024**2)
    seen = []
    with pytest.raises(BuildError, match="저장 공간 부족 예상"):
        B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out_dir / "x.mp4",
                               cancel_event=Event(), progress_cb=lambda f, t: seen.append(t))
    assert not any("반복 연결" in t for t in seen)  # 최종 연결 전에 멈춤
    _assert_clean(out_dir, (a, b), before, keep)


@needs_ffmpeg
def test_real_recheck_with_actual_cycle_size(clips_dir, tmp_path, monkeypatch):
    a, b, infos = _two_clips(clips_dir)
    before = {p: sha(p) for p in (a, b)}
    plan = B.build_bg_plan([B.Clip(i) for i in infos], 600, 1.0, B.choose_output_spec(infos))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    keep = out_dir / "keep.mp4"
    keep.write_bytes(b"user file")
    calls = []

    def fake(folder, size):
        calls.append(size)
        return (len(calls) == 1, 5 * 1024**3, size + 1024**3)  # 시작 전 검사는 통과, 한 바퀴 후 재검사는 부족
    monkeypatch.setattr(B, "enough_disk_space", fake)
    with pytest.raises(BuildError, match="한 바퀴 실제 크기"):
        B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out_dir / "x.mp4",
                               cancel_event=Event())
    assert len(calls) == 2 and calls[0] == plan.required_bytes and 0 < calls[1] < plan.expected_size
    _assert_clean(out_dir, (a, b), before, keep)


@needs_ffmpeg
def test_real_profiles_shrink_output(clips_dir, tmp_path):
    src = make_clip(clips_dir / "세밀.mp4", "mandelbrot=s=640x360:r=30", 6, audio=False)
    info = probe_video(src, Path(FFPROBE))
    sizes = {}
    for key in ("best", "small"):
        spec = B.choose_output_spec([info], profile=key)
        r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE),
                                   plan=B.build_bg_plan([B.Clip(info)], 20, 1.0, spec),
                                   output=tmp_path / f"{key}.mp4", cancel_event=Event())
        assert abs(r.duration - 20) < 0.3
        sizes[key] = r.size
    assert sizes["small"] < sizes["best"] * 0.8


# ---------------- P1: 반복 경계 · 페이드 · 미리보기 ----------------

PAT = bytes(range(0, 144))


def test_matched_diff_ignores_fade_but_not_structure():
    faded = bytes(v // 2 for v in PAT)
    assert A.grid_diff(PAT, faded) > 30 and A.matched_diff(PAT, faded) < 1.0
    shifted = PAT[8:] + PAT[:8]
    assert A.matched_diff(PAT, shifted) > 10
    blank = bytes([5] * 144)
    assert A.matched_diff(blank, blank) == 0


def test_fade_grids_not_classified_as_moving():
    fade = [bytes(v * k // 40 for v in PAT) for k in range(1, 41)] + [PAT] * 40  # 10초 페이드 인 + 10초 고정
    st = [A.grid_stats(g) for g in fade]
    drift = A._lag_diff(fade, 16, st)
    motion = A._lag_diff(fade, 2, st)
    assert A.classify_camera(motion, drift) == A.CAMERA_STATIC


def test_loop_motion_mismatch_is_marked_uncertain():
    q = bytes(min(255, max(0, v + (8 if i % 2 else -8))) for i, v in enumerate(PAT))
    grids = [PAT] * 60 + [PAT, q] * 10  # 끝 5초만 화면이 떨림 (움직임 속도 차이), 경계 차이 자체는 작음
    adv = A.recommend_loop(_an(grids), 1.0)
    assert adv.score <= A.GOOD_SCORE and adv.verdict == A.VERDICT_CHECK and adv.uncertain
    assert any("움직임 속도" in r for r in adv.reasons) and any("확인 필요" in r for r in adv.reasons)


def test_loop_structure_difference_detected_when_brightness_equal():
    other = PAT[::-1]  # 평균 밝기는 같고 구도만 다름
    full = A.evaluate_range(_an([PAT] * 40 + [other] * 40), 0, None, 1.0)
    assert full.verdict == A.VERDICT_BAD and full.struct_score > A.CHECK_SCORE
    assert abs(A.grid_stats(PAT).mean - A.grid_stats(other).mean) < 0.01


def test_evaluate_user_range():
    g = [bytes([100] * 144)] * 70 + [bytes([200] * 144)] * 10
    an = _an(g)
    assert A.evaluate_range(an, 0, None, 1.0).verdict == A.VERDICT_BAD
    mine = A.evaluate_range(an, 0, 17.0, 1.0)
    assert mine.verdict == A.VERDICT_GOOD and mine.end == pytest.approx(17.0) and not any("추천 구간" in r for r in mine.reasons)


def test_seam_segments_use_current_range():
    c = B.Clip(vi("a.mp4", 30), start=2, end=20)
    a, b = B.seam_segments(c, c, 1.0)
    assert a.start == pytest.approx(20 - 3.5) and a.length == pytest.approx(3.5)
    assert b.start == pytest.approx(2) and b.length == pytest.approx(3.5)
    cmd = " ".join(B.seam_preview_command(Path("ffmpeg"), a, b, 1.0, Path("p.mp4")))
    assert "xfade=transition=fade:duration=1.000000:offset=2.500000" in cmd and "-an" in cmd
    with pytest.raises(ValueError):
        B.seam_segments(B.Clip(vi("s.mp4", 1.0)), c, 1.0)


@needs_ffmpeg
def test_real_fade_in_static_scene_not_moving(clips_dir):
    p = make_clip(clips_dir / "페이드.mp4", "smptehdbars=s=640x360:r=30,fade=in:st=0:d=6", 12, audio=False)
    an = A.analyze_clip(Path(FFMPEG), p, probe_video(p, Path(FFPROBE)).duration)
    assert an.camera != A.CAMERA_MOVING  # 페이드 인 고정 화면은 이동 촬영이 아님 (이전에는 오판)
    assert A.recommend_loop(an, 1.0).verdict != A.VERDICT_GOOD  # 검은 화면 → 밝은 화면 경계는 여전히 '적합' 아님


@needs_ffmpeg
def test_real_seam_preview(clips_dir, tmp_path):
    red = make_clip(clips_dir / "빨강.mp4", "color=c=red:s=1280x720:r=30", 6, audio=False)
    blue = make_clip(clips_dir / "파랑.mp4", "color=c=blue:s=1280x720:r=25", 6, audio=False)
    ca, cb = (B.Clip(probe_video(p, Path(FFPROBE))) for p in (red, blue))
    out = B.make_seam_preview(Path(FFMPEG), ca, cb, 1.0, tmp_path / "p" / "seam.mp4", Event())
    info = probe_video(out, Path(FFPROBE))
    assert (info.width, info.height) == (640, 360) and abs(info.duration - 6.0) < 0.2
    assert pixel(out, 0.5)[0] > 200 and pixel(out, 5.5)[2] > 200
    m = pixel(out, 3.0)
    assert 60 < m[0] < 200 and 60 < m[2] < 200
    own = B.make_seam_preview(Path(FFMPEG), ca, ca, 1.0, tmp_path / "p" / "own.mp4", Event())
    assert own.exists() and {p: sha(p) for p in (red,)} == {red: sha(red)}


@needs_ffmpeg
def test_window_profile_preview_range_eval(tkroot, clips_dir, tmp_path, quiet, monkeypatch):
    import app.bg_ui as U
    from app.settings import load_settings
    from app.tooling import FFMPEG_GUARD
    opened = []
    monkeypatch.setattr(U, "_open_path", opened.append)
    a = make_clip(clips_dir / "숲.mp4", "testsrc2=s=320x180:r=30", 8)
    b = make_clip(clips_dir / "바다.mp4", "mandelbrot=s=320x180:r=25", 8)
    w = U.BackgroundMakerWindow(tkroot, tools=lambda: (FFMPEG, FFPROBE))
    try:
        w.withdraw()
        w.add_paths([a, b])
        assert pump(w, lambda: not w.loading and len(w.analyses) == 2)
        w.profile.set(B.get_profile("small").label)
        w._options_changed()
        assert w.plan.spec.crf == 24 and "화질 저하" in w.profile_note.get()
        assert load_settings()["bg_maker"]["profile"] == "small"
        assert "작업 중 최대" in w.size_text.get()
        w.tree.selection_set("0")
        w._refresh_buttons()
        assert str(w.btn_preview_loop.cget("state")) == "normal"
        assert w.preview_seam("self")
        assert str(w.btn_start.cget("state")) == "disabled"  # 미리보기 중에는 제작 시작 불가 (FFmpeg 1개)
        assert pump(w, lambda: not w.previewing, timeout=60)
        assert opened and opened[-1].exists() and opened[-1].suffix == ".mp4"
        first = opened[-1]
        assert w.preview_seam("next") and pump(w, lambda: not w.previewing, timeout=60)
        assert len(opened) == 2 and not first.exists()  # 이전 미리보기는 정리
        assert FFMPEG_GUARD.try_acquire("live")
        try:
            assert not w.preview_seam("self")
        finally:
            FFMPEG_GUARD.release("live")
        w.range_start.set("1")
        w.range_end.set("6")
        w._apply_range()
        assert w.tree.set("0", "loop").endswith("(내 구간)") and w.clips[0].path in w.range_eval
        assert "지금 사용 구간" in w.detail.get()
        last = opened[-1]
    finally:
        w.destroy()
    assert not last.exists()  # 창을 닫으면 이 창이 만든 미리보기만 삭제


NATURE_ENV = "PLVM_NATURE_SAMPLES"


def test_real_nature_samples_build(tmp_path):
    """실제 자연풍경 MP4 검증 (라이선스 확인된 샘플 폴더를 환경변수로 줄 때만). 없으면 SKIP — 합성 영상 테스트와 구분."""
    import os
    folder = os.environ.get(NATURE_ENV)
    if not folder or not FFMPEG:
        pytest.skip(f"실제 자연풍경 샘플 없음: {NATURE_ENV} 미설정 (합성 영상 테스트는 위에서 별도로 실행)")
    files = sorted(Path(folder).glob("*.mp4"))[:4]
    if len(files) < 2:
        pytest.skip(f"{NATURE_ENV} 폴더에 MP4가 2개 미만")
    before = {p: sha(p) for p in files}
    infos = [probe_video(p, Path(FFPROBE)) for p in files]
    for p, i in zip(files, infos):
        adv = A.recommend_loop(A.analyze_clip(Path(FFMPEG), p, i.duration), 1.0)
        assert adv.verdict in (A.VERDICT_GOOD, A.VERDICT_CHECK, A.VERDICT_BAD)
    r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE),
                               plan=B.build_bg_plan([B.Clip(i) for i in infos], 180, 1.0, B.choose_output_spec(infos)),
                               output=tmp_path / "nature_3M.mp4", cancel_event=Event())
    assert abs(r.duration - 180) < 1.0 and {p: sha(p) for p in files} == before


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
