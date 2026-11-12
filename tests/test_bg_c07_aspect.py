"""통합 07 — 모든 화면 비율 영상·사진을 16:9 배경으로 (흐림 배경 / 꽉 채우기 / 원본 전체) + 길이 맞춤.

단위: 비율 판별·맞춤 계산·출력 해상도·경고, 필터 그래프(기존 16:9 경로 문자열 그대로), 사진 입력·움직임, EXIF 방향,
      표시 시간 입력, 10·25·35초 + 사진 30초 → 50분 7초 계획.
실제 FFmpeg(합성 영상): 9:16·16:9·1:1·4:3·21:9·회전 메타데이터·SAR·JPG(EXIF)·PNG·WebP 판별, 방식별 화면, 1080p 혼합 제작
      (16:9·오디오 0·CapCut 호환·정확한 프레임·원본 보존·한글/일본어 경로·미리보기=최종), 짧은 원본 조합 경계, 사진 움직임
      연속성, 취소 정리, GUI.
장시간(50분 7초 실제 제작)은 PLVM_LONG_TESTS=1 일 때만 (기본 SKIP).
"""
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import time
from pathlib import Path
from threading import Event

import pytest

from app import bg_core as B
from app import bg_fit as F
from app.core import BuildCancelled, VideoInfo

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")


def vi(name="a.mp4", dur=30.0, w=1920, h=1080, fps=30.0):
    return VideoInfo(Path(name).resolve(), dur, 1000, w, h, fps, "h264", "aac", "yuv420p", "High", 48000, 2)


def img_clip(name="사진.jpg", w=1200, h=1600, dur=30.0, motion=F.MOTION_NONE, exif=1):
    info = VideoInfo(Path(name).resolve(), dur, 1000, w, h, 0.0, "mjpeg", "", "yuvj420p", F.STILL_TAG, 0, 0)
    return B.Clip(info, meta=F.MediaMeta(True, w, h, 1.0, 0, exif), motion=motion)


# ---------------- 비율 · 맞춤 계산 ----------------

@pytest.mark.parametrize("w,h,label", [(1080, 1920, "9:16 세로"), (1920, 1080, "16:9"), (1080, 1080, "1:1 정사각형"),
                                       (1440, 1080, "4:3"), (2560, 1080, "21:9 와이드"), (1200, 1600, "3:4 세로")])
def test_aspect_label(w, h, label):
    assert F.aspect_label(w, h) == label


def test_fit_geometry_never_stretches():
    W, H = 1920, 1080
    for (w, h) in ((1080, 1920), (1080, 1080), (1440, 1080), (2560, 1080), (1200, 1600)):
        for fit in (F.FIT_BLUR, F.FIT_CONTAIN):
            g = F.fit_geometry(w, h, W, H, fit)
            assert g.kept == 1.0 and g.fg_w <= W and g.fg_h <= H
            assert abs((g.fg_w / g.fg_h) / (w / h) - 1) < 0.01  # 원본 비율 그대로
        g = F.fit_geometry(w, h, W, H, F.FIT_FILL)
        assert (g.fg_w, g.fg_h) == (W, H) and g.kept < 1
    g = F.fit_geometry(1080, 1920, W, H, F.FIT_FILL)
    assert g.kept == pytest.approx((9 / 16) / (16 / 9), abs=1e-6) and g.lost_axis == "위아래"
    assert F.fit_geometry(2560, 1080, W, H, F.FIT_FILL).lost_axis == "좌우"
    assert F.fit_geometry(1920, 1080, W, H, F.FIT_BLUR).plain  # 16:9는 어떤 방식이든 기존 경로


def test_output_spec_resolution_fit_and_notes():
    portrait = vi("세로.mp4", 10, 1080, 1920)
    s = B.choose_output_spec([portrait])                       # 기본 = 흐림 배경
    assert (s.width, s.height, s.fit) == (1920, 1080, F.FIT_BLUR) and "흐림 배경" in s.label
    assert any("흐린 배경" in n and "새 장면을 만들지 않음" in n for n in s.notes)
    fill = B.choose_output_spec([portrait], fit=F.FIT_FILL)
    assert (fill.width, fill.height) == (1920, 1080)
    assert any("심각한 잘림" in n and "68%" in n for n in fill.notes)
    assert any("확대" in n and "1.8배" in n for n in fill.notes)
    cont = B.choose_output_spec([portrait], fit=F.FIT_CONTAIN, pad="0xFFFFFF")
    assert cont.pad == "0xFFFFFF" and any("단색 여백" in n for n in cont.notes)
    assert (B.choose_output_spec([portrait], B.QUALITY_HD).height) == 720
    k4 = B.choose_output_spec([portrait, vi("b.mp4")], B.QUALITY_4K)   # 세로 1920화소 → 확대 없이 되는 1440p까지만
    assert k4.height == 1440 and any("가짜 4K" in n for n in k4.notes)  # 확대 4K를 실제 4K처럼 만들지 않음
    assert B.choose_output_spec([vi("b.mp4")], B.QUALITY_4K).height == 1080
    assert B.choose_output_spec([vi(w=3840, h=2160)], B.QUALITY_4K).height == 2160
    # 사진은 FPS 투표에 참여하지 않는다 / 사진만이면 30 FPS
    mix = B.choose_output_spec([vi("v.mp4", 10, fps=25), img_clip().info, img_clip("b.png").info])
    assert mix.fps == 25.0 and not any("FPS" in n for n in mix.notes)
    assert B.choose_output_spec([img_clip().info]).fps == 30.0
    small = B.choose_output_spec([img_clip("작은.jpg", 400, 300).info], B.QUALITY_FHD)
    assert small.height == 360 and any("확대하지 않고" in n for n in small.notes)


def test_legacy_169_filter_string_unchanged():
    spec = B.choose_output_spec([vi()])
    seg = B.clip_segment(B.Clip(vi()), 1.0, 5.0)
    legacy = f"[0:v]{B._norm_chain(spec, 5.0)}[v]"
    for fit in F.FIT_CHOICES:
        s2 = B.choose_output_spec([vi()], fit=fit)
        assert B._norm_graph(s2, seg, 5.0, "[0:v]", "[v]", "ia") == legacy
    cmd = B.piece_command(Path("ffmpeg"), B.Piece("body", seg, None, 5.0, "t"), spec, Path("o.mp4"))
    assert cmd[cmd.index("-filter_complex") + 1] == legacy and "-loop" not in cmd and "-ss" in cmd


def test_filter_graph_per_mode_and_no_stretch():
    c = B.Clip(vi("세로.mp4", 10, 1080, 1920))
    seg = B.clip_segment(c, 1.0, 4.0)
    graphs = {}
    for fit in F.FIT_CHOICES:
        spec = B.choose_output_spec([c.info], fit=fit, blur=3)
        graphs[fit] = B._norm_graph(spec, seg, 4.0, "[0:v]", "[v]", "ia")
    assert "gblur=sigma=10" in graphs[F.FIT_BLUR] and "overlay=x=(W-w)/2:y=(H-h)/2" in graphs[F.FIT_BLUR]
    assert "force_original_aspect_ratio=decrease" in graphs[F.FIT_BLUR] and F.BG_EQ in graphs[F.FIT_BLUR]
    assert "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x000000" in graphs[F.FIT_CONTAIN]
    assert "force_original_aspect_ratio=increase" in graphs[F.FIT_FILL] and "crop=1920:1080" in graphs[F.FIT_FILL]
    for g in graphs.values():
        # 원본을 W×H로 바로 늘리는 scale은 없다 (흐림 배경의 1920:1080 확대는 이미 16:9로 자른 작은 캔버스에서만)
        for part in g.replace(";", ",").split(","):
            if part.startswith("scale=1920:1080") and "force_original_aspect_ratio" not in part:
                assert "flags=bicubic" in part and "crop=320:180" in g
        assert g.endswith("setsar=1,format=yuv420p,settb=AVTB[v]")


def test_image_segment_input_and_motion_continuity():
    c = img_clip(dur=30.0, motion=F.MOTION_ZOOM_IN, exif=6)
    spec = B.choose_output_spec([c.info])
    plan = B.build_bg_plan([c, B.Clip(vi("v.mp4", 20))], 120, 1.0, spec)
    body, x_out, _, x_in = plan.pieces
    assert body.label.startswith("사진") and body.a.still and body.a.pre == "transpose=1"
    cmd = B.piece_command(Path("ffmpeg"), body, spec, Path("o.mp4"))
    i = cmd.index("-loop")
    assert cmd[i + 1] == "1" and "-noautorotate" in cmd and cmd[cmd.index("-pattern_type") + 1] == "none"
    assert "-ss" not in cmd[:cmd.index("-i")]
    g = cmd[cmd.index("-filter_complex") + 1]
    assert "transpose=1" in g and "eval=frame" in g and "(in/30.000000+1.000000)/30.000000" in g
    # 본문은 1초 지점부터, 다음 장면으로 넘어가는 디졸브는 29초 지점부터, 앞 장면에서 들어오는 디졸브는 0초 → 움직임이 이어짐
    assert body.a.start == pytest.approx(1.0) and x_out.a.start == pytest.approx(29.0) and x_in.b.start == 0.0
    assert "(in/30.000000+29.000000)/30.000000" in " ".join(B.piece_command(Path("ffmpeg"), x_out, spec, Path("o.mp4")))
    assert F.motion_amount(30) == pytest.approx(0.06) and F.motion_amount(600) == F.MOTION_MAX
    assert F.motion_filter(F.MOTION_NONE, 1920, 1080, 0, 30) == ""
    for m in (F.MOTION_ZOOM_IN, F.MOTION_ZOOM_OUT, F.MOTION_PAN):   # 소수 화소 보간 (정수 crop 덜컥임 없음)
        f = F.motion_filter(m, 1920, 1080, 0, 30)
        assert f.startswith("perspective=") and "interpolation=cubic" in f and "crop" not in f
    assert "(W-W/1.06000)*min" in F.motion_filter(F.MOTION_PAN, 1920, 1080, 0, 30)
    assert "in/29.970030" in F.motion_filter(F.MOTION_ZOOM_IN, 1920, 1080, 0, 30, B.exact_fps(29.97))


def _jpeg_with_exif(raw: bytes, orient: int, big_endian=False) -> bytes:
    e = ">" if big_endian else "<"
    tiff = (b"MM" if big_endian else b"II") + struct.pack(e + "H", 42) + struct.pack(e + "I", 8) + struct.pack(e + "H", 1)
    tiff += struct.pack(e + "HHI", 0x0112, 3, 1) + struct.pack(e + "HH", orient, 0) + struct.pack(e + "I", 0)
    body = b"Exif\x00\x00" + tiff
    return raw[:2] + b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body + raw[2:]


def test_exif_orientation_parsing(tmp_path):
    fake_jpeg = b"\xff\xd8\xff\xdb\x00\x04\x00\x00" + b"\x00" * 64
    for o in range(1, 9):
        for be in (False, True):
            p = tmp_path / f"o{o}{be}.jpg"
            p.write_bytes(_jpeg_with_exif(fake_jpeg, o, be))
            assert F.read_exif_orientation(p) == o
    tiff = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 1) + struct.pack("<HHIHH", 0x0112, 3, 1, 8, 0) + b"\0" * 4
    png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(tiff)) + b"eXIf" + tiff + b"\0\0\0\0"
    (tmp_path / "a.png").write_bytes(png)
    assert F.read_exif_orientation(tmp_path / "a.png") == 8
    webp_chunk = b"EXIF" + struct.pack("<I", len(tiff)) + tiff
    webp = b"RIFF" + struct.pack("<I", 4 + len(webp_chunk)) + b"WEBP" + webp_chunk
    (tmp_path / "a.webp").write_bytes(webp)
    assert F.read_exif_orientation(tmp_path / "a.webp") == 8
    (tmp_path / "x.jpg").write_bytes(b"not an image")
    assert F.read_exif_orientation(tmp_path / "x.jpg") == 1
    assert F.read_exif_orientation(tmp_path / "없음.jpg") == 1
    assert F.display_size(1600, 1200, exif=6) == (1200, 1600) and F.display_size(1600, 1200, exif=3) == (1600, 1200)
    assert F.display_size(1440, 1080, sar=4 / 3) == (1920, 1080)
    assert F.display_size(1920, 1080, rotation=90) == (1080, 1920)
    assert F.MediaMeta(True, 1, 1, 1.0, 0, 6).pre_filter == "transpose=1"
    assert F.MediaMeta(False, 1, 1, 4 / 3, 90, 6).pre_filter.startswith("scale='trunc(iw*sar/2)*2'")  # 영상은 EXIF 무시


@pytest.mark.parametrize("text,want", [("30", 30.0), ("30초", 30.0), ("1:30", 90.0), (" 12.5 ", 12.5), ("600", 600.0)])
def test_parse_still_ok(text, want):
    assert F.parse_still(text) == want


@pytest.mark.parametrize("text", ["", "abc", "2", "601", "-5", "nan", "1:xx"])
def test_parse_still_rejects(text):
    with pytest.raises(ValueError):
        F.parse_still(text)


def test_plan_short_clips_plus_image_fifty_minutes_seven_seconds():
    clips = [B.Clip(vi("호수_세로_10초.mp4", 10, 1080, 1920)), B.Clip(vi("눈산_25초.mp4", 25)),
             B.Clip(vi("바다_35초.mp4", 35)), img_clip("카페_정사각.jpg", 1080, 1080, 30)]
    spec = B.choose_output_spec([c.info for c in clips])
    plan = B.build_bg_plan(clips, B.parse_length("00:50:07"), 1.0, spec)
    assert (spec.width, spec.height, spec.fps) == (1920, 1080, 30.0)
    assert plan.cycle_duration == pytest.approx(100 - 4)          # 디졸브 4번 (사진 → 첫 영상 포함)
    assert plan.target_frames == 3007 * 30 and plan.repeats == math.ceil(3007 / 96)
    assert [p.kind for p in plan.pieces] == ["body", "xfade"] * 4
    last = plan.pieces[-1]
    assert last.a.still and not last.b.still and last.b.path.name == "호수_세로_10초.mp4"  # 사진 → 영상 디졸브
    assert any("정지 이미지" in w for w in plan.warnings)
    assert any("과도한 반복" in w for w in plan.warnings)        # 같은 사진·짧은 장면이 32번
    only_img = B.build_bg_plan([img_clip()], 600, 1.0, spec)
    assert any("전부 사진" in w for w in only_img.warnings) and any("사진 1장" in w for w in only_img.warnings)


def test_measurement_and_seam_keep_fit_info():
    c = B.Clip(vi("세로.mp4", 20, 1080, 1920))
    spec = B.choose_output_spec([c.info])
    plan = B.build_bg_plan([c, img_clip()], 300, 1.0, spec)
    for p in B.measurement_pieces(plan):
        assert p.a.dw in (1080, 1200) and p.a.dh in (1920, 1600)
    a, b = B.seam_segments(c, img_clip(), 1.0)
    assert (a.dw, a.dh, b.still) == (1080, 1920, True)
    pspec = B.preview_spec_for(spec)
    assert (pspec.width, pspec.fit, pspec.blur) == (640, F.FIT_BLUR, spec.blur)
    cmd = " ".join(B.seam_preview_command(Path("ffmpeg"), a, b, 1.0, Path("p.mp4"), pspec))
    assert "gblur" in cmd and "-loop 1" in cmd
    assert B.preview_spec_for(None) is B.PREVIEW_SPEC


# ---------------- 실제 FFmpeg ----------------

def run(*a):
    subprocess.run([str(x) for x in a], check=True, capture_output=True)


def make_video(path: Path, w: int, h: int, dur: float, *, pattern="half", audio=True, fps="30", extra_vf=""):
    """pattern half: 위쪽 절반 흰색(세로) / 왼쪽 절반 흰색(가로) — 방향 확인용. color=<이름>: 단색."""
    if pattern.startswith("color="):
        src = f"color=c={pattern[6:]}:s={w}x{h}:r={fps}"
    elif pattern == "testsrc":
        src = f"testsrc2=s={w}x{h}:r={fps}"
    else:
        box = "w=iw:h=ih/2" if h > w else "w=iw/2:h=ih"
        src = f"color=c=black:s={w}x{h}:r={fps},drawbox=x=0:y=0:{box}:color=white:t=fill"
    vf = f"{extra_vf}," if extra_vf else ""
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", src]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=f=440"]
    cmd += ["-t", f"{dur}", "-vf", f"{vf}format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast"]
    cmd += ["-c:a", "aac", "-shortest"] if audio else []
    run(*cmd, path)
    return path


def make_image(path: Path, w: int, h: int, *, color=None, exif=None):
    src = f"color=c={color}:s={w}x{h}" if color else \
        f"color=c=black:s={w}x{h},drawbox=x=0:y=0:w=iw/2:h=ih:color=white:t=fill"
    run(FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", src, "-frames:v", "1", path)
    if exif:
        path.write_bytes(_jpeg_with_exif(path.read_bytes(), exif))
    return path


def gray(path_or_args, t: float | None = None, w=16, h=9) -> bytes:
    """FFmpeg가 w×h 회색으로 줄인 1장 (Python은 작은 숫자 배열만 본다)."""
    src = ["-ss", f"{t}", "-i", str(path_or_args)] if t is not None else ["-i", str(path_or_args)]
    return subprocess.run([FFMPEG, "-v", "error", *src, "-frames:v", "1", "-vf",
                           f"scale={w}:{h}:flags=area,format=gray", "-f", "rawvideo", "-"],
                          capture_output=True, check=True).stdout[:w * h]


def cols(g: bytes, c0: int, c1: int, w=16, h=9) -> float:
    return sum(g[y * w + x] for y in range(h) for x in range(c0, c1)) / (h * (c1 - c0))


def rows(g: bytes, r0: int, r1: int, x0: int, x1: int, w=16) -> float:
    return sum(g[y * w + x] for y in range(r0, r1) for x in range(x0, x1)) / ((r1 - r0) * (x1 - x0))


def probe_json(path: Path) -> dict:
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,profile,pix_fmt,width,height,sample_aspect_ratio,"
                          "display_aspect_ratio,r_frame_rate:format=format_name", "-of", "json", str(path)],
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
    return json.loads(out)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def leftovers(folder: Path):
    return [p.name for p in folder.iterdir()
            if p.name.startswith((B.TEMP_PREFIX, ".PLVM_TEMP_")) or p.name.endswith(".part.mp4")]


def clip_of(path: Path, still=F.DEFAULT_STILL, motion=F.MOTION_NONE) -> B.Clip:
    info, meta = F.probe_media(path, Path(FFPROBE), still)
    return B.Clip(info, meta=meta, motion=motion if meta.is_image else F.MOTION_NONE)


@pytest.fixture
def media(tmp_path):
    d = tmp_path / "원본_日本語_사진 100%"
    d.mkdir()
    return d


@needs_ffmpeg
def test_real_probe_every_ratio_rotation_sar_and_images(media):
    files = {
        "세로_숏츠.mp4": make_video(media / "세로_숏츠.mp4", 1080, 1920, 1),
        "가로.mp4": make_video(media / "가로.mp4", 1920, 1080, 1),
        "정사각.mp4": make_video(media / "정사각.mp4", 1080, 1080, 1),
        "4대3.mov": make_video(media / "4대3.mov", 1440, 1080, 1),
        "21대9.mkv": make_video(media / "21대9.mkv", 2560, 1080, 1),
        "애너모픽.mp4": make_video(media / "애너모픽.mp4", 1440, 1080, 1, extra_vf="setsar=4/3"),
    }
    rot = media / "회전_메타.mp4"
    run(FFMPEG, "-v", "error", "-y", "-display_rotation", "90", "-i", files["가로.mp4"], "-c", "copy", rot)
    jpg = make_image(media / "카페 사진.jpg", 1600, 1200, exif=6)
    png = make_image(media / "산 100%.png", 1200, 1200)
    want = {"세로_숏츠.mp4": (1080, 1920, "9:16 세로"), "가로.mp4": (1920, 1080, "16:9"),
            "정사각.mp4": (1080, 1080, "1:1 정사각형"), "4대3.mov": (1440, 1080, "4:3"),
            "21대9.mkv": (2560, 1080, "21:9 와이드"), "애너모픽.mp4": (1920, 1080, "16:9")}
    for name, (w, h, label) in want.items():
        info, meta = F.probe_media(files[name], Path(FFPROBE))
        assert (info.width, info.height, F.aspect_label(info.width, info.height)) == (w, h, label), name
        assert not meta.is_image and info.audio_codec == "aac"
    info, meta = F.probe_media(media / "애너모픽.mp4", Path(FFPROBE))
    assert meta.sar == pytest.approx(4 / 3) and "sar" in meta.pre_filter
    info, meta = F.probe_media(rot, Path(FFPROBE))
    assert (info.width, info.height, meta.rotation % 180) == (1080, 1920, 90)
    info, meta = F.probe_media(jpg, Path(FFPROBE), 30)
    assert (info.width, info.height, meta.exif_orientation, info.duration, info.fps) == (1200, 1600, 6, 30.0, 0.0)
    assert meta.is_image and info.audio_codec == "" and info.profile == F.STILL_TAG
    info, meta = F.probe_media(png, Path(FFPROBE), 10)
    assert (info.width, info.height, info.duration) == (1200, 1200, 10.0)
    webp = media / "w.webp"
    if subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x240", "-frames:v", "1",
                       str(webp)], capture_output=True).returncode == 0:
        info, meta = F.probe_media(webp, Path(FFPROBE))
        assert (info.width, info.height, info.video_codec) == (320, 240, "webp")
    bad = media / "가짜.jpg"
    bad.write_bytes(b"not an image at all")
    with pytest.raises(Exception):
        F.probe_media(bad, Path(FFPROBE))


@needs_ffmpeg
def test_real_fit_modes_rotation_and_exif_look(media):
    """세 방식의 실제 화면 (제작과 같은 필터): 흐림 배경은 양옆이 흐린 화면, 원본 전체는 양옆 단색, 꽉 채우기는 양옆도 원본.
    회전 메타데이터·EXIF 방향은 세로로 바로 서서 보인다 (위·아래가 다름 → 옆으로 누워 있지 않음)."""
    ff = Path(FFMPEG)
    portrait = clip_of(make_video(media / "세로.mp4", 360, 640, 2, pattern="color=orange"))
    looks = {}
    for fit in F.FIT_CHOICES:
        spec = B.choose_output_spec([portrait.info], fit=fit)
        p = media / f"pv_{fit}.png"
        p.write_bytes(B.fit_preview_png(ff, portrait, spec))
        looks[fit] = gray(p)
    center = cols(looks[F.FIT_BLUR], 7, 9)
    assert cols(looks[F.FIT_CONTAIN], 0, 3) < 5 and cols(looks[F.FIT_CONTAIN], 13, 16) < 5   # 검은 여백
    assert abs(cols(looks[F.FIT_CONTAIN], 7, 9) - center) < 6
    assert cols(looks[F.FIT_BLUR], 0, 3) > 40 and cols(looks[F.FIT_BLUR], 0, 3) < center   # 같은 화면, 살짝 어둡게
    assert abs(cols(looks[F.FIT_FILL], 0, 3) - center) < 6                                   # 꽉 채움
    white = B.choose_output_spec([portrait.info], fit=F.FIT_CONTAIN, pad="0xFFFFFF")
    (media / "w.png").write_bytes(B.fit_preview_png(ff, portrait, white))
    assert cols(gray(media / "w.png"), 0, 3) > 250
    crop = media / "crop.png"
    crop.write_bytes(B.crop_preview_png(ff, portrait, B.choose_output_spec([portrait.info], fit=F.FIT_FILL)))
    assert crop.read_bytes().startswith(b"\x89PNG")

    land = make_video(media / "가로_왼쪽흰색.mp4", 640, 360, 2)          # 왼쪽 절반 흰색
    rot = media / "회전.mp4"
    run(FFMPEG, "-v", "error", "-y", "-display_rotation", "90", "-i", land, "-c", "copy", rot)
    jpg = make_image(media / "회전사진.jpg", 640, 360, exif=6)         # EXIF 6: 시계 방향 90° → 왼쪽이 위로
    for src, top_white in ((clip_of(rot), None), (clip_of(jpg), True)):
        spec = B.choose_output_spec([src.info], fit=F.FIT_CONTAIN)
        p = media / f"r_{src.path.stem}.png"
        p.write_bytes(B.fit_preview_png(ff, src, spec))
        g = gray(p)
        assert cols(g, 0, 4) < 5 and cols(g, 12, 16) < 5                 # 세로로 서 있음 (양옆 여백)
        top, bottom = rows(g, 0, 4, 6, 10), rows(g, 5, 9, 6, 10)
        assert abs(top - bottom) > 150                                   # 위·아래가 다름
        if top_white is not None:
            assert (top > bottom) == top_white


@needs_ffmpeg
def test_real_mixed_1080p_blur_build_capcut_ready(media, tmp_path):
    """세로 영상 + EXIF 회전 JPG + 정사각형 PNG → 1920×1080 16:9 · 오디오 0 · H.264 High yuv420p · SAR 1:1 ·
    정확한 프레임 · 원본 보존 · 한글/일본어 경로 · 미리보기 = 최종 화면."""
    v = make_video(media / "호수_세로.mp4", 1080, 1920, 4, pattern="testsrc")
    j = make_image(media / "카페 사진.jpg", 1600, 1200, exif=6)
    p = make_image(media / "산 100%.png", 1080, 1080, color="teal")
    srcs = (v, j, p)
    before = {x: sha(x) for x in srcs}
    clips = [clip_of(v), clip_of(j, 5), clip_of(p, 5)]
    spec = B.choose_output_spec([c.info for c in clips])
    assert (spec.width, spec.height, spec.fit) == (1920, 1080, F.FIT_BLUR)
    target = B.parse_length("00:00:20.5")
    plan = B.build_bg_plan(clips, target, 1.0, spec)
    out_dir = tmp_path / "출력_結果"
    out = out_dir / B.default_bg_output_name(clips, 0, seconds=target)
    r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out, cancel_event=Event())
    assert r.frames == r.target_frames == plan.target_frames == math.ceil(20.5 * 30)
    assert (r.width, r.height) == (1920, 1080)
    info = probe_json(out)
    vs = [s for s in info["streams"] if s["codec_type"] == "video"]
    assert len(info["streams"]) == 1 and len(vs) == 1                     # 오디오 스트림 0개
    s = vs[0]
    assert (s["codec_name"], s["profile"], s["pix_fmt"]) == ("h264", "High", "yuv420p")
    assert s.get("sample_aspect_ratio", "1:1") == "1:1" and s.get("display_aspect_ratio", "16:9") == "16:9"
    assert s["r_frame_rate"] == "30/1" and "mp4" in info["format"]["format_name"]
    assert subprocess.run([FFMPEG, "-v", "error", "-i", str(out), "-f", "null", "-"],
                          capture_output=True, text=True).stderr.strip() == ""
    assert {x: sha(x) for x in srcs} == before and leftovers(out_dir) == []
    # 미리보기 = 최종: 정사각형 PNG 본문(움직임 없음) 가운데 프레임을 같은 크기로 비교
    t0 = sum(pc.length for pc in plan.pieces[:4])
    mid = t0 + plan.pieces[4].length / 2
    assert plan.pieces[4].a.path == p.resolve()
    pv = tmp_path / "pv.png"
    pv.write_bytes(B.fit_preview_png(Path(FFMPEG), clips[2], spec))
    a, b = gray(pv, w=48, h=27), gray(out, mid, w=48, h=27)
    assert sum(abs(x - y) for x, y in zip(a, b)) / len(a) < 3.0
    assert cols(gray(out, mid), 0, 3) > 20                                # 양옆은 흐린 배경 (검은 막대 아님)


@needs_ffmpeg
def test_real_short_10_25_35_plus_image_combo_boundaries(media, tmp_path):
    """10·25·35초 짧은 원본 + 정사각형 사진 30초 → 두 바퀴 + 마지막 바퀴 중간에서 정확히 끝. 장면 경계·사진↔영상 디졸브."""
    r10 = make_video(media / "빨강_세로_10초.mp4", 180, 320, 10, pattern="color=red")
    b25 = make_video(media / "파랑_25초.mp4", 320, 180, 25, pattern="color=blue")
    g35 = make_video(media / "초록_정사각_35초.mp4", 240, 240, 35, pattern="color=green")
    img = make_image(media / "흰색_사진.png", 300, 300, color="white")
    clips = [clip_of(r10), clip_of(b25), clip_of(g35), clip_of(img, 30)]
    spec = B.choose_output_spec([c.info for c in clips], fit=F.FIT_CONTAIN)   # 검은 여백 → 장면 판별이 쉬움
    target = 96 * 2 + 50.0
    plan = B.build_bg_plan(clips, target, 1.0, spec)
    assert plan.cycle_duration == pytest.approx(96.0)
    out = tmp_path / "combo.mp4"
    res = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out,
                                 cancel_event=Event())
    assert res.frames == res.target_frames == plan.target_frames and res.repeats == 3

    def rgb(t):
        raw = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t}", "-i", str(out), "-frames:v", "1", "-vf",
                              "crop=iw/8:ih/8:iw*7/16:ih*7/16,scale=1:1:flags=area,format=rgb24", "-f", "rawvideo", "-"],
                             capture_output=True, check=True).stdout
        return tuple(raw[:3])
    for cyc in (0, 96):
        assert rgb(cyc + 4)[0] > 200 and rgb(cyc + 4)[2] < 60            # 빨강 (세로)
        assert rgb(cyc + 20)[2] > 200                                     # 파랑
        assert rgb(cyc + 50)[1] > 100 and rgb(cyc + 50)[0] < 60          # 초록 (정사각)
        assert min(rgb(cyc + 80)) > 230                                   # 사진 (흰색)
    mix = rgb(95.5)                                                       # 사진 → 첫 영상 디졸브 한가운데
    assert 90 < mix[1] < 200 and mix[0] > 200
    end = rgb(target - 0.5)                                               # 3번째 바퀴 49.5초 = 초록 장면 안에서 끝
    assert end[1] > 100 and end[0] < 60
    assert not any(s for s in subprocess.run([FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries",
                                              "stream=index", "-of", "csv=p=0", str(out)],
                                             capture_output=True, text=True).stdout.split())


@needs_ffmpeg
def test_real_image_slow_zoom_is_smooth_across_pieces(media, tmp_path):
    img = media / "그라데이션.png"
    run(FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=800x600", "-frames:v", "1", img)
    c = clip_of(img, 12, F.MOTION_ZOOM_IN)
    other = clip_of(make_video(media / "v.mp4", 320, 180, 6, pattern="color=gray"))
    spec = B.choose_output_spec([c.info, other.info], fit=F.FIT_BLUR)
    plan = B.build_bg_plan([c, other], 17.0, 1.0, spec)
    out = tmp_path / "zoom.mp4"
    B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out, cancel_event=Event())
    fx = spec.fps

    def diff(t1, t2):
        a, b = gray(out, t1, 64, 36), gray(out, t2, 64, 36)
        return sum(abs(x - y) for x, y in zip(a, b)) / len(a)
    assert diff(0.2, 9.5) > 1.0                       # 실제로 움직임 (줌인)
    steps = [diff(t, t + 1 / fx) for t in (2.0, 4.0, 6.0, 8.0, 9.5)]
    assert max(steps) < 0.5                           # 매 프레임 아주 조금씩 (정수 화소 '덜컥임' 없음)
    edge = plan.pieces[0].length                      # 본문 → 디졸브 조각 경계 (사진 11초 지점)
    assert diff(edge - 1 / fx, edge) < 2.5            # 조각이 바뀌어도 튀지 않음 (디졸브 첫 1프레임 진행분만)


@needs_ffmpeg
def test_real_cancel_with_images_cleans_up(media, tmp_path):
    c = clip_of(make_image(media / "a.jpg", 1600, 1200, exif=6), 30)
    d = clip_of(make_video(media / "b.mp4", 1080, 1920, 20, pattern="testsrc"))
    spec = B.choose_output_spec([c.info, d.info])
    plan = B.build_bg_plan([c, d], 600, 1.0, spec)
    ev = Event()
    out = tmp_path / "out" / "x.mp4"
    with pytest.raises(BuildCancelled):
        B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out, cancel_event=ev,
                               progress_cb=lambda f, t: ev.set() if f > 0.02 else None)
    assert not out.exists() and leftovers(out.parent) == []


# ---------------- 화면 (GUI) ----------------

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


def test_window_settings_compat_and_defaults(tkroot):
    from app.bg_ui import BackgroundMakerWindow
    from app.settings import load_settings, update_settings
    update_settings(bg_maker={"dissolve": 1.0, "minutes": 10, "quality": "FHD"})   # 통합 07 이전 설정 (새 키 없음)
    w = BackgroundMakerWindow(tkroot, tools=lambda: (None, None))
    try:
        w.withdraw()
        assert w.fit_key() == F.FIT_BLUR and w.blur_key() == F.DEFAULT_BLUR and w.pad_key() == F.DEFAULT_PAD
        assert w.still_default_seconds() == 30.0 and w.motion_key(w.motion_default.get()) == F.MOTION_NONE
        assert not w.adv.winfo_manager()                     # 고급 옵션은 기본 숨김 (초보자는 기본값 그대로)
        w.show_advanced.set(True)
        w._toggle_advanced()
        assert w.adv.winfo_manager() == "pack"
        w.fit.set(F.FIT_CONTAIN)
        w.quality.set("HD")
        w._options_changed()
        saved = load_settings()["bg_maker"]
        assert saved["fit"] == F.FIT_CONTAIN and saved["quality"] == "HD" and saved["dissolve"] == 1.0
    finally:
        w.destroy()
    update_settings(bg_maker={"fit": "엉뚱한값", "blur": "x", "pad": 3, "still_seconds": "abc", "motion": None})
    w = BackgroundMakerWindow(tkroot, tools=lambda: (None, None))
    try:
        w.withdraw()
        assert (w.fit_key(), w.blur_key(), w.pad_key(), w.still_default_seconds()) == (
            F.FIT_BLUR, F.DEFAULT_BLUR, F.DEFAULT_PAD, 30.0)
    finally:
        w.destroy()


@needs_ffmpeg
def test_window_mixed_media_fit_preview_still_and_build(tkroot, media, tmp_path, quiet):
    from app.bg_ui import BackgroundMakerWindow
    from app.settings import load_settings
    v = make_video(media / "세로 호수.mp4", 360, 640, 6, pattern="testsrc")
    j = make_image(media / "카페 사진.jpg", 640, 480, exif=6)
    p = make_image(media / "산 100%.png", 400, 400, color="teal")
    w = BackgroundMakerWindow(tkroot, tools=lambda: (FFMPEG, FFPROBE))
    try:
        w.withdraw()
        w.still_default.set("10초")
        assert w.add_paths([v, j, p])
        assert pump(w, lambda: not w.loading and len(w.analyses) == 3)
        assert [c.is_image for c in w.clips] == [False, True, True]
        assert [c.info.duration for c in w.clips[1:]] == [10.0, 10.0]
        assert "사진 2장" in w.summary.get() and "9:16 세로" in w.fit_note.get()
        assert w.tree.set("1", "fps") == "사진" and "3:4 세로" in w.tree.set("1", "res")
        assert "정지 이미지" in w.warn_text.get()
        # 사진 선택 → 표시 시간·움직임 바꾸기 (다시 분석) + 무음 사본 버튼은 사진에서 비활성
        w.tree.selection_set("1")
        w._show_detail()
        w._refresh_buttons()
        assert "EXIF 방향 6" in w.detail.get() and str(w.btn_silent.cget("state")) == "disabled"
        w.still_sel.set("20")
        w.motion_sel.set(F.MOTION_LABELS[F.MOTION_ZOOM_IN])
        assert w._apply_still()
        assert pump(w, lambda: not w.loading and len(w.analyses) == 3)
        assert w.clips[1].info.duration == 20.0 and w.clips[1].motion == F.MOTION_ZOOM_IN
        w.still_sel.set("2")                                  # 너무 짧음 → 거부
        assert not w._apply_still() and any("표시 시간" in str(x) for x in quiet)
        # 화면 맞춤 미리보기 (꽉 채우기 → 잘림 표시 함께)
        w.fit.set(F.FIT_FILL)
        w._options_changed()
        assert "잘림" in w.fit_note.get()
        w.tree.selection_set("0")
        w._refresh_buttons()
        assert w.preview_fit()
        assert str(w.btn_start.cget("state")) == "disabled"  # 미리보기 중에는 제작 시작 불가 (FFmpeg 1개)
        assert pump(w, lambda: not w.fit_previewing, timeout=60)
        assert "꽉 채우기" in str(w.fit_img_result.cget("text")) and "잘림" in str(w.fit_img_source.cget("text"))
        w.fit.set(F.FIT_BLUR)
        w._options_changed()
        assert load_settings()["bg_maker"]["fit"] == F.FIT_BLUR
        # 음악 길이 직접 입력으로 짧게 제작
        w.custom_length.set("00:00:25")
        w._length_settled()
        w.outdir.set(str(tmp_path / "BG"))
        w._options_changed()
        assert w.plan is not None and w.plan.spec.fit == F.FIT_BLUR
        assert w.start(confirm=False)
        assert pump(w, lambda: not w.running, timeout=300)
        r = w.result
        assert r.frames == r.target_frames and r.output.exists() and r.width / r.height == pytest.approx(16 / 9)
    finally:
        w.destroy()


# ---------------- 장시간 (선택) ----------------

@needs_ffmpeg
@pytest.mark.skipif(os.environ.get("PLVM_LONG_TESTS") != "1",
                    reason="장시간 실제 제작(혼합 비율 50분 7초)은 PLVM_LONG_TESTS=1 일 때만 실행 — 짧은 합성 테스트로 대신 검증")
def test_real_long_mixed_fifty_minutes_seven_seconds(media, tmp_path):
    clips = [clip_of(make_video(media / "호수_세로_10초.mp4", 1080, 1920, 10, pattern="testsrc")),
             clip_of(make_video(media / "눈산_25초.mp4", 1920, 1080, 25, pattern="testsrc")),
             clip_of(make_video(media / "바다_35초.mp4", 1440, 1080, 35, pattern="testsrc")),
             clip_of(make_image(media / "카페_정사각.png", 1080, 1080, color="teal"), 30)]
    spec = B.choose_output_spec([c.info for c in clips])
    plan = B.build_bg_plan(clips, B.parse_length("00:50:07"), 1.0, spec)
    out = tmp_path / B.default_bg_output_name(clips, 0, seconds=plan.target)
    r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out, cancel_event=Event())
    assert r.frames == r.target_frames == 3007 * 30 and (r.width, r.height) == (1920, 1080)
    assert len(probe_json(out)["streams"]) == 1
