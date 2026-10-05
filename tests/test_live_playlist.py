"""Playlist 순수 모델/검증/manifest/명령 (Gate 2)."""
from pathlib import Path

import pytest

from app.live_core import build_live_copy_command, build_stream_command
from app.live_playlist import (
    MAX_PLAYLIST_ITEMS, LivePlaylist, PlaylistError, build_ffconcat, entry_durations, escape_ffconcat_path,
    validate_playlist, write_ffconcat,
)
from app.live_profile import MODE_COPY, MODE_TRANSCODE, LiveConfig, LiveConfigError, validate_live_config
from app.live_ready import LiveReadyIssue, LiveReadyReport
from app.live_session import playlist_position

FAKE_KEY = "dummy-playlist-0000-not-real"


def rep(name="a.mp4", *, w=1920, h=1080, fps=30.0, ac="aac", rate=44100, ch=2, dur=60.0, vc="h264", issues=()):
    return LiveReadyReport(path=Path(name), issues=list(issues), width=w, height=h, fps=fps, duration=dur,
                           video_codec=vc, audio_codec=ac, sample_rate=rate, channels=ch)


def test_add_remove_reorder_clear_total(tmp_path):
    pl = LivePlaylist()
    for n in "abc":
        pl.add(tmp_path / f"{n}.mp4")
    assert [p.name for p in pl.paths] == ["a.mp4", "b.mp4", "c.mp4"]
    assert pl.move(2, -1) == 1
    assert [p.name for p in pl.paths] == ["a.mp4", "c.mp4", "b.mp4"]
    assert pl.move(0, -1) == 0  # 범위 밖은 그대로
    pl.remove(1)
    assert [p.name for p in pl.paths] == ["a.mp4", "b.mp4"]
    pl.set_report(tmp_path / "a.mp4", rep(dur=100))
    pl.set_report(tmp_path / "b.mp4", rep(dur=50.5))
    assert pl.total_duration == 150.5 and pl.analyzed
    pl.clear()
    assert len(pl) == 0


def test_duplicate_rejected_case_insensitive_on_windows(tmp_path):
    pl = LivePlaylist()
    pl.add(tmp_path / "Song.mp4")
    with pytest.raises(PlaylistError, match="이미 추가"):
        pl.add(tmp_path / "Song.mp4")
    with pytest.raises(PlaylistError):
        pl.add(tmp_path / "." / "Song.mp4")


def test_max_count(tmp_path):
    pl = LivePlaylist()
    for i in range(MAX_PLAYLIST_ITEMS):
        pl.add(tmp_path / f"{i}.mp4")
    with pytest.raises(PlaylistError, match="최대 20"):
        pl.add(tmp_path / "x.mp4")


def test_validation_ok():
    v = validate_playlist([rep("a"), rep("b"), rep("c")])
    assert v.ok and v.item_status == ["✓ LIVE READY"] * 3


@pytest.mark.parametrize("bad,needle", [
    (dict(w=1280, h=720), "해상도"),
    (dict(fps=29.97), "FPS가 29.97fps"),
    (dict(rate=48000), "오디오"),
    (dict(ch=1), "오디오"),
    (dict(issues=[LiveReadyIssue("keyframe", "Keyframe 간격 5초")]), "LIVE READY가 아닙니다"),
    (dict(issues=[LiveReadyIssue("vfr", "VFR", blocking=False)]), "가변 프레임"),
    (dict(issues=[LiveReadyIssue("av_length", "차이", blocking=False)]), "길이 차이"),
])
def test_validation_blocks_incompatible(bad, needle):
    v = validate_playlist([rep("a"), rep("b", **bad)])
    assert not v.ok
    assert any(needle in m for m in v.messages), v.messages
    assert any(m.startswith("2번 영상") for m in v.messages)
    assert "LIVE READY 파일 만들기" in v.messages[-1]  # 자동 재인코딩 대신 안내
    assert v.item_status[0] == "✓ LIVE READY" and v.item_status[1].startswith("✗")


def test_validation_waits_for_analysis():
    v = validate_playlist([rep("a"), None])
    assert not v.ok and "분석 중" in v.first_error


def test_entry_durations_add_one_aac_frame():
    d = entry_durations([rep(dur=10.0, rate=44100), rep(dur=20.0, rate=48000)])
    assert d[0] == pytest.approx(10.0 + 1024 / 44100)
    assert d[1] == pytest.approx(20.0 + 1024 / 48000)


@pytest.mark.parametrize("raw,expected", [
    (r"C:\music\a.mp4", "'C:/music/a.mp4'"),
    ("D:/03 long/it's here.mp4", "'D:/03 long/it'\\''s here.mp4'"),
])
def test_ffconcat_escaping(raw, expected):
    assert escape_ffconcat_path(raw) == expected


def test_ffconcat_rejects_newline_injection():
    with pytest.raises(PlaylistError):
        escape_ffconcat_path("a.mp4\nfile '/etc/passwd'")


def test_build_and_write_ffconcat(tmp_path):
    text = build_ffconcat([(Path(r"C:\x\A.mp4"), 10.02322), (Path(r"C:\x\B.mp4"), 8.5)])
    assert text == "ffconcat version 1.0\nfile 'C:/x/A.mp4'\nduration 10.023220\nfile 'C:/x/B.mp4'\nduration 8.500000\n"
    target = write_ffconcat(tmp_path / "pl.ffconcat", [(Path("A.mp4"), 1.0)])
    assert target.read_bytes().count(b"\r") == 0
    assert not (tmp_path / "pl.ffconcat.part").exists()


def test_playlist_position_rounds():
    d = [10.0, 20.0, 30.0]  # total 60
    assert playlist_position(0, d) == (0, 1)
    assert playlist_position(9.99, d) == (0, 1)
    assert playlist_position(10.0, d) == (1, 1)
    assert playlist_position(59.9, d) == (2, 1)
    assert playlist_position(60.0, d) == (0, 2)
    assert playlist_position(185.0, d) == (0, 4)
    assert playlist_position(None, d) is None and playlist_position(5, []) is None


def cfg(**kw):
    base = dict(input_path=Path("pl.ffconcat"), ingest_url="rtmps://a.rtmps.youtube.com:443/live2",
                stream_key=FAKE_KEY, mode=MODE_COPY, input_format="concat")
    base.update(kw)
    return LiveConfig(**base)


def test_playlist_command_is_concat_streamcopy_no_reencode():
    cmd = build_live_copy_command(ffmpeg=Path("ffmpeg"), config=cfg())
    i = cmd.index("-i")
    assert cmd[i - 4:i] == ["-f", "concat", "-safe", "0"]
    assert cmd.index("-re") < i and cmd[cmd.index("-stream_loop") + 1] == "-1"
    assert cmd[cmd.index("-c:v") + 1] == "copy" and cmd[cmd.index("-c:a") + 1] == "copy"
    for banned in ("libx264", "-r", "-g", "-b:v", "-vf", "-af", "-filter_complex", "-preset"):
        assert banned not in cmd
    assert FAKE_KEY in cmd[-1] and all(FAKE_KEY not in a for a in cmd[:-1])


def test_single_file_command_unchanged_by_playlist_feature():
    single = build_stream_command(ffmpeg=Path("ffmpeg"), config=cfg(input_path=Path("A.mp4"), input_format=""))
    assert "-f" in single and single[single.index("-i") - 1] == "-1"  # -stream_loop -1 바로 뒤 -i (concat 없음)
    assert "concat" not in single


def test_playlist_requires_direct_copy():
    with pytest.raises(LiveConfigError, match="DIRECT COPY"):
        validate_live_config(cfg(mode=MODE_TRANSCODE), check_input=False)
    with pytest.raises(LiveConfigError):
        validate_live_config(cfg(input_format="weird"), check_input=False)


def test_preflight_playlist(tmp_path):
    from app.core import VideoInfo
    from app.live_controller import run_preflight
    from app.live_profile import preset_by_key
    from app.tooling import FfmpegExecutionGuard
    ff, fp = tmp_path / "ffmpeg.exe", tmp_path / "ffprobe.exe"
    files = [tmp_path / f"{n}_LIVE_READY.mp4" for n in "ABC"]
    for p in (ff, fp, *files):
        p.write_bytes(b"x")
    probe = lambda p, f: VideoInfo(Path(p), 10.0, 1, 1920, 1080, 30.0, "h264", "aac", "yuv420p", "High", 44100, 2)
    common = dict(ffmpeg=ff, ffprobe=fp, input_path=files[0], ingest_url="rtmps://a.rtmps.youtube.com:443/live2",
                  stream_key=FAKE_KEY, preset=preset_by_key("1080p30"), probe=probe, guard=FfmpegExecutionGuard(),
                  manifest_path=tmp_path / "pl.ffconcat")
    r = run_preflight(**common, playlist_reports=[rep("A"), rep("B"), rep("C")])
    assert r.ok, r.report()
    assert r.config.input_format == "concat" and r.config.mode == MODE_COPY
    assert r.config.input_path == tmp_path / "pl.ffconcat"
    assert "DIRECT COPY Playlist 가능" in r.report() and FAKE_KEY not in r.report()
    r = run_preflight(**common, playlist_reports=[rep("A"), rep("B", fps=29.97)])
    assert not r.ok and any("2번 영상의 FPS가 29.97fps" in e for e in r.errors())
