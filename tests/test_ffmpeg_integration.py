import shutil
import subprocess
from pathlib import Path
from threading import Event

import pytest

from app.core import build_round_plan, probe_video, run_concat_copy


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")
def test_stream_copy_repeat_keeps_stream_properties(tmp_path: Path):
    src = tmp_path / "source.mp4"
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=320x180:r=30:d=2",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(src),
    ], check=True)
    info = probe_video(src, Path(FFPROBE))
    plan = build_round_plan([info], 3)
    out = tmp_path / "out.mp4"
    result = run_concat_copy(
        ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), infos=[info],
        plan=plan, output=out, cancel_event=Event(),
    )
    out_info = probe_video(out, Path(FFPROBE))
    assert result.output == out
    assert out_info.video_codec == info.video_codec
    assert out_info.audio_codec == info.audio_codec
    assert out_info.width == info.width
    assert out_info.height == info.height
    assert out_info.pix_fmt == info.pix_fmt
    assert abs(out_info.duration - plan.expected_duration) < 2.0
