"""통합 08 UX HOTFIX — 초보자 3단계 주인공 합성.

단위: 사진 그대로 넣기 배치(면적 상한·살짝 띄움), 예전 라이브러리 JSON 호환, 한국어 프리셋 이름(내부 키 유지).
실제 FFmpeg: JPG(EXIF 회전)·투명 영역 0% PNG → '사진' 판별(확장자로 판단하지 않음) → 네모난 사진 레이어 실제 합성,
      같은 사진 중복 보관 방지, 배경 제거(로컬 제거기 없음 → 비활성 / 시험용 로컬 제거기 → 투명 사본·원본 보존·취소·품질 검사).
GUI: 초보자 기본 화면(고급 설정 접힘) → 투명 PNG 선택 즉시 자동 미리보기 → 분위기 카드 → 위치·크기 버튼·마우스 끌기 →
     합성 전/후 → 적용 예정 장면 수 → [✓ 이 모습으로 적용] 후 ④ 화면으로 복귀 → JPG·불투명 PNG는 오류 창 없이 화면 안 안내 →
     사진 그대로 넣기 → 제작. 화면 크기 1366×768 / 1920×1080 · 배율 100/125/150% · 키보드 탭 이동.
"""
import shutil
import struct
import time
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from app import bg_core as B
from app import bg_remove as R
from app import bg_story as S
from app.core import BuildCancelled, BuildError
from test_bg_story_08 import (  # noqa: F401 (fixtures)
    FFMPEG, FFPROBE, clip_of, fake_layer, gray, make_moving, make_person, media, needs_ffmpeg, probe_streams, pump,
    quiet, rgb, run, sha, tkroot, vi,
)


def _jpeg_with_exif(raw: bytes, orient: int) -> bytes:
    """JPEG에 APP1 Exif(방향 태그 하나)를 끼워 넣는다 (휴대폰 세로 사진 흉내)."""
    ifd = struct.pack("<H", 1) + struct.pack("<HHI", 0x0112, 3, 1) + struct.pack("<H", orient) + b"\x00\x00" + b"\x00" * 4
    tiff = b"II*\x00" + struct.pack("<I", 8) + ifd
    app1 = b"\xff\xe1" + struct.pack(">H", len(tiff) + 8) + b"Exif\x00\x00" + tiff
    return raw[:2] + app1 + raw[2:]


def make_photo(path: Path, w=640, h=360, *, exif=None, green=False):
    """배경이 있는 사진: 왼쪽 절반 흰색 (EXIF 6이면 바로 세웠을 때 위쪽이 흰색). green=True: 초록 배경 + 주황 인물."""
    src = (f"color=c=0x00ff00:s={w}x{h},drawbox=x={w // 3}:y={h // 5}:w={w // 3}:h={h * 4 // 5}:color=orange:t=fill"
           if green else f"color=c=black:s={w}x{h},drawbox=x=0:y=0:w={w // 2}:h={h}:color=white:t=fill")
    run(FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", src, "-frames:v", "1", "-q:v", "2", path)
    if exif:
        path.write_bytes(_jpeg_with_exif(path.read_bytes(), exif))
    return path


# ---------------- 단위 ----------------

def test_korean_preset_names_keep_internal_keys():
    names = {p.key: p.name for p in S.BUILTIN_PRESETS}
    assert names == {"christmas_couple": "🎄 크리스마스 커플", "fireworks_couple": "🎆 불꽃축제 커플",
                     "tokyo_night": "🌃 도쿄 야경 STORY", "cafe_story": "☕ 카페 STORY"}


def test_photo_layer_is_small_and_lifted():
    photo = S.StoryImage(Path("p.jpg").resolve(), 1600, 1200, (0, 0, 1600, 1200), "사진", 0.0, S.KIND_PHOTO)
    fire = S.get_preset("fireworks_couple").style                    # lift 음수여도 사진은 아래 끝에 붙지 않음
    for st in (S.StoryStyle(), fire, S.StoryStyle(size=1.1)):
        pl = S.placement(S.StoryLayer(photo, st), 1920, 1080)
        assert pl.w * pl.h <= 1920 * 1080 * S.PHOTO_MAX_AREA + 4000
        assert abs(pl.w / pl.h - 4 / 3) < 0.02                       # 비율 유지
        assert pl.y + pl.h <= 1080 - 1080 * S.PHOTO_MIN_LIFT + 1
    notes = S.story_notes([B.Clip(vi("a.mp4", 30), story=S.StoryLayer(photo, S.StoryStyle()))], 1.0, 30.0)
    assert any("네모난 사진" in n and "인물만 오려낸 합성이 아닙니다" in n for n in notes)


def test_old_library_json_without_kind_still_loads(_isolated_settings):
    d = _isolated_settings / S.LIBRARY_DIR
    d.mkdir()
    (d / "abc.png").write_bytes(b"old")
    (d / S.LIBRARY_FILE).write_text('{"version": 1, "images": [{"id": "abc", "name": "예전 커플", "file": "abc.png", '
                                    '"tags": ["Night"], "width": 400, "height": 800, "crop": [1, 2, 300, 700], '
                                    '"transparent": 0.6, "style": null, "preset": "tokyo_night", "added": "x"}]}',
                                    encoding="utf-8")
    lib = S.StoryLibrary()
    e = lib.get("abc")
    img = lib.image_of(e)
    assert e.kind == S.KIND_CUTOUT and img.kind == S.KIND_CUTOUT and img.pre == "" and img.crop == (1, 2, 300, 700)
    assert (d / "abc.png").read_bytes() == b"old"


# ---------------- 실제 FFmpeg ----------------

@needs_ffmpeg
def test_real_jpg_exif_photo_layer_and_dedupe(media, tmp_path, _isolated_settings):
    jpg = make_photo(media / "휴대폰 세로 사진.jpg", exif=6)          # 640×360 → 바로 세우면 360×640, 위쪽 흰색
    before = sha(jpg)
    img = S.inspect_image(Path(FFMPEG), Path(FFPROBE), jpg)
    assert img.kind == S.KIND_PHOTO and (img.width, img.height) == (360, 640) and img.pre == "transpose=1"
    lib = S.StoryLibrary()
    e1 = lib.add(img, "세로 사진")
    e2 = lib.add(img, "같은 사진 또 고름")
    assert e1.id == e2.id and len(lib.entries) == 1 and e1.kind == S.KIND_PHOTO and e1.exif == 6   # 사본 중복 없음
    v = make_moving(media / "夜景.mp4", 14)
    layer = S.StoryLayer(lib.image_of(e1), S.get_preset("christmas_couple").style, "사진")
    c = replace(clip_of(v), story=layer)
    spec = B.choose_output_spec([c.info])
    out = tmp_path / "photo_layer.mp4"
    r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=B.build_bg_plan([c], 12.0, 1.0, spec),
                               output=out, cancel_event=Event())
    assert r.frames == r.target_frames and [s["codec_type"] for s in probe_streams(out)] == ["video"]
    w = S.clip_window(c, 1.0, spec.fps)
    t = w.start + w.fade + 1 - 1.0
    pl = S.placement(layer, spec.width, spec.height)
    assert pl.y + pl.h < spec.height                                  # 아래 끝에서 띄움
    W, H = spec.width, spec.height
    top = rgb(out, t, ((pl.x + pl.w * 0.3) / W, (pl.y + pl.h * 0.1) / H, pl.w * 0.4 / W, pl.h * 0.3 / H))
    bottom = rgb(out, t, ((pl.x + pl.w * 0.3) / W, (pl.y + pl.h * 0.6) / H, pl.w * 0.4 / W, pl.h * 0.3 / H))
    assert min(top) > 200 and max(bottom) < 40                        # EXIF 방향대로 바로 선 네모난 사진
    assert rgb(out, t, (0.05, 0.4, 0.1, 0.2))[2] > 80                 # 사진 밖은 원래 장면
    assert sha(jpg) == before


@needs_ffmpeg
def test_real_background_removal_available_and_unavailable(media, tmp_path):
    R.register(None)
    assert R.detect() is None and "배경 제거를 쓸 수 없습니다" in R.UNAVAILABLE_NOTE
    src = make_photo(media / "초록 배경 인물.jpg", green=True)
    before = sha(src)
    out = tmp_path / "out_結果"

    def chroma(s, d, cancel, prog):          # 시험용 로컬 제거기 (FFmpeg colorkey, 외부 전송 없음)
        prog(0.5)
        run(FFMPEG, "-v", "error", "-y", "-i", s, "-vf", "colorkey=0x00ff00:0.3:0.1,format=rgba", "-frames:v", "1",
            "-c:v", "png", "-f", "image2", d)
    rem = R.Remover("시험용 크로마키", "test", "test", 0.0, chroma)
    seen = []
    img = R.remove_background(rem, Path(FFMPEG), Path(FFPROBE), src, out, Event(), seen.append)
    assert img.kind == S.KIND_CUTOUT and 0.5 < img.transparent < 0.95 and seen[0] == 0.0 and seen[-1] == 1.0
    assert [p.suffix for p in out.iterdir()] == [".png"] and sha(src) == before   # .part 없음, 원본 그대로

    def copy_only(s, d, cancel, prog):       # 배경을 지우지 못한 결과 → 품질 검사에서 거부, 결과 삭제
        run(FFMPEG, "-v", "error", "-y", "-i", s, "-frames:v", "1", "-c:v", "png", "-f", "image2", d)
    with pytest.raises(BuildError) as e:
        R.remove_background(R.Remover("x", "", "", 0, copy_only), Path(FFMPEG), Path(FFPROBE), src, tmp_path / "bad",
                            Event())
    assert "제대로 지워지지" in str(e.value) and list((tmp_path / "bad").iterdir()) == []
    ev = Event()

    def cancelled(s, d, cancel, prog):
        ev.set()
    with pytest.raises(BuildCancelled):
        R.remove_background(R.Remover("x", "", "", 0, cancelled), Path(FFMPEG), Path(FFPROBE), src, tmp_path / "c", ev)
    assert list((tmp_path / "c").iterdir()) == [] and sha(src) == before


# ---------------- GUI ----------------

def _owner(tkroot, tools):
    from app.bg_ui import BackgroundMakerWindow
    w = BackgroundMakerWindow(tkroot, tools=tools)
    w.quiet_errors = True
    w.withdraw()
    return w


@needs_ffmpeg
def test_beginner_three_step_flow_png_jpg_opaque(tkroot, media, tmp_path, quiet, _isolated_settings):
    v1 = make_moving(media / "크리스마스 거리.mp4", 12)
    v2 = make_moving(media / "東京タワー夜景.mp4", 12, bg="darkgreen")
    png = make_person(media / "주인공 커플.png")
    jpg = make_photo(media / "배경있는 사진.jpg")
    opaque = media / "투명0%.png"
    run(FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=300x400", "-frames:v", "1", opaque)
    originals = {p: sha(p) for p in (png, jpg, opaque)}
    w = _owner(tkroot, lambda: (FFMPEG, FFPROBE))
    try:
        assert w.add_paths([v1, v2])
        assert pump(w, lambda: not w.loading and len(w.clips) == 2)
        d = w.open_story()
        d.withdraw()
        # 초보자 기본 화면: 고급 설정 접힘, 분위기 카드 4개, 적용 버튼은 사진을 고르기 전 비활성
        assert d.adv.winfo_manager() == "" and len(d.cards) == 4
        assert "🎄 크리스마스 커플" in str(d.cards["christmas_couple"].cget("text"))
        assert str(d.btn_apply.cget("state")) == "disabled" and d.mode.get() == S.APPLY_ALL
        assert d.scene_box.winfo_manager() == ""                     # '선택한 장면만'을 고를 때만 장면 목록
        assert "적용 예정: 2개 장면" in d.count_text.get()
        # ① 투명 PNG → 자동 보관 + 첫 장면 합성 미리보기 자동 표시
        assert d.select_path(png)
        assert pump(d, lambda: d.preview_count >= 1 and not d.busy, 90)
        assert len(d.lib.entries) == 1 and d.image.kind == S.KIND_CUTOUT and d.notice_kind == "ok"
        assert "표시 중: 장면 1" in d.pv_caption.get() and d.canvas.find_withtag("box")
        assert str(d.btn_apply.cget("state")) == "normal"
        # 미리보기 상자 = 실제 출력 좌표를 줄인 것
        spec = d.pv_info["spec"]
        pl = S.placement(d.pv_info["layer"], spec.width, spec.height)
        assert d.pv_info["box"][0] == pytest.approx(pl.x * d.pv_w / spec.width)
        # ② 분위기 카드 → 미리보기 자동 갱신
        d.cards["fireworks_couple"].invoke()
        assert pump(d, lambda: d.preview_count >= 2 and not d.busy, 90)
        assert d.style().align == S.ALIGN_CENTER and d.style().lift < 0
        # 위치·크기 간단 버튼 + 마우스 끌기(move_to)
        d.set_align(S.ALIGN_LEFT)
        d.resize(+1)
        assert d.style().align == S.ALIGN_LEFT and d.style().size == pytest.approx(0.52)
        assert pump(d, lambda: d.preview_count >= 3 and not d.busy, 90)
        x0, y0, x1, y1 = d.pv_info["box"]
        d.move_to(d.pv_w - (x1 - x0) - 20, y0 - 30, d.pv_w - 20, y1 - 30)   # 오른쪽 위로 끌어 놓기
        st = d.style()
        assert st.align == S.ALIGN_RIGHT and st.margin == pytest.approx(round(20 / d.pv_w * 100) / 100, abs=0.011)
        assert pump(d, lambda: d.preview_count >= 4 and not d.busy, 90)
        d.show_before.set(True)
        d._redraw()
        assert not d.canvas.find_withtag("box")                       # 합성 전 화면
        d.show_before.set(False)
        d._redraw()
        # 고급 설정 펼치기/접기
        d.show_adv.set(True)
        d._toggle_adv()
        assert d.adv.winfo_manager() == "pack"
        d.show_adv.set(False)
        d._toggle_adv()
        assert d.adv.winfo_manager() == ""
        # 선택한 장면만 → 적용 예정 수
        d.mode.set(S.APPLY_SELECTED)
        d._mode_changed()
        d.lst_scenes.selection_clear(0, "end")
        d.lst_scenes.selection_set(1)
        d._update_count()
        assert d.scene_box.winfo_manager() == "pack" and "적용 예정: 1개 장면" in d.count_text.get()
        d.mode.set(S.APPLY_ALL)
        d._mode_changed()
        # [✓ 이 모습으로 적용] → 창 닫고 ④로 복귀
        assert d.apply()
        assert w.story_dialog is None and all(c.story is not None for c in w.clips)
        assert "주인공을 2개 장면" in w.status.get() and w.clips[0].story.style.align == S.ALIGN_RIGHT
        # JPG: 오류 창 없이 화면 안 안내 + 3가지 선택지 (배경 제거는 이 PC에서 비활성 + 이유)
        d = w.open_story()
        d.withdraw()
        assert d.entry_id.get() == d.lib.entries[0].id                # 마지막 사진 기억
        assert d.select_path(jpg)
        assert pump(d, lambda: not d.checking, 60)
        assert d.image.kind == S.KIND_PHOTO and d.photo_box.winfo_manager() == "pack" and d.notice_kind == "warn"
        assert "배경이 남아 있는 사진" in d.notice.get() and quiet == []   # 모달 오류 창 없음
        assert str(d.btn_apply.cget("state")) == "disabled" and str(d.btn_remove.cget("state")) == "disabled"
        assert str(d.remove_note.cget("text")) == R.UNAVAILABLE_NOTE
        assert not d.remove_background() and "쓸 수 없습니다" in d.notice.get()
        assert not d.apply()
        n0 = d.preview_count
        assert d.use_photo_as_is()
        assert pump(d, lambda: d.preview_count > n0 and not d.busy, 90)
        assert "네모난 사진 그대로" in d.pv_caption.get() and str(d.btn_apply.cget("state")) == "normal"
        # 투명 영역 0% PNG도 같은 안내 (확장자로 판단하지 않음)
        assert d.select_path(opaque)
        assert pump(d, lambda: not d.checking, 60)
        assert d.image.kind == S.KIND_PHOTO and d.photo_box.winfo_manager() == "pack" and quiet == []
        assert d.use_photo_as_is() and d.apply()
        assert w.clips[0].story.image.kind == S.KIND_PHOTO
        # 제작 (음악 길이 직접 입력)
        w.minutes.set(0)
        w.custom_length.set("00:00:20")
        w._length_settled()
        w.outdir.set(str(tmp_path / "BG"))
        w._options_changed()
        assert w.start(confirm=False)
        assert pump(w, lambda: not w.running, 300)
        assert w.result.frames == w.result.target_frames == 600
    finally:
        w.destroy()
    lib = S.StoryLibrary()
    assert sorted(e.kind for e in lib.entries) == [S.KIND_CUTOUT, S.KIND_PHOTO, S.KIND_PHOTO]
    assert all(lib.available(e) for e in lib.entries) and {p: sha(p) for p in originals} == originals


@needs_ffmpeg
def test_gui_background_removal_when_local_remover_exists(tkroot, media, quiet, _isolated_settings):
    v = make_moving(media / "a.mp4", 12)
    src = make_photo(media / "초록 배경.jpg", green=True)

    def chroma(s, d, cancel, prog):
        run(FFMPEG, "-v", "error", "-y", "-i", s, "-vf", "colorkey=0x00ff00:0.3:0.1,format=rgba", "-frames:v", "1",
            "-c:v", "png", "-f", "image2", d)
    R.register(R.Remover("시험용 크로마키", "test", "test", 0.0, chroma))
    w = _owner(tkroot, lambda: (FFMPEG, FFPROBE))
    try:
        assert w.add_paths([v])
        assert pump(w, lambda: not w.loading and len(w.clips) == 1)
        d = w.open_story()
        d.withdraw()
        assert d.select_path(src)
        assert pump(d, lambda: not d.checking, 60)
        assert str(d.btn_remove.cget("state")) == "normal" and str(d.remove_note.cget("text")) == ""
        assert d.remove_background()
        assert pump(d, lambda: not d.removing, 60)
        assert d.image.kind == S.KIND_CUTOUT and d.photo_box.winfo_manager() == "" and d.notice_kind == "ok"
        assert pump(d, lambda: d.preview_count >= 1 and not d.busy, 90)
        assert d.apply() and w.clips[0].story.image.kind == S.KIND_CUTOUT
    finally:
        R.register(None)
        w.destroy()


@pytest.mark.parametrize("screen", [(1366, 768), (1920, 1080)])
@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5])
def test_layout_fits_screen_and_scaling(tkroot, monkeypatch, screen, scale, _isolated_settings):
    """작은 노트북(1366×768)·FHD, Windows 배율 100/125/150%: 창이 화면 안, 적용 버튼 항상 보임, 가로 잘림 없음, 탭 이동."""
    from app.bg_story_ui import StoryDialog
    sw, sh = screen
    monkeypatch.setattr(StoryDialog, "winfo_screenwidth", lambda self: sw)
    monkeypatch.setattr(StoryDialog, "winfo_screenheight", lambda self: sh)
    old = float(tkroot.tk.call("tk", "scaling"))
    tkroot.tk.call("tk", "scaling", 96 * scale / 72)
    w = _owner(tkroot, lambda: (None, None))
    try:
        d = w.open_story()
        for _ in range(5):
            d.update()
        W, H = d.winfo_width(), d.winfo_height()
        assert W <= sw and H <= sh
        assert d.btn_apply.winfo_ismapped() and d.btn_apply.winfo_rooty() + d.btn_apply.winfo_height() <= d.winfo_rooty() + H
        assert d.btn_apply.winfo_width() >= d.btn_apply.winfo_reqwidth()
        assert d.scroll.body.winfo_reqwidth() <= W + 2, (d.scroll.body.winfo_reqwidth(), W)   # 가로로 잘리는 칸 없음
        for b in list(d.cards.values()) + [d.btn_pick, d.btn_left, d.btn_smaller, d.chk_before]:
            assert b.winfo_rootx() + b.winfo_width() <= d.winfo_rootx() + W
        d.photo_box.pack(fill="x", pady=(8, 0))                      # 배경 있는 사진 선택지가 나와도 가로로 잘리지 않음
        d.update()
        assert d.scroll.body.winfo_reqwidth() <= W + 2, ("photo", d.scroll.body.winfo_reqwidth(), W)
        for b in (d.btn_remove, d.btn_as_is, d.btn_other):
            assert b.winfo_width() >= b.winfo_reqwidth()
        d.photo_box.pack_forget()
        for b in d.cards.values():                                   # 분위기 카드 글자가 잘리지 않음
            assert b.winfo_width() >= b.winfo_reqwidth()
        d.show_adv.set(True)                                         # 고급 설정을 펼쳐도 가로로 잘리지 않음
        d._toggle_adv()
        d.update()
        assert d.scroll.body.winfo_reqwidth() <= W + 2, ("adv", d.scroll.body.winfo_reqwidth(), W)
        d.show_adv.set(False)
        d._toggle_adv()
        # 키보드 탭 이동: 사진 고르기 → … → 분위기 카드 → … → 적용 버튼 (사진을 고른 뒤처럼 적용 버튼 활성)
        d.btn_apply.configure(state="normal")
        seen, cur = [], d.btn_pick
        for _ in range(300):
            seen.append(cur)
            cur = cur.tk_focusNext()
            if cur is None or cur == d.btn_pick:
                break
        assert d.btn_apply in seen and all(c in seen for c in d.cards.values())
    finally:
        w.destroy()
        tkroot.tk.call("tk", "scaling", old)
