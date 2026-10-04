import hashlib
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from app.live_ready import (
    LiveReadyCancelled, analyze_live_ready, build_live_ready_command, live_ready_output_path, make_live_ready_file,
)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
real = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")


def gen(path: Path, *, d=6, gop=60, ac=2, rate=44100, acodec="aac", vcodec="libx264", pix="yuv420p", audio=True, audio_d=None):
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc=s=320x180:r=30:d={d}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}:duration={audio_d or d}"]
    cmd += ["-c:v", vcodec, "-pix_fmt", pix]
    if vcodec == "libx264":
        cmd += ["-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0"]
    if audio:
        cmd += ["-c:a", acodec, "-ac", str(ac)]
    cmd += [str(path)]
    subprocess.run(cmd, check=True)
    return path


def codes(rep):
    return {i.code for i in rep.issues if i.blocking}


@real
def test_ready_file(tmp_path):
    rep = analyze_live_ready(gen(tmp_path / "ok.mp4"), Path(FFPROBE))
    assert rep.ready, rep.summary_lines()
    assert rep.keyframe_max == pytest.approx(2.0, abs=0.05)
    text = "\n".join(rep.summary_lines())
    assert "✓ LIVE READY" in text and "H.264" in text and "Keyframe 약 2초" in text and "무재인코딩" in text
    assert (rep.width, rep.height, round(rep.fps)) == (320, 180, 30)


@real
@pytest.mark.parametrize("kw,code", [
    (dict(gop=150), "keyframe"),
    (dict(ac=1), "channels"),
    (dict(acodec="libmp3lame"), "acodec"),
    (dict(rate=22050), "sample_rate"),
    (dict(audio=False), "no_audio"),
    (dict(pix="yuv444p"), "pix_fmt"),
    (dict(vcodec="mpeg4", pix="yuv420p"), "vcodec"),
])
def test_not_ready_reasons(tmp_path, kw, code):
    rep = analyze_live_ready(gen(tmp_path / "x.mp4", **kw), Path(FFPROBE))
    assert not rep.ready
    assert code in codes(rep), rep.summary_lines()
    assert "⚠ LIVE READY 아님" in rep.summary_lines()[0]


@real
def test_keyframe_reported_as_5_seconds(tmp_path):
    rep = analyze_live_ready(gen(tmp_path / "g.mp4", d=12, gop=150), Path(FFPROBE))
    assert any("Keyframe 간격 5초" in i.message for i in rep.issues)


@real
def test_av_length_mismatch_is_warning(tmp_path):
    rep = analyze_live_ready(gen(tmp_path / "av.mp4", d=6, audio_d=4), Path(FFPROBE))
    assert rep.ready  # 경고만
    assert any(i.code == "av_length" and not i.blocking for i in rep.issues)


def test_missing_file(tmp_path):
    rep = analyze_live_ready(tmp_path / "none.mp4", Path("ffprobe"))
    assert not rep.ready and "missing" in codes(rep)


def fake_runner(meta, packets):
    def run(cmd, **kw):
        assert kw.get("shell") is not True
        out = packets if "packet=pts_time,dts_time,flags" in cmd else meta
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")
    return run


META = {"format": {"duration": "600", "bit_rate": "8200000"},
        "streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p", "width": 1920, "height": 1080,
                     "avg_frame_rate": "30/1", "r_frame_rate": "30/1", "bit_rate": "8000000", "duration": "600"},
                    {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2, "duration": "600"}]}


def pk(times, keys):
    return {"packets": [{"pts_time": str(t), "dts_time": str(t), "flags": "K__" if t in keys else "___"} for t in times]}


def test_analyzer_reads_only_sample_window(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return fake_runner(META, pk([i / 30 for i in range(1800)], {i * 2.0 for i in range(31)}))(cmd, **kw)
    rep = analyze_live_ready(f, Path("ffprobe"), runner=run)
    assert rep.ready, rep.summary_lines()
    assert "✓ 1920×1080 / 30fps" in rep.summary_lines()
    pkt_cmd = next(c for c in seen if "packet=pts_time,dts_time,flags" in c)
    assert pkt_cmd[pkt_cmd.index("-read_intervals") + 1] == "%+60"
    assert "-show_frames" not in pkt_cmd  # frame 디코딩 없음


def test_analyzer_timestamp_and_vfr_and_bitrate(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    meta = json.loads(json.dumps(META))
    meta["streams"][0]["avg_frame_rate"] = "24/1"
    meta["streams"][0]["bit_rate"] = "60000000"
    packets = {"packets": [{"pts_time": "0", "dts_time": "0", "flags": "K_"}, {"pts_time": "1", "dts_time": "1", "flags": "__"},
                           {"pts_time": "0.5", "dts_time": "0.5", "flags": "__"}, {"pts_time": "2", "dts_time": "2", "flags": "K_"}]}
    rep = analyze_live_ready(f, Path("ffprobe"), runner=fake_runner(meta, packets))
    assert {"timestamps", "bitrate"} <= codes(rep)
    assert any(i.code == "vfr" for i in rep.issues)


def test_live_ready_command_defaults():
    cmd = build_live_ready_command(ffmpeg=Path("ffmpeg"), src=Path("in.mp4"), dst=Path("out.part.mp4"), height=1080)
    opt = lambda k: cmd[cmd.index(k) + 1]
    assert opt("-c:v") == "libx264" and opt("-pix_fmt") == "yuv420p" and opt("-r") == "30"
    assert opt("-g") == "60" and opt("-sc_threshold") == "0"
    assert opt("-c:a") == "aac" and opt("-b:a") == "128k" and opt("-ar") == "44100" and opt("-ac") == "2"
    assert "-vf" not in cmd  # 1080p 이하 해상도 유지
    cmd4k = build_live_ready_command(ffmpeg=Path("ffmpeg"), src=Path("in.mp4"), dst=Path("o.mp4"), height=2160)
    assert cmd4k[cmd4k.index("-vf") + 1] == "scale=-2:1080"


def test_output_path_never_overwrites(tmp_path):
    src = tmp_path / "song.mp4"
    src.write_bytes(b"x")
    assert live_ready_output_path(src).name == "song_LIVE_READY.mp4"
    (tmp_path / "song_LIVE_READY.mp4").write_bytes(b"y")
    assert live_ready_output_path(src).name == "song_LIVE_READY_2.mp4"


@real
def test_make_live_ready_file_converts_once_and_keeps_original(tmp_path):
    src = gen(tmp_path / "orig.mp4", d=6, gop=150, ac=1)
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    prog = []
    out = make_live_ready_file(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), src=src, height=180, duration=6,
                               cancel=threading.Event(), progress_cb=lambda f, t: prog.append(f))
    assert out.name == "orig_LIVE_READY.mp4"
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before  # 원본 무변경
    rep = analyze_live_ready(out, Path(FFPROBE))
    assert rep.ready and rep.channels == 2 and rep.sample_rate == 44100
    assert not list(tmp_path.glob("*.part.mp4"))
    assert prog and prog[-1] == 1.0


@real
def test_make_live_ready_cancel_cleans_part(tmp_path):
    src = gen(tmp_path / "long.mp4", d=30, gop=150)
    ev = threading.Event()
    ev.set()
    with pytest.raises(LiveReadyCancelled):
        make_live_ready_file(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), src=src, height=180, duration=30, cancel=ev,
                             progress_cb=lambda f, t: None)
    assert not list(tmp_path.glob("*.part.mp4"))
    assert not (tmp_path / "long_LIVE_READY.mp4").exists()
