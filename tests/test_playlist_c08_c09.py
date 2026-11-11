"""통합 03 (C08+C09) ⑥ 음악 플레이리스트 — 15곡 · 12초 제목 SRT · 배경 반복 · ①②③ 연결.

모든 음원·영상은 FFmpeg로 만든 '합성 시험 자료'다 (사인파·테스트 패턴). 실제 음악 품질 검증이 아니다.
실제 사용자 음원 시험은 PLVM_PLAYLIST_SAMPLES(폴더) 지정 시에만 실행하고, 없으면 SKIP.
"""
import hashlib
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app import pl_core as P
from app.core import BuildCancelled, BuildError, probe_video

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
need_ff = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="FFmpeg 없음")
JP_TITLES = ["夜明けの窓", "静かな帰り道", "雨のあとで", "春の手紙", "夏の終わり", "月の光", "星降る夜", "朝の珈琲",
             "風の音", "海辺の町", "冬の駅", "白い息", "帰り道", "花びら", "また明日"]


def _ff(*args):
    subprocess.run([FFMPEG, "-hide_banner", "-v", "error", "-y", *map(str, args)], check=True)


def make_tone(path: Path, seconds: float, freq: int = 440):
    path.parent.mkdir(parents=True, exist_ok=True)
    src = ["-f", "lavfi", "-i", f"sine=f={freq}:d={seconds}:sample_rate=44100"]
    if path.suffix == ".mp3":
        _ff(*src, "-c:a", "libmp3lame", "-b:a", "128k", path)
    elif path.suffix == ".m4a":
        _ff(*src, "-c:a", "aac", "-b:a", "128k", path)
    else:
        _ff(*src, path)
    return path


def make_bg(path: Path, seconds: float, size="1920x1080", codec="libx264", fps=30):
    path.parent.mkdir(parents=True, exist_ok=True)
    _ff("-f", "lavfi", "-i", f"testsrc2=s={size}:r={fps}:d={seconds}", "-c:v", codec, "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-g", str(fps), *(["-x265-params", "log-level=error"] if codec == "libx265" else []), path)
    return path


def sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def fake_track(k: int, dur: float, title: str | None = None) -> P.Track:
    return P.Track(Path(f"C:/x/{k:02d}.mp3"), title or f"曲{k}", dur, dur, "mp3", 44100, 2, 1000, f"fp{k}")


# ---------------- 제목 · 타임라인 · SRT (FFmpeg 불필요) ----------------

@pytest.mark.parametrize("name,title", [
    ("01. 夜明けの窓.mp3", "夜明けの窓"), ("02 - 静かな帰り道.wav", "静かな帰り道"), ("03_雨のあとで.m4a", "雨のあとで"),
    ("Track 04 - Morning Coffee.mp3", "Morning Coffee"), ("5 새벽 창가.flac", "새벽 창가"), ("(06) 月の光.mp3", "月の光"),
    ("Spring_Letter.mp3", "Spring Letter"), ("2024 Live.mp3", "2024 Live"), ("07.mp3", "07"),
])
def test_title_from_filename(name, title):
    assert P.title_from_filename(name) == title


def test_timeline_15_tracks_titles_exactly_12_seconds_from_each_start():
    tracks = [fake_track(k, 150.0 + k * 7.25, JP_TITLES[k - 1]) for k in range(1, 16)]
    entries, total = P.build_timeline(tracks)
    assert len(entries) == 15 and abs(total - sum(t.duration for t in tracks)) < 1e-6
    pos = 0.0
    for k, (e, t) in enumerate(zip(entries, tracks), 1):
        assert e.number == k and abs(e.start - pos) < 1e-9 and e.title_start == e.start
        assert abs((e.title_end - e.title_start) - 12.0) < 1e-9
        assert e.label == f"{k:02d}. {JP_TITLES[k - 1]}"
        pos += t.duration
    srt = P.build_srt(entries)
    assert P.validate_srt(srt, entries) == []
    assert srt.startswith("1\r\n00:00:00,000 --> 00:00:12,000\r\n01. 夜明けの窓\r\n")
    assert "\r\n15\r\n" in srt and "15. また明日" in srt
    # 15번째 곡 시작 = 앞 14곡 합
    s15 = sum(t.duration for t in tracks[:14])
    assert f"{P.srt_time(s15)} --> {P.srt_time(s15 + 12)}" in srt


def test_short_track_title_never_reaches_next_song():
    tracks = [fake_track(1, 8.0), fake_track(2, 30.0), fake_track(3, 5.0)]
    entries, total = P.build_timeline(tracks)
    assert entries[0].title_end == pytest.approx(8.0 - P.TITLE_END_GAP)
    assert entries[1].title_end == pytest.approx(8.0 + 12.0)
    assert entries[2].title_end == pytest.approx(total)  # 마지막 곡은 영상 끝까지만
    assert P.validate_srt(P.build_srt(entries), entries) == []


def test_reorder_recalculates_starts_and_srt():
    a, b, c = fake_track(1, 100.0, "A"), fake_track(2, 50.0, "B"), fake_track(3, 70.0, "C")
    e1, _ = P.build_timeline([a, b, c])
    e2, _ = P.build_timeline([c, a, b])
    assert [e.start for e in e1] == [0, 100, 150] and [e.start for e in e2] == [0, 70, 170]
    assert [e.label for e in e2] == ["01. C", "02. A", "03. B"]


def test_crossfade_shifts_starts_and_is_optional():
    tr = [fake_track(k, 60.0) for k in range(1, 4)]
    e0, t0 = P.build_timeline(tr)
    e2, t2 = P.build_timeline(tr, 2.0)
    assert t0 == 180 and t2 == 176 and [e.start for e in e2] == [0, 58, 116]
    with pytest.raises(ValueError):
        P.build_timeline([fake_track(1, 4.0), fake_track(2, 60)], 2.0)


def test_srt_time_format_and_validator_catches_errors():
    assert P.srt_time(0) == "00:00:00,000" and P.srt_time(3723.4567) == "01:02:03,457" and P.srt_time(-1) == "00:00:00,000"
    entries, _ = P.build_timeline([fake_track(1, 60), fake_track(2, 60)])
    good = P.build_srt(entries)
    assert P.validate_srt(good, entries) == []
    assert P.validate_srt(good.replace("00:00:12,000", "00:01:05,000", 1), entries)  # 다음 곡까지 남음
    assert P.validate_srt(good.replace("00:00:12,000", "0:00:12.000", 1))           # 형식 오류
    assert P.validate_srt(good.replace("\r\n2\r\n", "\r\n5\r\n"))                   # 번호
    assert P.validate_srt(good.replace("01. 曲1", "01. 다른 제목"), entries)          # 제목


def test_write_text_new_never_overwrites(tmp_path):
    p = tmp_path / "한글 폴더" / "list.srt"
    p.parent.mkdir()
    a = P.write_text_new(p, "가")
    b = P.write_text_new(p, "나")
    assert a == p and b.name == "list_2.srt" and p.read_text(encoding="utf-8") == "가"
    assert b.read_bytes() == "나".encode("utf-8")  # UTF-8, BOM 없음


def test_duplicate_findings():
    t1 = fake_track(1, 100, "A")
    t2 = P.Track(Path("C:/x/b.mp3"), "B", 90, 90, "mp3", 44100, 2, 1, "fp1")  # 같은 내용
    t3 = fake_track(3, 100.2, "a")                                                  # 같은 제목·길이
    out = P.duplicate_findings([t1, t2, t3])
    assert len(out) == 2 and "같은 음원" in out[0] and "제목·길이" in out[1]


# ---------------- 배경 반복 계획 ----------------

def _info(dur=600.0, w=1920, h=1080, codec="h264", pix="yuv420p", fps=30.0, size=10**8, path="C:/bg.mp4"):
    from app.core import VideoInfo
    return VideoInfo(Path(path), dur, size, w, h, fps, codec, "", pix, "High", 0, 0)


def test_loop_plan_longer_background_cut_at_music_end():
    lp = P.plan_loop(_info(dur=4000), None, 3000)
    assert lp.repeats == 1 and not lp.seams and lp.outpoint is None and lp.natural


def test_loop_plan_uses_bg_cycle_boundary():
    lp = P.plan_loop(_info(dur=610.0), 120.0, 3000)       # ④가 10분 10초로 잘라 만든 배경 (한 바퀴 2분)
    assert lp.unit == pytest.approx(600.0) and lp.outpoint == pytest.approx(600.0)
    assert lp.repeats == 5 and lp.natural and lp.seams[0] == pytest.approx(600.0)
    nat = P.plan_loop(_info(dur=600.0), 120.0, 3000)      # 한 바퀴 배수면 outpoint 없이 파일 전체
    assert nat.outpoint is None and nat.repeats == 5
    raw = P.plan_loop(_info(dur=610.0), None, 3000)
    assert not raw.natural and "갑자기" in raw.note and raw.repeats == 5


def test_plan_copy_vs_encode_and_no_fake_4k():
    tr = [fake_track(k, 200) for k in range(1, 16)]
    p = P.build_playlist_plan(tr, _info())
    assert p.copy_video and p.live_ready_candidate and not any("15곡" in n for n in p.notes)
    assert P.build_playlist_plan(tr, _info(), burn_titles=True).copy_video is False
    hv = P.build_playlist_plan(tr, _info(), codec="hevc")
    assert not hv.copy_video and not hv.live_ready_candidate and any("LIVE" in n for n in hv.notes)
    k4 = P.build_playlist_plan(tr, _info(), quality="4K")
    assert k4.copy_video and k4.spec.height == 1080 and any("4K 원본이 아니" in n for n in k4.notes)
    real4k = P.build_playlist_plan(tr, _info(w=3840, h=2160), quality="4K")
    assert real4k.copy_video and real4k.spec.height == 2160
    down = P.build_playlist_plan(tr, _info(w=3840, h=2160))
    assert not down.copy_video and down.spec.height == 1080
    assert P.build_playlist_plan(tr[:3], _info()).notes[0].startswith("기본 1세트는 15곡")
    assert p.expected_size > 0 and p.required_bytes > p.expected_size


# ---------------- 실제 FFmpeg (합성 자료) ----------------

@need_ff
def test_analyze_mp3_wav_real_duration_and_damage(tmp_path):
    mp3 = make_tone(tmp_path / "음원" / "01. 夜明けの窓.mp3", 14.0)
    wav = make_tone(tmp_path / "음원" / "02 - 静かな帰り道.wav", 13.5, 550)
    a = P.analyze_track(Path(FFMPEG), Path(FFPROBE), mp3)
    b = P.analyze_track(Path(FFMPEG), Path(FFPROBE), wav)
    assert a.title == "夜明けの窓" and abs(a.duration - 14.0) < 0.06 and a.codec == "mp3"
    assert b.title == "静かな帰り道" and abs(b.duration - 13.5) < 0.01 and b.codec.startswith("pcm")
    bad = tmp_path / "음원" / "03 손상.mp3"
    bad.write_bytes(b"ID3" + os.urandom(4000))
    with pytest.raises(BuildError):
        P.analyze_track(Path(FFMPEG), Path(FFPROBE), bad)
    with pytest.raises(BuildError):
        P.analyze_track(Path(FFMPEG), Path(FFPROBE), tmp_path / "없음.mp3")
    trunc = tmp_path / "음원" / "04 잘림.wav"
    trunc.write_bytes(wav.read_bytes()[:30])
    with pytest.raises(BuildError):
        P.analyze_track(Path(FFMPEG), Path(FFPROBE), trunc)


def _tracks(tmp_path, n=15, dur=3.2, sub="한글 음원 폴더"):
    out = []
    for k in range(1, n + 1):
        ext = (".mp3", ".wav", ".m4a")[k % 3]
        p = make_tone(tmp_path / sub / f"{k:02d}. {JP_TITLES[k - 1]}{ext}", dur + (k % 4) * 0.25, 300 + 37 * k)
        out.append(P.analyze_track(Path(FFMPEG), Path(FFPROBE), p))
    return out


@need_ff
def test_build_15_tracks_copy_path_sync_srt_and_originals_untouched(tmp_path):
    tracks = _tracks(tmp_path)
    bg_path = make_bg(tmp_path / "배경 폴더" / "배경.mp4", 10)
    before = {t.path: sha(t.path) for t in tracks} | {bg_path: sha(bg_path)}
    bg = probe_video(bg_path, Path(FFPROBE))
    plan = P.build_playlist_plan(tracks, bg)
    assert plan.copy_video and plan.loop.repeats == 6 and len(plan.entries) == 15
    r = P.run_playlist_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan,
                             output=tmp_path / "출력" / "플레이리스트.mp4", cancel_event=threading.Event())
    assert r.output.exists() and r.copied_video and (r.width, r.height) == (1920, 1080) and r.codec == "h264"
    assert abs(r.audio_duration - plan.total) < 0.1 and abs(r.video_duration - r.audio_duration) < 0.15
    srt = r.srt.read_text(encoding="utf-8")
    assert r.srt.name == "플레이리스트.srt" and P.validate_srt(srt, plan.entries) == []
    assert "01. 夜明けの窓" in srt and "15. また明日" in srt
    assert {p: sha(p) for p in before} == before                             # 원본 그대로
    left = [x.name for x in r.output.parent.iterdir()]
    assert sorted(left) == ["플레이리스트.mp4", "플레이리스트.srt"]               # 임시 파일 없음


@need_ff
def test_srt_exact_12s_and_audio_has_no_gaps(tmp_path):
    tracks = [P.analyze_track(Path(FFMPEG), Path(FFPROBE), make_tone(tmp_path / f"{k:02d} t{k}.wav", 14.0, 400 + k * 100))
              for k in range(1, 4)]
    bg = probe_video(make_bg(tmp_path / "bg.mp4", 50), Path(FFPROBE))
    plan = P.build_playlist_plan(tracks, bg)
    assert plan.loop.repeats == 1  # 배경이 더 길다 → 음악 끝에서 종료
    r = P.run_playlist_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=tmp_path / "o" / "x.mp4",
                             cancel_event=threading.Event())
    assert abs(r.duration - 42.0) < 0.01 and abs(r.audio_duration - 42.0) < 0.1 and abs(r.video_duration - 42.0) < 0.1
    srt = r.srt.read_text(encoding="utf-8")
    for a, z in (("00:00:00,000", "00:00:12,000"), ("00:00:14,000", "00:00:26,000"), ("00:00:28,000", "00:00:40,000")):
        assert f"{a} --> {z}" in srt
    # 곡 사이 무음 구간 없음 (silencedetect: 0.2초 이상 무음이 없어야 함)
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", str(r.output), "-map", "0:a:0", "-af",
                        "silencedetect=n=-50dB:d=0.2", "-f", "null", "-"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert "silence_start" not in p.stderr


@need_ff
def test_burn_titles_hevc_and_4k_downscale(tmp_path):
    tracks = [P.analyze_track(Path(FFMPEG), Path(FFPROBE), make_tone(tmp_path / f"0{k} 曲{k}.mp3", 3.0)) for k in (1, 2)]
    bg4k = probe_video(make_bg(tmp_path / "bg4k.mp4", 2, size="3840x2160", fps=25), Path(FFPROBE))
    burn = P.build_playlist_plan(tracks, bg4k, burn_titles=True, profile="small")
    assert not burn.copy_video and burn.spec.height == 1080
    r = P.run_playlist_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=burn, output=tmp_path / "한글 출력" / "b.mp4",
                             cancel_event=threading.Event())
    assert (r.width, r.height, r.codec) == (1920, 1080, "h264") and abs(r.fps - 25) < 0.01
    hv = P.build_playlist_plan(tracks, bg4k, quality="4K", codec="hevc", profile="small")
    r2 = P.run_playlist_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=hv, output=tmp_path / "o" / "h.mp4",
                              cancel_event=threading.Event(), save_srt=False)
    assert (r2.width, r2.height, r2.codec) == (3840, 2160, "hevc") and r2.srt is None
    assert abs(r2.audio_duration - 6.0) < 0.1


@need_ff
def test_bg_maker_sidecar_gives_natural_cycle_loop(tmp_path):
    """④로 한 바퀴보다 길고 한 바퀴 배수가 아닌 배경을 만든 뒤, ⑥이 한 바퀴 경계(키프레임)에서만 반복하는지."""
    from app import bg_core as B
    clips = []
    for k, c in enumerate(("testsrc2", "smptebars")):
        p = tmp_path / f"c{k}.mp4"
        _ff("-f", "lavfi", "-i", f"{c}=s=640x360:r=30:d=3", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt",
            "yuv420p", p)
        clips.append(B.clip_from_info(probe_video(p, Path(FFPROBE))))
    spec = B.choose_output_spec([c.info for c in clips])
    plan = B.build_bg_plan(clips, 8.0, 0.5, spec)
    r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=tmp_path / "④" / "bg.mp4",
                               cancel_event=threading.Event())
    side = P.sidecar_path(r.output)
    assert side.exists()
    info = probe_video(r.output, Path(FFPROBE))
    cyc = P.read_bg_cycle(r.output, info)
    assert cyc == pytest.approx(r.cycle_duration, abs=1e-3) and info.duration > cyc
    tracks = [P.analyze_track(Path(FFMPEG), Path(FFPROBE), make_tone(tmp_path / f"t{k}.wav", 9.0)) for k in (1, 2)]
    pp = P.build_playlist_plan(tracks, info, cycle=cyc)
    assert pp.loop.natural and pp.loop.outpoint is not None and pp.loop.repeats >= 3
    out = P.run_playlist_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=pp, output=tmp_path / "o" / "pl.mp4",
                               cancel_event=threading.Event())
    assert abs(out.video_duration - 18.0) < 0.15  # 이음 디코드 검사(seam_check)까지 통과
    assert out.natural_loop and out.loop_repeats == pp.loop.repeats
    # 한 바퀴 경계 키프레임의 DTS로 잘라 이음새에 중복·누락 프레임이 없다 (표시 시각 간격이 모두 1프레임)
    fr = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v", "-show_entries", "frame=pts_time", "-of",
                         "csv=p=0", str(out.output)], capture_output=True, text=True).stdout.split()
    pts = [float(x.strip(",")) for x in fr]
    steps = [b - a for a, b in zip(pts, pts[1:]) if b < 17.9]
    assert steps and max(steps) < 1.5 / info.fps and min(steps) > 0.5 / info.fps
    # 정보 파일이 다른 영상과 맞지 않으면 쓰지 않는다
    side.write_text(side.read_text(encoding="utf-8").replace(f'"size": {info.size}', '"size": 1'), encoding="utf-8")
    assert P.read_bg_cycle(r.output, info) is None


@need_ff
def test_disk_shortage_and_cancel_leave_no_temp(tmp_path, monkeypatch):
    tracks = [P.analyze_track(Path(FFMPEG), Path(FFPROBE), make_tone(tmp_path / f"t{k}.wav", 20.0)) for k in (1, 2)]
    bg = probe_video(make_bg(tmp_path / "bg.mp4", 4), Path(FFPROBE))
    plan = P.build_playlist_plan(tracks, bg, burn_titles=True)
    out_dir = tmp_path / "out"
    monkeypatch.setattr(P, "enough_disk_space", lambda d, n: (False, 10, n))
    with pytest.raises(BuildError, match="저장 공간 부족"):
        P.run_playlist_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out_dir / "x.mp4",
                             cancel_event=threading.Event())
    monkeypatch.undo()
    cancel = threading.Event()
    seen = []

    def prog(f, t):
        seen.append(f)
        if f > 0.3:
            cancel.set()
    with pytest.raises(BuildCancelled):
        P.run_playlist_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out_dir / "y.mp4",
                             cancel_event=cancel, progress_cb=prog)
    assert list(out_dir.iterdir()) == []
    with pytest.raises(BuildError, match="이미 존재"):
        (out_dir / "z.mp4").write_bytes(b"keep")
        P.run_playlist_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out_dir / "z.mp4",
                             cancel_event=threading.Event())
    assert (out_dir / "z.mp4").read_bytes() == b"keep"


def test_stale_temp_cleanup_only_own(tmp_path):
    own = tmp_path / f"{P.TEMP_PREFIX}abc"
    own.mkdir()
    (own / P.OWNER_FILE).write_text("x.mp4.part.mp4", encoding="utf-8")
    (tmp_path / "x.mp4.part.mp4").write_bytes(b"1")
    other = tmp_path / ".PLVM_BG_TEMP_zzz"
    other.mkdir()
    keep = tmp_path / "user.mp4"
    keep.write_bytes(b"u")
    old = time.time() - 13 * 3600
    os.utime(own, (old, old))
    assert P.cleanup_stale_temp(tmp_path) == 1
    assert not own.exists() and not (tmp_path / "x.mp4.part.mp4").exists() and other.exists() and keep.exists()


@pytest.mark.skipif(not os.environ.get("PLVM_PLAYLIST_SAMPLES"), reason="실제 사용자 음원 시험: PLVM_PLAYLIST_SAMPLES 미지정")
def test_real_user_tracks_optional():  # pragma: no cover - 사용자 자료가 있을 때만
    folder = Path(os.environ["PLVM_PLAYLIST_SAMPLES"])
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in P.AUDIO_EXTS)[:15]
    tracks = [P.analyze_track(Path(FFMPEG), Path(FFPROBE), p) for p in files]
    entries, total = P.build_timeline(tracks)
    assert P.validate_srt(P.build_srt(entries), entries) == [] and total > 0


# ---------------- 화면 ----------------

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


def _pump(w, cond, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        w.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


@need_ff
def test_window_full_flow(tkroot, tmp_path):
    from app import pl_ui as U
    sent = {}
    w = U.PlaylistMakerWindow(tkroot, tools=lambda: (Path(FFMPEG), Path(FFPROBE)),
                              send_to_long=lambda p: sent.setdefault("long", p),
                              send_to_live=lambda p: sent.setdefault("live", p),
                              send_to_upload=lambda p: sent.setdefault("upload", p))
    w.quiet_errors = True
    try:
        files = [make_tone(tmp_path / "음원" / f"{k:02d}. {JP_TITLES[k - 1]}.mp3", 2.0 + k * 0.1) for k in range(1, 16)]
        bg = make_bg(tmp_path / "배경.mp4", 6)
        assert w.current_step() == 0
        assert w.set_background(bg)
        assert w.add_audio_paths(list(reversed(files)))  # 파일명 순으로 정렬되어 들어간다
        assert _pump(w, lambda: w.loading == 0 and w.plan is not None, 120), w.plan_text.get()
        assert [it.title for it in w.items][:2] == ["夜明けの窓", "静かな帰り道"] and len(w.items) == 15
        rows = [w.tree.item(i)["values"] for i in w.tree.get_children()]
        assert str(rows[0][0]).zfill(2) == "01" and rows[0][4] == "00:00" and "15곡" in w.summary.get()
        # 순서·제목 바꾸면 시작 시각·SRT가 다시 계산된다
        w.move_item(0, 2)
        w.set_title(0, "새 제목 · 새벽")
        e, _ = w.timeline()
        assert e[0].label == "01. 새 제목 · 새벽" and e[2].label == "03. 夜明けの窓"
        assert e[1].start == pytest.approx(w.items[0].track.duration)
        srt = w.save_srt_only()
        assert srt and P.validate_srt(srt.read_text(encoding="utf-8"), e) == []
        assert not any(p.suffix == ".mp4" for p in srt.parent.iterdir())  # SRT만: 영상 작업 없음
        w.outname.set("완성 플레이리스트.mp4")
        assert w.start()
        assert _pump(w, lambda: not w.running, 180), w.status.get()
        assert w.result is not None, getattr(w, "error", "")
        assert w.last_output.name == "완성 플레이리스트.mp4" and str(w.btn_live.cget("state")) == "normal"
        assert w.current_step() == 5
        for k in ("long", "live", "upload"):
            w._send(k)
        assert sent == {"long": w.last_output, "live": w.last_output, "upload": w.last_output}
    finally:
        w.destroy()


@need_ff
def test_window_hevc_result_cannot_go_to_live(tkroot, tmp_path):
    from app import pl_ui as U
    sent = {}
    w = U.PlaylistMakerWindow(tkroot, tools=lambda: (Path(FFMPEG), Path(FFPROBE)),
                              send_to_live=lambda p: sent.setdefault("live", p))
    w.quiet_errors = True
    try:
        w.codec.set("hevc")
        from app.bg_core import get_profile
        w.profile.set(get_profile("small").label)
        w.set_background(make_bg(tmp_path / "bg.mp4", 2))
        w.add_audio_paths([make_tone(tmp_path / "a.wav", 2.0)])
        assert _pump(w, lambda: w.loading == 0 and w.plan is not None, 60)
        assert w.start() and _pump(w, lambda: not w.running, 120) and w.result.codec == "hevc"
        assert str(w.btn_live.cget("state")) == "disabled" and w._send("live") is None and not sent
    finally:
        w.destroy()


def test_main_window_playlist_card_and_live_handoff(tmp_path, monkeypatch):
    import app.ui as ui
    try:
        app = ui.MainWindow(tmp_path)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no display: {e}")
    try:
        assert {"long", "live", "upload", "bg", "stock", "playlist"} <= set(app.mode_cards)
        assert "playlist" in app.card_help_links
        pw = app._open_playlist()
        assert pw.winfo_exists() and app._open_playlist() is pw
        if FFMPEG:
            mp4 = make_bg(tmp_path / "완성.mp4", 1)
            app.ffmpeg, app.ffprobe = Path(FFMPEG), Path(FFPROBE)
            added = app._receive_to_live(mp4)
            lw = app._live_window()
            assert added and lw.playlist_mode and lw.playlist.items[-1].path == mp4.resolve()
            assert not lw.controller.guard.owner if hasattr(lw.controller, "guard") else True  # 송출 시작 안 함
            lw.destroy()
        pw.destroy()
    finally:
        app._finish_close()


@need_ff
def test_subtitles_filter_detection(tmp_path):
    assert P.subtitles_available(Path(FFMPEG))
    assert not P.subtitles_available(tmp_path / "없는 ffmpeg.exe")
