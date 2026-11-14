"""④ 음악 길이 직접 입력 — 짧은 원본(10·25·35초) 여러 개를 음악 전체 길이와 같은 무음 배경 MP4로.

단위: 길이 입력 해석(HH:MM:SS · .mmm · :FF), 프레임 변환(목표보다 짧지 않음, 1프레임 미만 오차), 마지막 바퀴 나누기,
      파일명, 12시간 제한, 예상 시간.
실제 FFmpeg: 짧은 합성 영상으로 정확한 프레임 수 · 반복 경계 디졸브 · 원본 오디오 제외 · 전체 디코드 · 중단 · 디스크 부족.
장시간(50분 7초 실제 제작)은 PLVM_LONG_TESTS=1 일 때만 실행 (기본 SKIP).
"""
import hashlib
import math
import os
import shutil
import subprocess
import time
from pathlib import Path
from threading import Event

import pytest

from app import bg_core as B
from app.core import BuildCancelled, BuildError, VideoInfo, probe_video

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")


def vi(name="a.mp4", dur=30.0, w=1920, h=1080, fps=30.0):
    return VideoInfo(Path(name).resolve(), dur, 1000, w, h, fps, "h264", "aac", "yuv420p", "High", 48000, 2)


# ---------------- 길이 입력 ----------------

@pytest.mark.parametrize("text,fps,want", [
    ("00:50:07", None, 3007.0),
    ("50:07", None, 3007.0),
    ("0:50:07", None, 3007.0),
    ("00:59:59", None, 3599.0),
    ("01:00:00", None, 3600.0),
    ("12:00:00", None, 43200.0),
    ("00:50:07.250", None, 3007.25),
    ("00:50:07.5", None, 3007.5),
    ("00:50:07,250", None, 3007.25),
    (" 00:50:07 ", None, 3007.0),
    ("00:50:07:15", 30.0, 3007.5),
    ("00:50:07:00", 30.0, 3007.0),
    ("00:50:07:12", 24.0, 3007.5),
    ("00:50:07:15", 29.97, 3007 + 15 * 1001 / 30000),
])
def test_parse_length_ok(text, fps, want):
    assert B.parse_length(text, fps) == pytest.approx(want, abs=1e-9)


@pytest.mark.parametrize("text,fps,msg", [
    ("", None, "입력"),
    ("abc", None, "형식"),
    ("3007", None, "형식"),
    ("00:60:00", None, "0~59"),
    ("00:00:60", None, "0~59"),
    ("00:50:07.1234", None, "소수 3자리"),
    ("00:50:07:30", 30.0, "0~29"),
    ("00:50:07:15", None, "FPS"),
    ("00:50:07.5:10", 30.0, "소수"),
    ("12:00:01", None, "12시간"),
    ("00:00:00", None, "0보다"),
    ("00::07", None, "형식"),
    ("-1:00:00", None, "숫자"),
])
def test_parse_length_rejects(text, fps, msg):
    with pytest.raises(ValueError, match=msg):
        B.parse_length(text, fps)


def test_target_frames_never_shorter_and_within_one_frame():
    for fps in B.PROJECT_FPS_CHOICES:
        fx = B.exact_fps(fps)
        for t in (0.04, 10.0, 59.999, 3007.0, 3007.25, 3599.0, 3599.999, 3600.0, 43200.0):
            n = B.target_frame_count(t, fps)
            assert n / fx >= t - 1e-9, (fps, t)
            assert n / fx - t < 1 / fx + 1e-9, (fps, t)
    assert B.target_frame_count(3007, 30.0) == 90210  # 정확히 나누어 떨어지면 그대로
    assert B.target_frame_count(3599, 30.0) == 107970
    assert B.target_frame_count(3007, 29.97) == math.ceil(3007 * 30000 / 1001)
    assert B.exact_fps(29.97) == pytest.approx(30000 / 1001) and B.exact_fps(25.0) == 25.0


def test_format_and_tags():
    assert B.format_precise(3007) == "00:50:07.000" and B.format_precise(3007.004) == "00:50:07.004"
    assert B.format_precise(43200) == "12:00:00.000"
    assert B.length_tag(600) == "10M" and B.length_tag(3007) == "50M07S" and B.length_tag(3599) == "59M59S"
    assert B.length_tag(3007.25) == "50M07S250" and B.length_tag(3600) == "1H00M00S"
    clips = [B.Clip(vi("숲.mp4")), B.Clip(vi("바다.mp4"))]
    assert B.default_bg_output_name(clips, 10) == "숲_외1_BG_10M.mp4"  # 기존 버튼 이름 그대로
    assert B.default_bg_output_name(clips, 0, seconds=3007) == "숲_외1_BG_50M07S.mp4"


def test_split_last_cycle():
    frames = [100, 30, 200, 30]  # 한 바퀴 360
    assert B.split_last_cycle(frames, 720) == (2, 0, 0)           # 바퀴 경계에서 정확히 끝
    assert B.split_last_cycle(frames, 360 + 130) == (1, 2, 0)     # 조각 경계에서 끝 → 끝 조각 인코딩 없음
    assert B.split_last_cycle(frames, 360 + 131) == (1, 2, 1)
    assert B.split_last_cycle(frames, 50) == (0, 0, 50)           # 한 바퀴보다 짧음
    assert B.split_last_cycle(frames, 359) == (0, 3, 29)
    for n in range(1, 1200, 7):
        full, k, t = B.split_last_cycle(frames, n)
        assert full * 360 + sum(frames[:k]) + t == n and 0 <= t < (frames[k] if k < len(frames) else 1)


def test_plan_ten_twentyfive_thirtyfive_for_fifty_minutes_seven_seconds():
    infos = [vi("a10.mp4", 10), vi("b25.mp4", 25), vi("c35.mp4", 35)]
    spec = B.choose_output_spec(infos)
    plan = B.build_bg_plan([B.Clip(i) for i in infos], B.parse_length("00:50:07"), 1.0, spec)
    assert plan.cycle_duration == pytest.approx(70 - 3)            # 디졸브 3번 (마지막 → 처음 포함)
    assert plan.repeats == math.ceil(3007 / 67)
    assert plan.target_frames == 3007 * 30
    last = plan.pieces[-1]
    assert last.kind == "xfade" and last.a.path.name == "c35.mp4" and last.b.path.name == "a10.mp4"
    assert plan.expected_size == int(spec.bytes_per_second * 3007)
    with pytest.raises(ValueError, match="12시간"):
        B.build_bg_plan([B.Clip(i) for i in infos], 12 * 3600 + 1, 1.0, spec)
    assert B.build_bg_plan([B.Clip(i) for i in infos], 12 * 3600, 1.0, spec).target_frames == 12 * 3600 * 30


def test_ten_second_clip_allowed_with_longest_dissolve():
    spec = B.choose_output_spec([vi("a.mp4", 10)])
    plan = B.build_bg_plan([B.Clip(vi("a.mp4", 10)), B.Clip(vi("b.mp4", 10))], 3007, 2.0, spec)
    assert plan.cycle_duration == pytest.approx(20 - 4)
    with pytest.raises(ValueError, match="너무 짧"):
        B.build_bg_plan([B.Clip(vi("x.mp4", 4.0))], 3007, 2.0, spec)


def test_tail_command_keeps_full_dissolve_curve():
    spec = B.choose_output_spec([vi()])
    plan = B.build_bg_plan([B.Clip(vi("a.mp4")), B.Clip(vi("b.mp4"))], 600, 1.0, spec)
    xf = plan.pieces[1]
    cmd = B.piece_command(Path("ffmpeg"), xf, spec, Path("t.mp4"), max_frames=7)
    s = " ".join(cmd)
    assert cmd[cmd.index("-frames:v") + 1] == "7"
    assert f"xfade=transition=fade:duration={xf.length:.6f}" in s  # 디졸브 곡선은 원래 길이 그대로, 앞 7프레임만
    assert "-an" in cmd and "copy" not in cmd


def test_estimate_build_seconds():
    infos = [vi("a.mp4", 10), vi("b.mp4", 25), vi("c.mp4", 35)]
    spec = B.choose_output_spec(infos)
    short = B.build_bg_plan([B.Clip(i) for i in infos], 600, 1.0, spec)
    long = B.build_bg_plan([B.Clip(i) for i in infos], 3007, 1.0, spec)
    assert 0 < B.estimate_build_seconds(short) < B.estimate_build_seconds(long)
    assert B.estimate_build_seconds(long, encode_speed=10) < B.estimate_build_seconds(long, encode_speed=0.5)
    hevc = B.choose_output_spec(infos, codec=B.CODEC_HEVC)
    assert B.estimate_build_seconds(B.build_bg_plan([B.Clip(i) for i in infos], 3007, 1.0, hevc)) > \
        B.estimate_build_seconds(long)


# ---------------- 실제 FFmpeg (짧은 합성 영상) ----------------

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


def streams(path: Path):
    return subprocess.run([FFPROBE, "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
                          capture_output=True, text=True).stdout.split()


def full_decode_errors(path: Path) -> str:
    p = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
                       capture_output=True, text=True)
    return (p.stderr or "").strip() + ("" if p.returncode == 0 else f" rc={p.returncode}")


def leftovers(folder: Path):
    return [p.name for p in folder.iterdir()
            if p.name.startswith((B.TEMP_PREFIX, ".PLVM_TEMP_")) or p.name.endswith(".part.mp4")]


@pytest.fixture
def three_clips(tmp_path):
    """10초 빨강 · 25초 파랑 · 35초 초록 (원본 오디오 있음, 29.97 FPS) — 한 바퀴 = 70 - 3 = 67초."""
    d = tmp_path / "원본 자연풍경"
    d.mkdir()
    r = make_clip(d / "빨강_10초.mp4", "color=c=red:s=320x180:r=30000/1001", 10)
    b = make_clip(d / "파랑_25초.mp4", "color=c=blue:s=320x180:r=30000/1001", 25)
    g = make_clip(d / "초록_35초.mp4", "color=c=green:s=320x180:r=30000/1001", 35)
    infos = [probe_video(p, Path(FFPROBE)) for p in (r, b, g)]
    return (r, b, g), infos


@needs_ffmpeg
def test_real_three_clips_exact_length_seam_and_no_audio(three_clips, tmp_path):
    paths, infos = three_clips
    assert all(i.audio_codec for i in infos)  # 원본에는 오디오가 있음
    before = {p: sha(p) for p in paths}
    spec = B.choose_output_spec(infos)
    target = B.parse_length("00:02:07")   # 50분 7초와 같은 구조(전체 바퀴 + 마지막 바퀴 중간)를 짧게
    plan = B.build_bg_plan([B.Clip(i) for i in infos], target, 1.0, spec)
    out = tmp_path / "출력" / B.default_bg_output_name([B.Clip(i) for i in infos], 0, seconds=target)
    r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out, cancel_event=Event())
    fx = B.exact_fps(spec.fps)
    assert out.name.endswith("_BG_2M07S.mp4")
    assert r.frames == r.target_frames == plan.target_frames == math.ceil(127 * fx)
    assert 0 <= r.error_seconds < 1 / fx              # 목표보다 짧지 않음 + 1프레임 미만
    assert r.tail_frames > 0 and r.repeats == 2
    frames, dur = B.probe_frames(Path(FFPROBE), out)
    assert frames == r.target_frames and dur >= target
    assert streams(out) == ["video"]                   # 원본 오디오 0개
    assert full_decode_errors(out) == ""              # 처음부터 끝까지 정상 재생(디코드)
    # 반복 경계: 초록 → 빨강 디졸브 (66~67초), 두 번째 바퀴는 빨강으로 시작, 끝은 초록 장면
    wrap = pixel(out, 66.5)
    assert wrap[0] > 50 and wrap[1] > 40
    assert pixel(out, 68.0)[0] > 200
    assert pixel(out, 126.9)[1] > 100 and pixel(out, 126.9)[0] < 60
    assert {p: sha(p) for p in paths} == before       # 원본 그대로
    assert leftovers(out.parent) == []


@needs_ffmpeg
@pytest.mark.parametrize("text,fps", [("00:00:50.250", None), ("00:00:40:15", 30.0), ("00:01:07", None)])
def test_real_fractional_frame_and_cycle_boundary(three_clips, tmp_path, text, fps):
    _, infos = three_clips
    spec = B.choose_output_spec(infos)
    target = B.parse_length(text, fps)
    plan = B.build_bg_plan([B.Clip(i) for i in infos], target, 1.0, spec)
    out = tmp_path / f"o_{text.replace(':', '_')}.mp4"
    r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out, cancel_event=Event())
    fx = B.exact_fps(spec.fps)
    assert r.frames == r.target_frames and 0 <= r.error_seconds < 1 / fx
    assert streams(out) == ["video"] and full_decode_errors(out) == ""
    assert leftovers(tmp_path) == []


@needs_ffmpeg
def test_real_exact_cycle_multiple_needs_no_tail(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    a = make_clip(d / "a.mp4", "testsrc2=s=320x180:r=30", 10, audio=False)
    b = make_clip(d / "b.mp4", "mandelbrot=s=320x180:r=30", 10, audio=False)
    infos = [probe_video(p, Path(FFPROBE)) for p in (a, b)]
    spec = B.choose_output_spec(infos)
    plan = B.build_bg_plan([B.Clip(i) for i in infos], 36.0, 1.0, spec)  # 한 바퀴 18초 × 2
    assert plan.cycle_duration == pytest.approx(18.0)
    r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=tmp_path / "o.mp4",
                               cancel_event=Event())
    assert r.tail_frames == 0 and r.frames == 36 * 30 and r.repeats == 2
    assert abs(r.error_seconds) < 1e-6


@needs_ffmpeg
def test_real_cancel_during_tail_cleans_up(three_clips, tmp_path):
    paths, infos = three_clips
    spec = B.choose_output_spec(infos)
    plan = B.build_bg_plan([B.Clip(i) for i in infos], 100.0, 1.0, spec)
    out_dir = tmp_path / "out"
    cancel = Event()
    seen = []

    def prog(frac, text):
        seen.append(text)
        if "끝 길이" in text:
            cancel.set()
    with pytest.raises(BuildCancelled):
        B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out_dir / "x.mp4",
                               cancel_event=cancel, progress_cb=prog)
    assert any("끝 길이" in t for t in seen)
    assert not (out_dir / "x.mp4").exists() and leftovers(out_dir) == []
    assert all(p.exists() for p in paths)


@needs_ffmpeg
def test_real_disk_shortage_after_cycle_cleans_kept_pieces(three_clips, tmp_path, monkeypatch):
    _, infos = three_clips
    spec = B.choose_output_spec(infos)
    plan = B.build_bg_plan([B.Clip(i) for i in infos], B.parse_length("00:02:07"), 1.0, spec)
    calls = []
    real = B.enough_disk_space

    def fake(folder, size):
        calls.append(size)
        if len(calls) >= 2:  # 한 바퀴 실제 크기로 다시 검사하는 두 번째 호출에서 부족
            return False, 10 * 1024**2, size
        return real(folder, size)
    monkeypatch.setattr(B, "enough_disk_space", fake)
    out_dir = tmp_path / "out"
    with pytest.raises(BuildError, match="저장 공간 부족"):
        B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out_dir / "x.mp4",
                               cancel_event=Event())
    assert not (out_dir / "x.mp4").exists() and leftovers(out_dir) == []


@needs_ffmpeg
def test_real_short_result_is_rejected(three_clips, tmp_path, monkeypatch):
    """완성 프레임 수가 목표보다 적으면(CapCut 끝 검은 화면) 결과를 남기지 않는다."""
    _, infos = three_clips
    spec = B.choose_output_spec(infos)
    plan = B.build_bg_plan([B.Clip(i) for i in infos], 30.0, 1.0, spec)
    real = B.probe_frames
    out = tmp_path / "o.mp4"

    def fake(ffprobe, path):
        n, d = real(ffprobe, path)
        return (n - 2, d) if Path(path) == out else (n, d)
    monkeypatch.setattr(B, "probe_frames", fake)
    with pytest.raises(BuildError, match="길이 검증 실패"):
        B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out, cancel_event=Event())
    assert not out.exists() and leftovers(tmp_path) == []


# ---------------- 화면 ----------------

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
def test_window_custom_length_validation_and_build(tkroot, tmp_path, quiet):
    from app.bg_ui import CUSTOM, BackgroundMakerWindow
    from app.settings import load_settings
    d = tmp_path / "src"
    d.mkdir()
    a = make_clip(d / "숲_10초.mp4", "testsrc2=s=320x180:r=30", 10)
    b = make_clip(d / "바다_12초.mp4", "mandelbrot=s=320x180:r=30", 12)
    w = BackgroundMakerWindow(tkroot, tools=lambda: (FFMPEG, FFPROBE))
    try:
        w.withdraw()
        assert w.add_paths([a, b])
        assert pump(w, lambda: not w.loading and len(w.analyses) == 2)
        w.outdir.set(str(tmp_path / "BG"))
        # 기존 버튼은 그대로
        w.minutes.set(3)
        w._options_changed()
        assert w.plan is not None and w.plan.target == 180 and w.outname.get().endswith("_BG_3M.mp4")
        # 직접 입력: 타이핑하면 자동으로 '직접 입력' 선택 + 즉시 형식 검사
        w.custom_length.set("00:00:61")
        assert int(w.minutes.get()) == CUSTOM and w.length_check.get().startswith("⚠")
        assert pump(w, lambda: w._length_job is None, timeout=5)
        assert w.plan is None and "음악 길이" in w.plan_text.get()
        assert str(w.btn_start.cget("state")) == "normal"
        assert not w.start(confirm=False)               # 잘못된 길이로는 시작하지 않음
        w.custom_length.set("00:00:45.500")
        assert w.length_check.get().startswith("✓")
        assert pump(w, lambda: w._length_job is None, timeout=5)
        assert w.plan is not None and w.plan.target == pytest.approx(45.5)
        assert "1,365프레임" in w.plan_text.get() and "예상 제작 시간" in w.plan_text.get()
        assert "바퀴" in w.plan_text.get() and w.size_text.get()
        assert w.outname.get().endswith("_BG_0M45S500.mp4")
        saved = load_settings()["bg_maker"]
        assert saved["minutes"] == CUSTOM and saved["custom_length"] == "00:00:45.500" and saved["project_fps"] == 30.0
        assert w.start(confirm=False)
        assert pump(w, lambda: not w.running, timeout=240)
        assert w.last_output is not None and w.last_output.exists(), quiet
        r = w.result
        assert r.frames == r.target_frames == 1365 and "프레임 일치" in w.status.get()
        assert streams(w.last_output) == ["video"]
        # 프레임 표기 + 프로젝트 FPS
        w.project_fps.set("25")
        w.custom_length.set("00:00:45:12")
        assert pump(w, lambda: w._length_job is None, timeout=5)
        assert w.plan is not None and w.plan.target == pytest.approx(45 + 12 / 25)
    finally:
        w.destroy()


def test_window_old_settings_still_load(tkroot, quiet):
    """예전 설정(minutes=10, 새 키 없음)과 새 설정(minutes=0) 모두 그대로 읽힌다."""
    from app.bg_ui import CUSTOM, BackgroundMakerWindow
    from app.settings import update_settings
    update_settings(bg_maker={"minutes": 20, "dissolve": 1.0})
    w = BackgroundMakerWindow(tkroot, tools=lambda: (None, None))
    try:
        w.withdraw()
        assert int(w.minutes.get()) == 20 and w.target_seconds() == 1200
        assert w.custom_length.get() == "00:50:07" and w.project_fps_value() == 30.0
    finally:
        w.destroy()
    update_settings(bg_maker={"minutes": CUSTOM, "custom_length": "00:59:59", "project_fps": 29.97})
    w = BackgroundMakerWindow(tkroot, tools=lambda: (None, None))
    try:
        w.withdraw()
        assert int(w.minutes.get()) == CUSTOM and w.target_seconds() == 3599.0 and w.project_fps_value() == 29.97
    finally:
        w.destroy()


# ---------------- 장시간 (선택) ----------------

@needs_ffmpeg
@pytest.mark.skipif(os.environ.get("PLVM_LONG_TESTS") != "1",
                    reason="장시간 실제 제작(50분 7초)은 PLVM_LONG_TESTS=1 일 때만 실행 — 짧은 합성 영상 테스트로 대신 검증")
def test_real_long_fifty_minutes_seven_seconds(three_clips, tmp_path):
    _, infos = three_clips
    spec = B.choose_output_spec(infos)
    target = B.parse_length("00:50:07")
    plan = B.build_bg_plan([B.Clip(i) for i in infos], target, 1.0, spec)
    r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=tmp_path / "o.mp4",
                               cancel_event=Event())
    assert r.frames == r.target_frames and 0 <= r.error_seconds < 1 / B.exact_fps(spec.fps)
    assert streams(r.output) == ["video"] and full_decode_errors(r.output) == ""
