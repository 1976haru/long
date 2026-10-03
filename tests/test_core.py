from pathlib import Path

from app.core import (
    VideoInfo, build_round_plan, build_time_plan, default_output_name_rounds,
    default_output_name_time, format_duration, strict_copy_compatibility,
    target_seconds, verify_stream_copy,
)


def sample(name="a.mp4", duration=3600.0, size=4_000_000_000, width=1920,
           height=1080, fps=30.0, v="h264", a="aac", pix="yuv420p",
           rate=48000, ch=2):
    return VideoInfo(Path(name), duration, size, width, height, fps, v, a, pix, "High", rate, ch)


def test_duration():
    assert format_duration(3600) == "01:00:00"
    assert format_duration(3661) == "01:01:01"


def test_target():
    assert target_seconds(10, 0) == 36000
    assert target_seconds(7, 30) == 27000


def test_round_plan_does_not_cut_set():
    info = sample(duration=49 * 60 + 26, size=1_700_000_000)
    plan = build_round_plan([info], 10)
    assert plan.trim_to is None
    assert len(plan.sequence) == 10
    assert format_duration(plan.expected_duration) == "08:14:20"


def test_round_plan_multi_set_means_full_cycle():
    a = sample("a.mp4", duration=120)
    b = sample("b.mp4", duration=180)
    plan = build_round_plan([a, b], 3)
    assert [p.name for p in plan.sequence] == ["a.mp4", "b.mp4"] * 3
    assert plan.expected_duration == 900


def test_time_plan_can_trim():
    info = sample(duration=3600, size=1_000_000_000)
    plan = build_time_plan([info], 10 * 3600)
    assert len(plan.sequence) == 10
    assert plan.trim_to == 36000


def test_compatibility_blocks_mismatch():
    a = sample("a.mp4")
    b = sample("b.mp4", width=1280)
    ok, reason = strict_copy_compatibility([a, b])
    assert not ok
    assert "가로 해상도" in reason


def test_verify_stream_copy_signature():
    src = sample("a.mp4", duration=10)
    out = sample("out.mp4", duration=100)
    ok, _ = verify_stream_copy(src, out, 100, 10)
    assert ok
    bad = sample("out.mp4", duration=100, v="hevc")
    ok, _ = verify_stream_copy(src, bad, 100, 10)
    assert not ok


def test_names():
    p = Path("story 01.mp4")
    assert default_output_name_rounds(p, 15) == "story 01_15R_FINAL.mp4"
    assert default_output_name_time(p, 10, 0) == "story 01_10H_FINAL.mp4"
    assert default_output_name_time(p, 7, 30) == "story 01_7H30M_FINAL.mp4"
