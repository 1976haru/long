"""통합 08 — 움직이는 풍경 영상 위에 STORY 주인공 이미지(투명 PNG) 자동 합성.

단위: 프리셋(기본 4개 · JSON 저장·복제·수정·삭제 · 손상 파일 보존), 스타일 호환, 배치(비율 유지·좌/중/우·하단 기준),
      등장 구간(디졸브 바깥·짧은 장면), 장면 지정(선택/전체/간격), 필터 그래프(16:9 기존 경로 그대로 · 알파 보존),
      라이브러리(사본 보관·원본 보존·목록에서 빼도 파일 유지), JPG 배경 제거 안내.
실제 FFmpeg: 투명 PNG 확인·불투명 PNG 거부, 실제 움직이는 MP4 위 합성(크리스마스·불꽃·야경), 크기·위치·투명도,
      페이드·느린 확대(덜컥임 없음), 미리보기 = 최종, 반복 경계·끝 조각 연속성, 사진 장면 회귀, 무음 MP4 + 음악 길이,
      한글·일본어 경로, GUI 흐름(등록 → 프리셋 → 적용 → 장면별 켜기/끄기 → 미리보기 → 제작 → 설정 보존).
"""
import hashlib
import json
import math
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from app import bg_core as B
from app import bg_fit as F
from app import bg_story as S
from app.core import VideoInfo

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")


def vi(name="a.mp4", dur=30.0, w=1920, h=1080, fps=30.0):
    return VideoInfo(Path(name).resolve(), dur, 1000, w, h, fps, "h264", "", "yuv420p", "High", 0, 0)


def fake_layer(style=None, crop=(100, 100, 200, 600), name="커플"):
    img = S.StoryImage(Path("커플.png").resolve(), 400, 800, crop, name, 0.7)
    return S.StoryLayer(img, style or S.StoryStyle(), "테스트")


# ---------------- 프리셋 · 스타일 ----------------

def test_builtin_presets_match_scenes():
    keys = {p.key: p for p in S.BUILTIN_PRESETS}
    assert {"christmas_couple", "fireworks_couple", "tokyo_night"} <= set(keys)
    xmas, fire, night = keys["christmas_couple"], keys["fireworks_couple"], keys["tokyo_night"]
    assert xmas.style.align in (S.ALIGN_LEFT, S.ALIGN_RIGHT) and xmas.style.fade >= 1.0     # 한쪽 + 부드러운 페이드
    assert fire.style.align == S.ALIGN_CENTER and fire.style.lift < 0 and fire.style.protect_top >= 0.5  # 하단 뒷모습
    assert night.style.align in (S.ALIGN_LEFT, S.ALIGN_RIGHT) and night.style.motion == S.SMOTION_ZOOM
    assert night.style.fade >= 1.5
    for p in S.BUILTIN_PRESETS:
        assert p.builtin and p.scene in S.TAGS and p.style == p.style.clamped()
        assert 20 <= p.style.duration <= 40 and 1.0 <= p.style.fade <= 2.0     # 권장: 20~40초 등장, 1~2초 페이드
        # 기본 배치는 화면을 다 가리지 않는다 (FHD 기준 30% 미만)
        pl = S.placement(fake_layer(p.style), 1920, 1080)
        assert pl.w * min(pl.h, 1080 - pl.visible_top) < 1920 * 1080 * 0.30
    fpl = S.placement(fake_layer(fire.style), 1920, 1080)
    assert fpl.visible_top >= 1080 * fire.style.protect_top and fpl.y + fpl.h > 1080   # 하단에서 잘린 뒷모습


def test_style_from_dict_is_compatible():
    st = S.StoryStyle.from_dict({"size": "0.5", "align": "엉뚱", "opacity": 7, "fade": -3, "unknown": 1,
                                 "motion": "fly", "feather": "x", "duration": float("nan")})
    assert st.size == 0.5 and st.align == S.StoryStyle().align and st.opacity == 1.0 and st.fade == 0.0
    assert st.motion == S.SMOTION_NONE and st.feather == S.StoryStyle().feather and st.duration == 30.0
    assert S.StoryStyle.from_dict(None) == S.StoryStyle()
    assert S.StoryStyle.from_dict(S.StoryStyle(size=0.4).to_dict()).size == 0.4


def test_preset_json_duplicate_edit_delete_and_builtin_protected(_isolated_settings):
    p = S.duplicate_preset("christmas_couple")
    assert p.key.startswith("user_") and "복사" in p.name and p.style == S.get_preset("christmas_couple").style
    p2 = S.save_preset(p.name, replace(p.style, size=0.5, align=S.ALIGN_LEFT), p.scene, key=p.key)   # 수정
    p3 = S.save_preset("내 불꽃", S.StoryStyle(lift=-0.2), "Fireworks")                               # 추가
    keys = [x.key for x in S.load_presets()]
    assert keys[:len(S.BUILTIN_PRESETS)] == [x.key for x in S.BUILTIN_PRESETS]
    assert p.key in keys and p3.key in keys and len(keys) == len(S.BUILTIN_PRESETS) + 2
    assert S.get_preset(p.key).style.size == 0.5 and S.get_preset(p.key).style.align == S.ALIGN_LEFT
    data = json.loads((_isolated_settings / S.PRESETS_FILE).read_text(encoding="utf-8"))
    assert data["version"] == 1 and all(d["key"].startswith("user_") for d in data["presets"])   # 기본 프리셋은 파일에 없음
    with pytest.raises(ValueError):
        S.save_preset("x", S.StoryStyle(), key="christmas_couple")
    assert not S.delete_preset("christmas_couple")
    assert S.delete_preset(p3.key) and p3.key not in [x.key for x in S.load_presets()]
    assert p2.key == p.key


def test_corrupt_json_is_preserved_not_deleted(_isolated_settings):
    f = _isolated_settings / S.PRESETS_FILE
    f.write_text("{망가진", encoding="utf-8")
    assert [p.key for p in S.load_presets()] == [p.key for p in S.BUILTIN_PRESETS]
    assert any(x.name.startswith(S.PRESETS_FILE + ".broken-") for x in _isolated_settings.iterdir())
    lib_dir = _isolated_settings / S.LIBRARY_DIR
    lib_dir.mkdir()
    (lib_dir / "keep.png").write_bytes(b"x")
    (lib_dir / S.LIBRARY_FILE).write_text("[[[", encoding="utf-8")
    lib = S.StoryLibrary()
    assert lib.entries == [] and (lib_dir / "keep.png").exists()
    assert any(x.name.startswith(S.LIBRARY_FILE + ".broken-") for x in lib_dir.iterdir())


# ---------------- 배치 · 등장 구간 · 장면 지정 ----------------

def test_placement_keeps_ratio_align_and_bottom():
    for W, H in ((1920, 1080), (640, 360), (3840, 2160)):
        for a in S.ALIGNS:
            pl = S.placement(fake_layer(S.StoryStyle(size=0.6, align=a, margin=0.05)), W, H)
            assert pl.h == round(H * 0.6) and abs(pl.w / pl.h - 200 / 600) < 0.01        # 원본 비율 유지 (crop 영역 기준)
            assert pl.y + pl.h == H                                                      # 화면 하단 기준
            if a == S.ALIGN_LEFT:
                assert pl.x == round(W * 0.05)
            elif a == S.ALIGN_RIGHT:
                assert pl.x + pl.w == W - round(W * 0.05)
            else:
                assert abs(pl.x + pl.w / 2 - W / 2) <= 1
    big, small = (S.placement(fake_layer(S.StoryStyle(lift=-0.2)), W, H) for W, H in ((1920, 1080), (640, 360)))
    assert big.y + big.h > 1080 and abs(big.y / 1080 - small.y / 360) < 0.01             # 미리보기 해상도에서도 같은 구도
    up = S.placement(fake_layer(S.StoryStyle(lift=0.1)), 1920, 1080)
    assert up.y + up.h == 1080 - 108
    sunk = S.placement(fake_layer(S.StoryStyle(size=1.0, lift=-0.6)), 1920, 1080)
    assert sunk.y <= 1080 - sunk.h // 5                                                  # 너무 내려도 위쪽은 보임


def test_story_window_outside_dissolves():
    st = S.StoryStyle(duration=30, fade=1.5, delay=1.0)
    w = S.story_window(st, 60.0, 1.0)
    assert (w.start, w.end, w.fade) == (2.0, 32.0, 1.5)                 # 디졸브(1초) 끝 + 1초 뒤부터 30초
    w = S.story_window(st, 10.0, 1.0)                                   # 10초 짧은 장면: 디졸브 바깥 전부
    assert w.start == pytest.approx(2.0) and w.end == pytest.approx(8.5) and w.fade == 1.5
    w = S.story_window(S.StoryStyle(delay=20), 12.0, 1.0)               # 지연이 너무 길면 앞쪽부터
    assert w.start == pytest.approx(1.5)
    w = S.story_window(S.StoryStyle(fade=3), 7.0, 1.0)
    assert w.fade == pytest.approx((w.end - w.start) / 3)               # 짧으면 페이드도 줄임
    assert S.story_window(st, 4.5, 1.0) is None                         # 2초 미만 → 넣지 않음


def test_assign_story_modes_keep_original_list():
    clips = [B.Clip(vi(f"{k}.mp4", 30)) for k in range(5)]
    clips[2].enabled = False
    layer = fake_layer()
    allc = S.assign_story(clips, layer, S.APPLY_ALL)
    assert [c.story is not None for c in allc] == [True, True, False, True, True]
    assert all(c.story is None for c in clips)                          # 원본 목록은 그대로
    sel = S.assign_story(clips, layer, S.APPLY_SELECTED, {1, 2})
    assert [c.story is not None for c in sel] == [False, True, False, False, False]
    iv = S.assign_story(clips, layer, S.APPLY_INTERVAL, every=2)        # 사용 중 장면 기준 1, 3번째
    assert [c.story is not None for c in iv] == [True, False, False, True, False]
    assert all(c.story is None for c in S.assign_story(allc, None))


def test_plan_pieces_and_filter_graph():
    layer = fake_layer(S.get_preset("christmas_couple").style)
    a, b = B.Clip(vi("거리.mp4", 40), story=layer), B.Clip(vi("夜景.mp4", 12))
    spec = B.choose_output_spec([a.info, b.info])
    plan = B.build_bg_plan([a, b], 120.0, 1.0, spec)
    body_a, x_ab, body_b, x_ba = plan.pieces
    assert B.story_active(body_a.a, body_a.length) and body_a.a.window.start == pytest.approx(2.0)
    assert not B.story_active(x_ab.a, x_ab.length) and not B.story_active(x_ba.b, x_ba.length)  # 디졸브에는 없음
    assert not B.story_active(body_b.a, body_b.length)
    cmd = B.piece_command(Path("ffmpeg"), body_a, spec, Path("o.mp4"))
    g = cmd[cmd.index("-filter_complex") + 1]
    assert cmd.count("-i") == 2 and "-pattern_type" in cmd and str(layer.image.path) in cmd
    for part in ("[1:v]format=rgba,crop=200:600:100:100", "premultiply=inplace=1", "unpremultiply=inplace=1",
                 "alphamerge", "fade=t=in:st=2.000000:d=1.500000:alpha=1", "loop=loop=-1:size=1",
                 "overlay=x=", ":format=yuv444:shortest=1", "colorchannelmixer="):
        assert part in g, part
    assert g.endswith("format=yuv420p,settb=AVTB[v]")
    # 주인공이 없는 장면 조각은 기존 16:9 필터 문자열 그대로
    plain = B.piece_command(Path("ffmpeg"), body_b, spec, Path("o.mp4"))
    assert plain[plain.index("-filter_complex") + 1] == f"[0:v]{B._norm_chain(spec, body_b.length)}[v]"
    # 느린 확대: 화면 전체(W×H)가 아니라 인물 주변 작은 캔버스에서만 perspective → 덜 느림
    zl = fake_layer(S.get_preset("tokyo_night").style)
    zplan = B.build_bg_plan([B.Clip(vi("z.mp4", 40), story=zl)], 60.0, 1.0, spec)
    zc = B.piece_command(Path("ffmpeg"), zplan.pieces[0], spec, Path("o.mp4"))
    zg = zc[zc.index("-filter_complex") + 1]
    assert "perspective=" in zg and "interpolation=cubic:eval=frame" in zg and "pad=1920:1080" not in zg
    pl = S.placement(zl, spec.width, spec.height)
    assert f"pad={pl.w + 2 * (math.ceil(pl.w * S.ZOOM_AMOUNT / 2) + 4)}:" in zg
    # 측정 조각(시작 위치를 바꾼 구간)도 장면 안 시간이 따라간다
    m = B.measurement_pieces(plan)[0]
    assert m.a.local0 == pytest.approx(m.a.start - a.used_start) and m.a.story is layer
    assert any("👤 주인공 이미지" in w for w in plan.warnings)


def test_story_notes_short_wrap_protect():
    fire = fake_layer(S.get_preset("fireworks_couple").style)
    clips = [B.Clip(vi("a.mp4", 30), story=fire), B.Clip(vi("b.mp4", 30)), B.Clip(vi("c.mp4", 4), story=fire)]
    notes = S.story_notes(clips, 1.0, 30.0)
    assert any("짧아 주인공을 넣지 못하는" in n and "c.mp4" in n for n in notes)
    assert any("연달아" in n for n in notes)
    tall = fake_layer(replace(fire.style, size=0.9, lift=0.0))
    assert any("불꽃 공간" in n for n in S.story_notes([B.Clip(vi("a.mp4", 30), story=tall)], 1.0, 30.0))
    huge = fake_layer(S.StoryStyle(size=1.1), crop=(0, 0, 400, 400))
    assert any("30% 이상" in n for n in S.story_notes([B.Clip(vi("a.mp4", 30), story=huge)], 1.0, 30.0))


def test_jpg_needs_background_removal_without_ffmpeg(tmp_path):
    jpg = tmp_path / "커플 사진.jpg"
    jpg.write_bytes(b"\xff\xd8\xff")
    with pytest.raises(S.StoryImageError) as e:
        S.inspect_image(Path("ffmpeg"), Path("ffprobe"), jpg)
    assert "배경 제거가 필요" in str(e.value) and "투명 PNG" in str(e.value)
    with pytest.raises(S.StoryImageError):
        S.inspect_image(Path("ffmpeg"), Path("ffprobe"), tmp_path / "x.gifv")


# ---------------- 실제 FFmpeg ----------------

def run(*a):
    subprocess.run([str(x) for x in a], check=True, capture_output=True)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def make_person(path: Path, w=400, h=800, color="orange"):
    """투명 배경 인물(몸통 사각형 + 둥근 머리, 바깥 여백 투명) — RGBA PNG."""
    a = (f"255*(between(X,{w * 0.3:.0f},{w * 0.7:.0f})*between(Y,{h * 0.31:.0f},{h * 0.96:.0f})"
         f"+lt(hypot(X-{w / 2:.0f},Y-{h * 0.22:.0f}),{w * 0.15:.0f}))")
    run(FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
        f"color=c={color}:s={w}x{h},format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{a}'", "-frames:v", "1", path)
    return path


def make_moving(path: Path, dur: float, w=640, h=360, bg="navy"):
    """실제로 움직이는 MP4: 배경색 위로 흰 상자가 위쪽 띠(y 20~60)를 계속 가로지른다 (+ 오디오 트랙)."""
    run(FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={bg}:s={w}x{h}:r=30", "-f", "lavfi", "-i",
        f"color=c=white:s=40x40:r=30", "-f", "lavfi", "-i", "sine=f=330", "-filter_complex",
        f"[0][1]overlay=x='mod(t*120,{w - 40})':y=20,format=yuv420p[v]", "-map", "[v]", "-map", "2:a",
        "-t", f"{dur}", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", path)
    return path


def rgb(src, t: float | None, box=(0.0, 0.0, 1.0, 1.0)) -> tuple[int, int, int]:
    """FFmpeg가 영역 평균을 1화소로 줄인 RGB (Python은 3바이트만 본다). box = x, y, w, h (화면 비율)."""
    x, y, w, h = box
    pre = ["-ss", f"{t:.4f}"] if t is not None else []
    raw = subprocess.run([FFMPEG, "-v", "error", *pre, "-i", str(src), "-frames:v", "1", "-vf",
                          f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},scale=1:1:flags=area,format=rgb24", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    return tuple(raw[:3])


def gray(src, t: float | None, w=48, h=27) -> bytes:
    pre = ["-ss", f"{t:.4f}"] if t is not None else []
    return subprocess.run([FFMPEG, "-v", "error", *pre, "-i", str(src), "-frames:v", "1", "-vf",
                           f"scale={w}:{h}:flags=area,format=gray", "-f", "rawvideo", "-"],
                          capture_output=True, check=True).stdout[:w * h]


def diff(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def is_orange(c) -> bool:
    return c[0] > 150 and 60 < c[1] < 200 and c[2] < 90


def is_navy(c) -> bool:
    return c[0] < 40 and c[1] < 40 and c[2] > 80


def clip_of(path: Path) -> B.Clip:
    info, meta = F.probe_media(path, Path(FFPROBE))
    return B.Clip(info, meta=meta)


def body_offset(plan, k: int) -> float:
    """k번째 장면 본문 조각이 한 바퀴에서 시작하는 시각 (= 장면 안 시간 d)."""
    return sum(p.length for p in plan.pieces[:2 * k])


def leftovers(folder: Path):
    return [p.name for p in folder.iterdir()
            if p.name.startswith((B.TEMP_PREFIX, ".PLVM_TEMP_")) or p.name.endswith(".part.mp4")]


def probe_streams(path: Path) -> list[dict]:
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt,width,height",
                          "-of", "json", str(path)], capture_output=True, text=True, encoding="utf-8", check=True).stdout
    return json.loads(out)["streams"]


@pytest.fixture
def media(tmp_path):
    d = tmp_path / "주인공_日本語_素材 100%"
    d.mkdir()
    return d


@needs_ffmpeg
def test_real_inspect_transparent_png_and_reject_opaque(media):
    png = make_person(media / "남녀 주인공_恋人.png")
    img = S.inspect_image(Path(FFMPEG), Path(FFPROBE), png)
    assert (img.width, img.height) == (400, 800) and 0.5 < img.transparent < 0.9
    x, y, w, h = img.crop                                             # 보이는 영역 (투명 여백 제외, 약간 여유)
    assert 100 <= x <= 120 and 80 <= y <= 115 and 160 <= w <= 200 and 640 <= h <= 720
    assert S.thumb_png(Path(FFMPEG), img).startswith(b"\x89PNG")
    opaque = media / "배경있음.png"
    run(FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=200x300", "-frames:v", "1", opaque)
    with pytest.raises(S.StoryImageError) as e:
        S.inspect_image(Path(FFMPEG), Path(FFPROBE), opaque)
    assert "배경 제거가 필요" in str(e.value)
    empty = media / "투명뿐.png"
    run(FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
        "color=c=red:s=64x64,format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='0'", "-frames:v", "1", empty)
    with pytest.raises(S.StoryImageError):
        S.inspect_image(Path(FFMPEG), Path(FFPROBE), empty)


@needs_ffmpeg
def test_library_keeps_original_and_copies(media, _isolated_settings):
    png = make_person(media / "커플.png")
    before = sha(png)
    img = S.inspect_image(Path(FFMPEG), Path(FFPROBE), png)
    lib = S.StoryLibrary()
    assert lib.folder == _isolated_settings / S.LIBRARY_DIR                # 설정 폴더 (테스트에서는 격리 폴더)
    e = lib.add(img, "Mika & Ren", ["Christmas", "Night", "없는태그"])
    assert e.tags == ["Christmas", "Night"] and lib.available(e)
    copy = lib.folder / e.file
    assert sha(copy) == before and copy != png
    lib.update(e.id, style=S.StoryStyle(size=0.5, align=S.ALIGN_LEFT), preset="tokyo_night", name="미카와 렌")
    again = S.StoryLibrary()                                               # 다시 열어도 그대로 (다음에도 바로 선택)
    e2 = again.get(e.id)
    assert e2.name == "미카와 렌" and e2.preset == "tokyo_night" and S.StoryStyle.from_dict(e2.style).size == 0.5
    assert again.image_of(e2).crop == img.crop
    assert again.remove(e.id) and copy.exists() and png.exists() and sha(png) == before   # 사본·원본 모두 남음


@needs_ffmpeg
def test_real_christmas_on_moving_mp4_silent_exact_length(media, tmp_path):
    """실제로 움직이는 MP4 2개 + 크리스마스 프리셋(오른쪽) → 무음 MP4, 음악 길이(프레임 정확), 원본 보존, 등장·디졸브."""
    v1 = make_moving(media / "크리스마스 거리.mp4", 14)
    v2 = make_moving(media / "イルミネーション.mp4", 12, bg="darkgreen")
    png = make_person(media / "커플.png")
    before = {p: sha(p) for p in (v1, v2, png)}
    img = S.inspect_image(Path(FFMPEG), Path(FFPROBE), png)
    layer = S.layer_for(img, S.get_preset("christmas_couple"))
    clips = S.assign_story([clip_of(v1), clip_of(v2)], layer, S.APPLY_SELECTED, {0})
    spec = B.choose_output_spec([c.info for c in clips])
    target = B.parse_length("00:00:31.5")
    plan = B.build_bg_plan(clips, target, 1.0, spec)
    out = tmp_path / "출력_結果" / B.default_bg_output_name(clips, 0, seconds=target)
    r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out, cancel_event=Event())
    assert r.frames == r.target_frames == math.ceil(31.5 * 30)
    st = probe_streams(out)
    assert [s["codec_type"] for s in st] == ["video"] and st[0]["pix_fmt"] == "yuv420p"     # 무음 (오디오 0개)
    assert (st[0]["width"], st[0]["height"]) == (640, 360)
    assert {p: sha(p) for p in before} == before and leftovers(out.parent) == []
    w = S.clip_window(clips[0], 1.0, spec.fps)
    pl = S.placement(layer, spec.width, spec.height)
    right = ((pl.x + pl.w * 0.3) / 640, (pl.y + pl.h * 0.5) / 360, pl.w * 0.4 / 640, pl.h * 0.3 / 360)
    left = (0.1, 0.5, 0.2, 0.3)
    T = lambda local: local - 1.0                                          # 장면 1 본문은 한 바퀴 시작 = 장면 안 1초
    assert is_orange(rgb(out, T(w.start + w.fade + 1), right))             # 오른쪽에 인물
    assert is_navy(rgb(out, T(w.start + w.fade + 1), left))                # 왼쪽 거리 풍경은 그대로
    assert is_navy(rgb(out, T(w.start - 0.4), right))                      # 등장 전 없음
    assert is_navy(rgb(out, T(w.end + 0.2), right))                        # 페이드아웃 뒤 없음 (다음 디졸브 전)
    mid = rgb(out, T(w.start + w.fade / 2), right)
    assert not is_orange(mid) and mid[0] > 60                              # 페이드 중간: 반쯤 보임
    x_ab = plan.pieces[0].length
    assert is_navy(rgb(out, x_ab + 0.5, right)) or rgb(out, x_ab + 0.5, right)[1] > 40   # 디졸브 중 인물 없음
    assert rgb(out, x_ab + 0.5, right)[0] < 60
    cyc = plan.cycle_duration
    second = body_offset(plan, 1) + 3.0                                    # 장면 2 (주인공 없음)
    assert rgb(out, second, right)[0] < 60
    assert is_orange(rgb(out, cyc + T(w.start + w.fade + 1), right))      # 두 번째 바퀴에서도 같은 때 등장


@needs_ffmpeg
def test_real_fireworks_and_tokyo_presets(media, tmp_path):
    """불꽃: 하단 가운데 뒷모습, 위쪽 하늘(움직이는 불꽃 자리)은 주인공 없는 결과와 똑같음.
    야경: 왼쪽 + 아주 느린 확대 — 실제로 커지지만 매 프레임 덜컥이지 않음."""
    v = make_moving(media / "花火大会.mp4", 30, bg="black")
    png = make_person(media / "뒷모습.png")
    img = S.inspect_image(Path(FFMPEG), Path(FFPROBE), png)
    base = clip_of(v)
    spec = B.choose_output_spec([base.info])
    outs = {}
    for key in ("none", "fireworks_couple", "tokyo_night"):
        clip = base if key == "none" else replace(base, story=S.layer_for(img, S.get_preset(key)))
        plan = B.build_bg_plan([clip], 28.0, 1.0, spec)       # 등장 구간(장면 안 2.5~28.5초) 전체가 들어가게
        outs[key] = tmp_path / f"{key}.mp4"
        B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=outs[key],
                               cancel_event=Event())
    fire = replace(base, story=S.layer_for(img, S.get_preset("fireworks_couple")))
    w = S.clip_window(fire, 1.0, spec.fps)
    t = w.start + w.fade + 2 - 1.0
    sky = (0.0, 0.0, 1.0, 0.5)
    top = 64 * 9                                                           # 64×18 중 위쪽 9줄 = 화면 위쪽 절반
    assert diff(gray(outs["none"], t, 64, 18)[:top], gray(outs["fireworks_couple"], t, 64, 18)[:top]) < 0.3
    raw_top = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t}", "-i", str(outs["fireworks_couple"]), "-frames:v", "1",
                              "-vf", "crop=iw:ih*0.6:0:0,scale=64:20:flags=area,format=gray", "-f", "rawvideo", "-"],
                             capture_output=True, check=True).stdout
    assert max(raw_top[64 * 3:]) < 60 or rgb(outs["fireworks_couple"], t, sky)[0] < 40          # 인물은 위쪽에 없음
    c = rgb(outs["fireworks_couple"], t, (0.46, 0.85, 0.08, 0.12))
    assert c[0] > 90 and c[0] > c[2] + 40                                  # 하단 가운데에 (어둡게 맞춘) 인물
    assert c[0] < 200                                                      # 밝기 보정 (원래 주황 255보다 어둡게)
    assert rgb(outs["fireworks_couple"], t, (0.05, 0.85, 0.15, 0.1))[0] < 30   # 하단 양옆은 풍경
    # 야경: 왼쪽, 아주 느린 확대
    night = replace(base, story=S.layer_for(img, S.get_preset("tokyo_night")))
    nw = S.clip_window(night, 1.0, spec.fps)
    pl = S.placement(night.story, spec.width, spec.height)
    assert pl.x < 640 * 0.2
    o = outs["tokyo_night"]
    t0, t1 = nw.start + nw.fade + 0.2 - 1.0, nw.end - nw.fade - 0.2 - 1.0
    head_edge = (pl.x / 640, (pl.y - pl.h * 0.02) / 360, pl.w / 640, pl.h * 0.12 / 360)
    assert diff(gray(o, t0, 64, 36), gray(o, t1, 64, 36)) > 0.15                # 실제로 변함 (확대)
    fx = spec.fps
    steps = []
    for t in (t0 + 2, t0 + 6, t0 + 10):
        a = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t:.4f}", "-i", str(o), "-frames:v", "2", "-vf",
                            f"crop=iw*0.4:ih*0.75:0:ih*0.25,scale=64:68:flags=area,format=gray", "-f", "rawvideo", "-"],
                           capture_output=True, check=True).stdout
        steps.append(diff(a[:64 * 68], a[64 * 68:2 * 64 * 68]))   # 움직이는 상자(위쪽 띠)를 뺀 인물 영역
    assert max(steps) < 0.6                                                # 매 프레임 아주 조금씩 (정수 화소 덜컥임 없음)
    assert head_edge[0] >= 0


@needs_ffmpeg
def test_real_size_position_opacity_and_preview_matches_output(media, tmp_path):
    v = make_moving(media / "夜の街.mp4", 16)
    png = make_person(media / "커플.png")
    img = S.inspect_image(Path(FFMPEG), Path(FFPROBE), png)
    base = clip_of(v)
    spec = B.choose_output_spec([base.info])
    looks = {}
    for name, st in {"small_left": S.StoryStyle(size=0.4, align=S.ALIGN_LEFT, fade=1.0, feather=0),
                     "big_center": S.StoryStyle(size=0.8, align=S.ALIGN_CENTER, fade=1.0),
                     "half_right": S.StoryStyle(size=0.6, align=S.ALIGN_RIGHT, opacity=0.5, fade=1.0)}.items():
        c = replace(base, story=S.StoryLayer(img, st, name))
        p = tmp_path / f"{name}.png"
        p.write_bytes(B.story_preview_png(Path(FFMPEG), c, spec, 1.0, size=(spec.width, spec.height)))
        looks[name] = (c, p)
    def cover(p, box):
        return rgb(p, None, box)

    def body(name):  # 인물 몸통 가운데 (배치 계산값 기준, 화면 비율)
        c = looks[name][0]
        pl = S.placement(c.story, spec.width, spec.height)
        return ((pl.x + pl.w * 0.42) / spec.width, (pl.y + pl.h * 0.6) / spec.height, pl.w * 0.16 / spec.width,
                pl.h * 0.15 / spec.height)
    small = S.placement(looks["small_left"][0].story, spec.width, spec.height)
    assert small.x + small.w < spec.width * 0.2 and small.y > spec.height * 0.55
    assert is_orange(cover(looks["small_left"][1], body("small_left")))
    assert is_navy(cover(looks["small_left"][1], (0.05, 0.3, 0.1, 0.1)))          # 작은 인물은 위쪽까지 오지 않음
    assert is_navy(cover(looks["small_left"][1], (0.8, 0.75, 0.1, 0.1)))
    assert is_orange(cover(looks["big_center"][1], body("big_center")))
    big = S.placement(looks["big_center"][0].story, spec.width, spec.height)
    assert big.h == 2 * small.h and abs(big.x + big.w / 2 - spec.width / 2) <= 1
    half = cover(looks["half_right"][1], body("half_right"))
    assert 90 < half[0] < 190 and half[2] > 40                                   # 50% 투명: 주황과 남색이 섞임
    # 미리보기 = 최종: 같은 해상도의 실제 결과 프레임과 비교 (가운데 큰 인물)
    c, pv = looks["big_center"]
    plan = B.build_bg_plan([c], 14.0, 1.0, spec)
    out = tmp_path / "pv_check.mp4"
    B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out, cancel_event=Event())
    local = B.story_preview_time(c, 1.0, spec.fps) - c.used_start
    assert diff(gray(pv, None), gray(out, local - 1.0)) < 3.0


@needs_ffmpeg
def test_real_repeat_boundary_and_tail_have_no_jump(media, tmp_path):
    """영상 1개(12초) 반복 + 주인공: 바퀴마다 같은 시각에 같은 화면, 반복 경계·조각 경계에서 튀지 않음,
    목표가 등장 구간 안에서 끝나도(끝 조각 다시 인코딩) 같은 화면."""
    v = make_moving(media / "루프.mp4", 12)
    png = make_person(media / "커플.png")
    img = S.inspect_image(Path(FFMPEG), Path(FFPROBE), png)
    c = replace(clip_of(v), story=S.StoryLayer(img, S.StoryStyle(size=0.6, align=S.ALIGN_RIGHT, fade=1.0,
                                                                 motion=S.SMOTION_DRIFT), "drift"))
    spec = B.choose_output_spec([c.info])
    plan = B.build_bg_plan([c], 30.0, 1.0, spec)
    cyc = plan.cycle_duration
    assert cyc == pytest.approx(11.0)                                      # 영상 1개: 본문 10초 + 디졸브 1초
    out = tmp_path / "loop.mp4"
    r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out, cancel_event=Event())
    assert r.frames == r.target_frames and r.tail_frames > 0
    w = S.clip_window(c, 1.0, spec.fps)
    fx = spec.fps
    end_local = (30.0 - 2 * cyc) + 1.0                                     # 세 번째 바퀴에서 장면 안 9초에 끝남
    assert w.start < end_local < w.end                                     # 등장 구간 안에서 끝 (끝 조각 다시 인코딩)
    for local in (w.start + 0.3, w.start + w.fade + 1, 6.0, w.end - 0.3):
        a, b = gray(out, local - 1.0, 64, 36), gray(out, cyc + local - 1.0, 64, 36)
        assert diff(a, b) < 1.2                                            # 바퀴마다 같은 시각 = 같은 화면 (초기화 없음)
        if 2 * cyc + local - 1.0 < 30.0 - 1 / fx:
            assert diff(a, gray(out, 2 * cyc + local - 1.0, 64, 36)) < 1.2
    raw = subprocess.run([FFMPEG, "-v", "error", "-sseof", "-0.5", "-i", str(out), "-vf",
                          "scale=64:36:flags=area,format=gray", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    last = raw[-64 * 36:]                                                  # 완성 파일의 마지막 프레임
    t_last = (r.frames - 1) / fx
    assert diff(last, gray(out, t_last - 2 * cyc, 64, 36)) < 1.5           # 끝 조각 = 앞 바퀴 같은 프레임
    # 반복 경계: 한 바퀴 끝 → 다음 바퀴 첫 프레임 변화가 평소 프레임 변화와 비슷 (인물 점프 없음)
    edge = diff(gray(out, cyc - 1 / fx, 64, 36), gray(out, cyc, 64, 36))
    normal = diff(gray(out, 3.0, 64, 36), gray(out, 3.0 + 1 / fx, 64, 36))
    assert edge < normal + 1.5
    right = (0.8, 0.6, 0.1, 0.2)
    assert rgb(out, cyc - 0.3, right)[0] < 60 and rgb(out, cyc + 0.3, right)[0] < 60    # 경계에서는 인물 없음 (디졸브 바깥)


@needs_ffmpeg
def test_real_story_on_photo_scene_and_photo_only_regression(media, tmp_path):
    photo = media / "카페 사진.png"
    run(FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=teal:s=800x600", "-frames:v", "1", photo)
    png = make_person(media / "커플.png")
    img = S.inspect_image(Path(FFMPEG), Path(FFPROBE), png)
    info, meta = F.probe_media(photo, Path(FFPROBE), 12)
    still = B.Clip(info, meta=meta, motion=F.MOTION_ZOOM_IN)
    spec = B.choose_output_spec([still.info])
    plain = tmp_path / "photo_only.mp4"                                    # 기존 ④: 사진만 (주인공 없음)
    plan0 = B.build_bg_plan([still], 15.0, 1.0, spec)
    assert not any("👤" in w for w in plan0.warnings)
    r0 = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan0, output=plain, cancel_event=Event())
    assert r0.frames == r0.target_frames
    withc = tmp_path / "photo_story.mp4"
    c = replace(still, story=S.layer_for(img, S.get_preset("cafe_story")))
    plan = B.build_bg_plan([c], 15.0, 1.0, spec)
    r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=withc, cancel_event=Event())
    assert r.frames == r.target_frames
    w = S.clip_window(c, 1.0, spec.fps)
    t = w.start + w.fade + 1 - 1.0
    pl = S.placement(c.story, spec.width, spec.height)
    box = ((pl.x + pl.w * 0.3) / spec.width, (pl.y + pl.h * 0.5) / spec.height, 0.03, 0.05)
    assert rgb(withc, t, box)[0] > 150 and rgb(plain, t, box)[0] < 60     # 사진 장면 위에도 겹침 / 기존 결과는 그대로
    assert diff(gray(plain, t, 16, 9)[:9 * 4], gray(withc, t, 16, 9)[:9 * 4]) < 1.0


@needs_ffmpeg
def test_real_cancel_cleans_story_build(media, tmp_path):
    v = make_moving(media / "a.mp4", 20, w=1280, h=720)
    img = S.inspect_image(Path(FFMPEG), Path(FFPROBE), make_person(media / "p.png"))
    c = replace(clip_of(v), story=S.layer_for(img, S.get_preset("tokyo_night")))
    spec = B.choose_output_spec([c.info])
    plan = B.build_bg_plan([c], 300, 1.0, spec)
    ev = Event()
    out = tmp_path / "o" / "x.mp4"
    from app.core import BuildCancelled
    with pytest.raises(BuildCancelled):
        B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out, cancel_event=ev,
                               progress_cb=lambda f, t: ev.set() if f > 0.01 else None)
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


def test_window_without_story_keeps_old_settings(tkroot):
    from app.bg_ui import BackgroundMakerWindow
    from app.settings import load_settings, update_settings
    update_settings(bg_maker={"dissolve": 1.0, "minutes": 10, "quality": "FHD", "fit": "blur"})  # 통합 08 이전 설정
    w = BackgroundMakerWindow(tkroot, tools=lambda: (None, None))
    try:
        w.withdraw()
        assert w.story_last == {} and w.story_layer is None
        assert str(w.btn_story.cget("text")) == "＋ 주인공 이미지 넣기"
        w._save_settings()
        saved = load_settings()["bg_maker"]
        assert saved["fit"] == "blur" and saved["story"] == {}
    finally:
        w.destroy()


@needs_ffmpeg
def test_window_story_flow_register_apply_toggle_preview_build(tkroot, media, tmp_path, quiet, _isolated_settings):
    from app.bg_ui import BackgroundMakerWindow
    from app.settings import load_settings
    v1 = make_moving(media / "크리스마스 거리.mp4", 12)
    v2 = make_moving(media / "東京タワー夜景.mp4", 12, bg="darkgreen")
    png = make_person(media / "주인공 커플.png")
    jpg = media / "배경있는 사진.jpg"
    run(FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=200x300", "-frames:v", "1", jpg)
    before = sha(png)
    w = BackgroundMakerWindow(tkroot, tools=lambda: (FFMPEG, FFPROBE))
    w.quiet_errors = True
    try:
        w.withdraw()
        assert w.add_paths([v1, v2])
        assert pump(w, lambda: not w.loading and len(w.clips) == 2)
        w.tree.selection_set("0")
        w._refresh_buttons()
        assert str(w.btn_story_toggle.cget("state")) == "disabled"      # 아직 적용한 주인공 없음
        d = w.open_story()
        d.withdraw()
        assert w.open_story() is d                                       # 이미 열린 창 재사용
        # JPG → 배경 제거 안내 (등록하지 않음)
        assert not d.register_path(jpg)
        assert any("배경 제거" in str(x) for x in quiet) and d.lib.entries == []
        # 투명 PNG 등록 → 라이브러리 사본
        d.tags["Christmas"].set(True)
        assert d.register_path(png, "Mika & Ren")
        assert pump(d, lambda: len(d.lib.entries) == 1 and not d.busy)
        e = d.lib.entries[0]
        assert e.name == "Mika & Ren" and e.tags == ["Christmas"] and d.entry_id.get() == e.id
        # 프리셋 선택 → 값 반영
        d.choose_preset("fireworks_couple")
        assert d.style().align == S.ALIGN_CENTER and d.style().lift < 0
        d.choose_preset("christmas_couple")
        d.nums["size"].set("55")
        assert d.style().size == pytest.approx(0.55)
        # 미리보기 (1장면) — 실제 제작과 같은 필터
        assert d.preview_still()
        assert pump(d, lambda: not d.busy, timeout=90)
        assert "주인공이 완전히 보이는" in str(d.pv.cget("text"))
        # 적용: 선택한 장면에만 (첫 장면)
        d.mode.set(S.APPLY_SELECTED)
        d.lst_scenes.selection_clear(0, "end")
        d.lst_scenes.selection_set(0)
        assert d.apply()
        assert [c.story is not None for c in w.clips] == [True, False]
        assert w.tree.set("0", "story") == "👤" and w.tree.set("1", "story") == ""
        assert any("👤 주인공 이미지: 1/2" in x for x in w.warn_text.get().splitlines())
        assert w.clips[0].story.style.size == pytest.approx(0.55)
        w.tree.selection_set("0")
        w._show_detail()
        assert "👤 주인공: Mika & Ren" in w.detail.get()
        # 장면별 켜기/끄기 (④ 목록)
        w.tree.selection_set("1")
        w._refresh_buttons()
        assert str(w.btn_story_toggle.cget("state")) == "normal"
        assert w.toggle_story() and w.clips[1].story is not None
        assert w.toggle_story() and w.clips[1].story is None
        # 등장·움직임 미리보기 (저화질 영상)
        d.scene.set(d.cb_scene.cget("values")[0])
        assert d.preview_motion()
        assert pump(d, lambda: not d.busy, timeout=90)
        assert d.last_preview is not None and d.last_preview.exists()
        saved = load_settings()["bg_maker"]["story"]
        assert saved["entry"] == e.id and saved["preset"] == "christmas_couple" and saved["mode"] == S.APPLY_SELECTED
        # 음악 길이 직접 입력 → 제작
        w.minutes.set(0)
        w.custom_length.set("00:00:25")
        w._length_settled()
        w.outdir.set(str(tmp_path / "BG_出力"))
        w._options_changed()
        assert w.plan is not None and B.story_active(w.plan.pieces[0].a, w.plan.pieces[0].length)
        assert w.start(confirm=False)
        assert pump(w, lambda: not w.running, timeout=300)
        r = w.result
        assert r.frames == r.target_frames == 750 and r.output.exists()
        assert [s["codec_type"] for s in probe_streams(r.output)] == ["video"]
        d.destroy()
        # 다시 열면 마지막 이미지·프리셋이 선택되어 있음 (사용자 이미지·설정 보존)
        d2 = w.open_story()
        d2.withdraw()
        assert d2.entry_id.get() == e.id and d2.current_preset().key == "christmas_couple"
        assert d2.mode.get() == S.APPLY_SELECTED
        d2.clear_all()
        assert all(c.story is None for c in w.clips)
    finally:
        w.destroy()
    assert sha(png) == before and (S.StoryLibrary().folder / e.file).exists()
