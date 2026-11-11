"""통합 04 (C10) 배경 원본 오디오 — 검사 · 무음 사본 · ④ 배경 제외 · ⑥ 음악만 출력 · ⑤ 라이브러리 기록.

모든 영상·음원은 FFmpeg로 만든 '합성 시험 자료'다 (테스트 패턴·사인파·잡음). 실제 사용자 자연 영상 시험은
PLVM_NATURE_SAMPLES 지정 시에만 (없으면 SKIP).
"""
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app import media_audio as MA
from app.core import BuildCancelled, BuildError, probe_video

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
need_ff = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="FFmpeg 없음")


def _ff(*args):
    subprocess.run([FFMPEG, "-hide_banner", "-v", "error", "-y", *map(str, args)], check=True)


def make_video(path: Path, seconds=3.0, *, audio=None, size="640x360", fps=30, vcodec="libx264", src="testsrc2"):
    """audio: None | 'aac' | 'mp3' | 'opus' | 'multi' | 'silent' | 'noise'."""
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["-f", "lavfi", "-i", f"{src}=s={size}:r={fps}:d={seconds}"]
    if audio == "multi":
        args += ["-f", "lavfi", "-i", f"sine=f=440:d={seconds}", "-f", "lavfi", "-i", f"sine=f=880:d={seconds}"]
        maps = ["-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:a:0", "aac", "-c:a:1", "aac", "-ac:a:1", "6",
                "-metadata:s:a:0", "language=jpn", "-metadata:s:a:1", "language=kor"]
    elif audio == "silent":
        args += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={seconds}"]
        maps = ["-map", "0:v", "-map", "1:a", "-c:a", "aac", "-t", str(seconds)]
    elif audio == "noise":
        args += ["-f", "lavfi", "-i", f"anoisesrc=d={seconds}:a=0.8:c=white"]
        maps = ["-map", "0:v", "-map", "1:a", "-c:a", "aac"]
    elif audio:
        args += ["-f", "lavfi", "-i", f"sine=f=440:d={seconds}"]
        maps = ["-map", "0:v", "-map", "1:a", "-c:a", {"aac": "aac", "mp3": "libmp3lame", "opus": "libopus"}[audio]]
    else:
        maps = ["-map", "0:v"]
    v = ["-c:v", vcodec, "-pix_fmt", "yuv420p", "-g", str(fps)]
    if vcodec == "libx264":
        v += ["-preset", "ultrafast"]
    elif vcodec == "libvpx-vp9":
        v += ["-deadline", "realtime", "-cpu-used", "8", "-b:v", "500k"]
    _ff(*args, *maps, *v, path)
    return path


def sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def audio_count(p: Path) -> int:
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0",
                          str(p)], capture_output=True, text=True).stdout.split()
    return len(out)


# ---------------- 검사 ----------------

@need_ff
def test_check_audio_none_present_multi_and_silent_track(tmp_path):
    none = MA.check_audio(Path(FFPROBE), make_video(tmp_path / "무음 영상.mp4"))
    assert none.status == MA.NONE and none.label == MA.LABEL_NONE and none.short == "🔇 없음"
    one = MA.check_audio(Path(FFPROBE), make_video(tmp_path / "자연음.mp4", audio="aac"))
    assert one.status == MA.PRESENT and one.label == MA.LABEL_PRESENT and len(one.streams) == 1
    assert one.streams[0].codec == "aac" and one.streams[0].channels == 1 and "aac" in one.detail
    multi = MA.check_audio(Path(FFPROBE), make_video(tmp_path / "다중.mp4", audio="multi"))
    assert multi.status == MA.PRESENT and [s.channels for s in multi.streams] == [1, 6]
    assert [s.language for s in multi.streams] == ["jpn", "kor"] and "오디오 2개" in multi.detail and "2개" in multi.short
    # 소리가 없는(무음) 오디오 '트랙'도 오디오 있음 — 조용하다고 없다고 보지 않는다
    quiet = MA.check_audio(Path(FFPROBE), make_video(tmp_path / "조용한 트랙.mp4", audio="silent"))
    assert quiet.status == MA.PRESENT
    again = MA.AudioCheck.from_json(json.loads(json.dumps(multi.to_json())))
    assert again == multi


def test_check_audio_failures_are_unknown_never_none(tmp_path):
    bad = tmp_path / "손상.mp4"
    bad.write_bytes(os.urandom(2048))
    assert MA.check_audio(None, bad).status == MA.UNKNOWN
    assert MA.check_audio(Path(FFPROBE or "ffprobe"), tmp_path / "없음.mp4").error == "파일 없음"
    if FFPROBE:
        r = MA.check_audio(Path(FFPROBE), bad)
        assert r.status == MA.UNKNOWN and r.error and "다시 검사" in r.label
    assert MA.check_audio(tmp_path / "없는 ffprobe.exe", bad).status == MA.UNKNOWN
    assert MA.AudioCheck.from_json({"status": "x"}) is None


# ---------------- 무음 사본 ----------------

@need_ff
def test_silent_copy_mp4_keeps_video_and_original(tmp_path):
    src = make_video(tmp_path / "한글 폴더" / "호수 자연음.mp4", 4.0, audio="multi", fps=25)
    rights = {"state": "저작자 표시 필요", "source_url": "https://commons.wikimedia.org/wiki/File:X.webm"}
    src.with_name(src.name + ".rights.json").write_text(json.dumps(rights, ensure_ascii=False), encoding="utf-8")
    before, st = sha(src), src.stat()
    seen = []
    r = MA.make_silent_copy(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), src=src, progress=lambda f, t: seen.append(f))
    assert r.output.name == "호수 자연음_noaudio.mp4" and r.output.parent == src.parent
    assert audio_count(r.output) == 0 and r.removed_streams == 2
    a, b = probe_video(src, Path(FFPROBE)), probe_video(r.output, Path(FFPROBE))
    assert (b.video_codec, b.width, b.height, b.fps) == (a.video_codec, a.width, a.height, a.fps) == ("h264", 640, 360, 25.0)
    assert abs(r.duration - 4.0) < 0.05 and r.codec == "h264"
    assert sha(src) == before and src.stat().st_mtime_ns == st.st_mtime_ns          # 원본 그대로
    side = json.loads(r.sidecar.read_text(encoding="utf-8"))
    assert side["source"]["sha256"] == before == r.source_sha256 and side["output"]["sha256"] == sha(r.output)
    assert side["rights_from_source"] == rights and side["rights_note"] == MA.RIGHTS_NOTE
    linked = json.loads(r.output.with_name(r.output.name + ".rights.json").read_text(encoding="utf-8"))
    assert linked["state"] == rights["state"] and linked["note"] == MA.RIGHTS_NOTE   # 증빙 연결, 해결로 표시 안 함
    assert seen and seen[-1] == 1.0
    r2 = MA.make_silent_copy(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), src=src)
    assert r2.output.name == "호수 자연음_noaudio_2.mp4"                             # 덮어쓰지 않음
    with pytest.raises(BuildError, match="덮어쓰지 않음"):
        MA.make_silent_copy(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), src=src, out=r.output)
    assert not list(src.parent.glob("*.part*"))


@need_ff
def test_silent_copy_webm_mov_and_incompatible(tmp_path):
    webm = make_video(tmp_path / "clip.webm", 2.0, audio="opus", vcodec="libvpx-vp9")
    r = MA.make_silent_copy(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), src=webm)
    assert r.output.suffix == ".webm" and r.codec == "vp9" and audio_count(r.output) == 0
    mov = make_video(tmp_path / "clip.mov", 2.0, audio="aac")
    r2 = MA.make_silent_copy(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), src=mov)
    assert r2.output.suffix == ".mp4" and r2.codec == "h264" and audio_count(r2.output) == 0
    avi = make_video(tmp_path / "clip.avi", 1.0, audio="mp3", vcodec="mpeg4")
    with pytest.raises(BuildError, match="지원하지 않습니다"):
        MA.make_silent_copy(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), src=avi)
    assert "재인코딩하지 않음" in MA.container_problem(tmp_path / "raw.mov", "rawvideo")
    assert MA.container_problem(tmp_path / "x.mkv", "anything") == ""
    # 확장자가 아니라 실제 형식으로: .webm 이름의 H.264 MP4 → .mp4 사본
    assert MA.copy_format(tmp_path / "named.webm", "h264", "mov,mp4,m4a,3gp,3g2,mj2") == (".mp4", "")
    assert MA.copy_format(tmp_path / "named.mp4", "vp9", "matroska,webm") == (".webm", "")
    assert MA.copy_format(tmp_path / "x.avi", "mpeg4", "avi")[0] is None


@need_ff
def test_silent_copy_cancel_and_disk_shortage_leave_nothing(tmp_path):
    src = make_video(tmp_path / "a.mp4", 3.0, audio="aac")
    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(BuildError, match="저장 공간 부족"):
        MA.make_silent_copy(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), src=src, disk_free=lambda d: 10)
    stop = threading.Event()
    stop.set()
    with pytest.raises(BuildCancelled):
        MA.make_silent_copy(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), src=src, cancel=stop)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


# ---------------- ④ 배경: 원본 오디오 제외 ----------------

def _bg_build(tmp_path, specs, name):
    from app import bg_core as B
    clips = []
    for k, (audio, src) in enumerate(specs):
        p = make_video(tmp_path / f"{name}" / f"{k:02d}_클립.mp4", 2.5, audio=audio, src=src)
        clips.append(B.clip_from_info(probe_video(p, Path(FFPROBE))))
    plan = B.build_bg_plan(clips, 6.0, 0.5, B.choose_output_spec([c.info for c in clips]))
    r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan,
                               output=tmp_path / name / "배경 결과.mp4", cancel_event=threading.Event())
    return r, [c.path for c in clips]


@need_ff
@pytest.mark.parametrize("name,specs", [
    ("all_audio", [("aac", "testsrc2"), ("aac", "smptebars")]),
    ("all_silent", [(None, "testsrc2"), (None, "smptebars")]),
    ("mixed_codecs_multi", [("aac", "testsrc2"), (None, "smptebars"), ("mp3", "rgbtestsrc"), ("multi", "testsrc")]),
])
def test_bg_maker_output_has_no_audio(tmp_path, name, specs):
    hashes_src = None
    r, srcs = _bg_build(tmp_path, specs, name)
    assert audio_count(r.output) == 0 and probe_video(r.output, Path(FFPROBE)).audio_codec == ""
    assert abs(r.duration - 6.0) < 0.2
    hashes_src = [audio_count(p) for p in srcs]
    assert hashes_src == [{"aac": 1, "mp3": 1, "multi": 2, None: 0}[a] for a, _ in specs]  # 원본 오디오는 그대로


# ---------------- ⑥ 플레이리스트: 음악만 ----------------

@need_ff
def test_playlist_audio_comes_only_from_music(tmp_path):
    from app import pl_core as P
    bg_path = make_video(tmp_path / "배경 (큰 잡음).mp4", 5.0, audio="noise")
    _ff("-f", "lavfi", "-i", "anoisesrc=d=5:a=0.8", "-c:a", "aac", tmp_path / "extra.m4a")
    two = tmp_path / "배경 2트랙.mp4"   # 배경에 오디오 2개
    _ff("-i", bg_path, "-i", tmp_path / "extra.m4a", "-map", "0:v", "-map", "0:a", "-map", "1:a", "-c", "copy", two)
    tracks = []
    for k in (1, 2):  # 음악은 '완전 무음' → 결과에 소리가 있으면 배경 오디오가 섞인 것
        p = tmp_path / f"0{k}. 静かな曲{k}.wav"
        _ff("-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=13", p)
        tracks.append(P.analyze_track(Path(FFMPEG), Path(FFPROBE), p))
    for bgfile in (bg_path, two):
        bg = probe_video(bgfile, Path(FFPROBE))
        assert MA.check_audio(Path(FFPROBE), bgfile).status == MA.PRESENT
        for burn in (False, True):
            plan = P.build_playlist_plan(tracks, bg, burn_titles=burn, profile="small")
            out = tmp_path / "out" / f"{bgfile.stem}_{int(burn)}.mp4"
            r = P.run_playlist_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out,
                                     cancel_event=threading.Event())
            assert audio_count(r.output) == 1 and abs(r.audio_duration - 26.0) < 0.1   # 15곡 길이 그대로
            vd = subprocess.run([FFMPEG, "-hide_banner", "-i", str(r.output), "-map", "0:a", "-af", "volumedetect", "-f",
                                 "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
            mx = float(vd.split("max_volume:")[1].split("dB")[0])
            assert mx <= -80.0, vd[-300:]                         # 배경 잡음이 섞이지 않음
            srt = r.srt.read_text(encoding="utf-8")
            assert "00:00:00,000 --> 00:00:12,000" in srt and "00:00:13,000 --> 00:00:25,000" in srt
    assert sha(bg_path) == sha(bg_path)


# ---------------- ⑤ 라이브러리 기록 ----------------

@need_ff
def test_download_records_audio_and_silent_copy_entry(tmp_path):
    import test_stock_c04_c05 as T
    from app import stock_download as D
    from app import stock_sources as S
    data = make_video(tmp_path / "src.mp4", 3.0, audio="aac").read_bytes()
    web = T.FakeWeb()
    fn, _ = T.range_server(data)
    web.route("https://upload.wikimedia.org/", fn)
    lib = D.Library(tmp_path / "라이브러리")
    c = T.dl_cand(dur=3.0, w=640, h=360)
    e = D.download(c, c.renditions[0], lib, mode=S.MODE_SHORT, ffprobe=Path(FFPROBE), ffmpeg=Path(FFMPEG),
                   cancel=threading.Event(), opener=web, resolver=T.PUBLIC, sleep=lambda s: None)
    ac = MA.AudioCheck.from_json(e["audio"])
    assert ac.status == MA.PRESENT and ac.streams[0].codec == "aac"
    src = Path(e["path"])
    before = sha(src)
    r = MA.make_silent_copy(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), src=src)
    d = lib.add_derived(lib.entries()[0], r)
    es = lib.entries()
    assert len(es) == 2 and es[0]["silent_copy"]["path"] == str(r.output) and es[0]["sha256"] == before
    assert d["derived_from"]["sha256"] == before and d["sha256"] == sha(r.output) and d["audio"]["status"] == MA.NONE
    assert d["rights"] == es[0]["rights"] and MA.RIGHTS_NOTE in d["warnings"]           # 권리 기록 연결
    assert sha(src) == before and lib.update(str(src), {"x": 1})["x"] == 1 and lib.update("없음", {}) is None


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
def test_bg_window_shows_audio_and_makes_silent_copy(tkroot, tmp_path):
    from app import bg_ui as U
    w = U.BackgroundMakerWindow(tkroot, tools=lambda: (Path(FFMPEG), Path(FFPROBE)))
    w.quiet_errors = True
    try:
        files = [make_video(tmp_path / "자연음 있음.mp4", 3.0, audio="aac"),
                 make_video(tmp_path / "무음.mp4", 3.0, src="smptebars"),
                 make_video(tmp_path / "다중 오디오.mp4", 3.0, audio="multi", src="rgbtestsrc")]
        w.add_paths(files)
        assert _pump(w, lambda: len(w.audio_checks) == 3 and not w.loading, 120)
        cells = {w.tree.item(i)["values"][1]: w.tree.item(i)["values"][9] for i in w.tree.get_children()}
        assert cells["자연음 있음.mp4"].startswith("🔊") and cells["무음.mp4"] == "🔇 없음" and "2개" in cells["다중 오디오.mp4"]
        assert "오디오 있는 영상 2개 — 배경 제작 시 자동 제외" in w.summary.get()
        assert str(w.always_exclude.get()) in ("True", "1")
        idx = [c.path.name for c in w.clips].index("자연음 있음.mp4")
        w.tree.selection_set(str(idx)); w._show_detail()
        assert MA.LABEL_PRESENT in w.detail.get()
        assert w.save_silent_copy()
        assert _pump(w, lambda: not w.silent_running, 60) and w.last_silent is not None
        assert audio_count(w.last_silent.output) == 0 and "원본은 그대로" in w.status.get()
        assert w.recheck_audio() and _pump(w, lambda: len(w.audio_checks) == 3, 30)
    finally:
        w.destroy()


@need_ff
def test_stock_window_audio_column_and_silent_button(tkroot, tmp_path):
    from app import stock_download as D
    from app import stock_ui as U
    libdir = tmp_path / "lib"
    src = make_video(libdir / "Commons" / "lake.mp4", 2.0, audio="aac")
    lib = D.Library(libdir)
    lib.add({"key": "c:1", "provider": "Wikimedia Commons", "path": str(src), "sha256": sha(src), "probe": {"playable": True},
             "rights": {"state": "사용 검토 가능"}, "warnings": [], "audio": MA.check_audio(Path(FFPROBE), src).to_json()})
    w = U.StockSearchWindow(tkroot, tools=lambda: (Path(FFMPEG), Path(FFPROBE)), adapters={}, check_dns=False)
    w.quiet_errors = True
    try:
        w.libdir.set(str(libdir))
        w._refresh_library()
        row = w.lib.item(w.lib.get_children()[0])["values"]
        assert row[6].startswith("🔊") and not w.save_silent.get()                     # 기본: 별도 저장 꺼짐
        w.lib.selection_set(w.lib.get_children()[0])
        assert w.save_silent_selected()
        assert _pump(w, lambda: not w.silent_running, 60)
        es = D.Library(libdir).entries()
        assert len(es) == 2 and es[1]["derived_from"]["path"] == str(src) and audio_count(Path(es[1]["path"])) == 0
        rows = [w.lib.item(i)["values"][6] for i in w.lib.get_children()]
        assert any("무음 사본 있음" in r for r in rows) and "🔇 무음 사본" in rows
        w.save_silent.set(True); w._save_audio_opts()
        from app.settings import load_settings
        assert load_settings()[MA.SETTINGS_KEY] == {"exclude": True, "save_silent_copy": True}
    finally:
        w.destroy()


@need_ff
def test_playlist_window_shows_bg_audio(tkroot, tmp_path):
    from app import pl_ui as U
    w = U.PlaylistMakerWindow(tkroot, tools=lambda: (Path(FFMPEG), Path(FFPROBE)))
    w.quiet_errors = True
    try:
        w.set_background(make_video(tmp_path / "bg.mp4", 2.0, audio="aac"))
        assert _pump(w, lambda: w.bg is not None, 30)
        w._refresh_all()
        assert MA.LABEL_PRESENT in w.bg_text.get() and w.bg_audio.status == MA.PRESENT
    finally:
        w.destroy()


@need_ff
def test_stock_auto_silent_after_download_only_for_audio(tkroot, tmp_path):
    """'오디오 없는 MP4를 별도로 저장'이 켜져 있으면 받은 영상 중 오디오가 있는 것만 사본 (작업 스레드 경로)."""
    from app import stock_download as D
    from app import stock_ui as U
    libdir = tmp_path / "lib"
    with_a = make_video(libdir / "a.mp4", 1.5, audio="aac")
    without = make_video(libdir / "b.mp4", 1.5)
    lib = D.Library(libdir)
    for k, f in enumerate((with_a, without)):
        lib.add({"key": f"k{k}", "path": str(f), "sha256": sha(f), "rights": {"state": "사용 검토 가능"}, "warnings": [],
                 "audio": MA.check_audio(Path(FFPROBE), f).to_json()})
    w = U.StockSearchWindow(tkroot, tools=lambda: (Path(FFMPEG), Path(FFPROBE)), adapters={}, check_dns=False)
    try:
        t = threading.Thread(target=w._silent_worker, args=([with_a, without], FFMPEG, FFPROBE, libdir), kwargs={"auto": True})
        t.start(); t.join(60)
        assert _pump(w, lambda: bool(getattr(w, "silent_message", "")), 10)
        es = D.Library(libdir).entries()
        assert len(es) == 3 and es[2]["derived_from"]["path"] == str(with_a) and "silent_copy" not in es[1]
        assert "1개 완성" in w.silent_message
    finally:
        w.destroy()
