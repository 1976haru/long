from pathlib import Path

import pytest

from app.live_core import ProgressParser, build_live_command
from app.live_profile import YOUTUBE_DEFAULT_PROFILE, LiveConfig, LiveConfigError, validate_live_config

FAKE_KEY = "test-fake-key0-0000-zzzz"


def cfg(**kw):
    base = dict(input_path=Path("set.mp4"), ingest_url="rtmp://a.rtmp.youtube.com/live2", stream_key=FAKE_KEY)
    base.update(kw)
    return LiveConfig(**base)


def opt(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_command_input_loop_and_realtime_pacing():
    cmd = build_live_command(ffmpeg=Path("ffmpeg"), config=cfg())
    assert opt(cmd, "-i") == "set.mp4"
    assert opt(cmd, "-stream_loop") == "-1"
    # -re / -stream_loop 는 입력 옵션이므로 -i 앞에 있어야 한다.
    assert cmd.index("-re") < cmd.index("-i")
    assert cmd.index("-stream_loop") < cmd.index("-i")
    assert "-c" not in cmd and "copy" not in cmd


def test_command_codecs_fps_keyframe():
    cmd = build_live_command(ffmpeg=Path("ffmpeg"), config=cfg(fps=30, keyframe_seconds=2, video_bitrate_kbps=6000, audio_bitrate_kbps=160))
    assert opt(cmd, "-c:v") == "libx264"
    assert opt(cmd, "-c:a") == "aac"
    assert opt(cmd, "-r") == "30"
    assert opt(cmd, "-g") == "60"
    assert opt(cmd, "-keyint_min") == "60"
    assert opt(cmd, "-sc_threshold") == "0"
    assert opt(cmd, "-b:v") == "6000k"
    assert opt(cmd, "-b:a") == "160k"
    assert opt(cmd, "-pix_fmt") == "yuv420p"
    assert opt(cmd, "-progress") == "pipe:1"


def test_command_keyframe_follows_fps():
    cmd = build_live_command(ffmpeg=Path("ffmpeg"), config=cfg(fps=60, keyframe_seconds=2))
    assert opt(cmd, "-g") == "120"


def test_command_rtmp_and_rtmps_targets():
    cmd = build_live_command(ffmpeg=Path("ffmpeg"), config=cfg())
    assert opt(cmd, "-f") == "flv"
    assert cmd[-1] == f"rtmp://a.rtmp.youtube.com/live2/{FAKE_KEY}"
    c2 = YOUTUBE_DEFAULT_PROFILE.to_config(Path("set.mp4"), FAKE_KEY)
    cmd2 = build_live_command(ffmpeg=Path("ffmpeg"), config=c2)
    assert cmd2[-1] == f"rtmps://a.rtmps.youtube.com:443/live2/{FAKE_KEY}"


def test_command_output_override_for_smoke():
    cmd = build_live_command(ffmpeg=Path("ffmpeg"), config=cfg(), output_target="out.flv")
    assert cmd[-1] == "out.flv"
    assert FAKE_KEY not in " ".join(cmd)


def test_validate_rejects_bad_values(tmp_path):
    for bad in (dict(fps=0), dict(fps=120), dict(keyframe_seconds=5), dict(video_bitrate_kbps=10), dict(audio_sample_rate=22050)):
        with pytest.raises(LiveConfigError):
            validate_live_config(cfg(**bad), check_input=False)
    with pytest.raises(LiveConfigError):
        validate_live_config(cfg(input_path=tmp_path / "missing.mp4"))
    f = tmp_path / "ok.mp4"
    f.write_bytes(b"x")
    validate_live_config(cfg(input_path=f))


def test_progress_parser_extracts_stats():
    p = ProgressParser()
    block = [
        "frame=900", "fps=30.01", "bitrate=8123.4kbits/s", "total_size=1000",
        "out_time_us=30000000", "out_time=00:00:30.000000", "speed=1.00x", "progress=continue",
    ]
    results = [p.feed(x) for x in block]
    assert results[:-1] == [None] * (len(block) - 1)
    s = results[-1]
    assert s.fps == pytest.approx(30.01)
    assert s.bitrate == "8123.4kbits/s"
    assert s.speed == pytest.approx(1.0)
    assert s.out_time_seconds == pytest.approx(30.0)
    assert s.frame == 900
    for x in ["fps=0.00", "bitrate=N/A", "out_time_us=N/A", "speed=N/A", "progress=continue"]:
        s = p.feed(x)
    assert s.bitrate is None and s.speed is None and s.out_time_seconds is None
