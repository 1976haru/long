"""④ 배경 만들기 — C06(자연스러운 조합·중복) / C07(화질·용량) 추가 검증.

합성 MP4(FFmpeg lavfi로 만든 시험 영상)만 사용한다. 실제 자연풍경 검증은 test_bg_maker.py의
test_real_nature_samples_build(환경변수 PLVM_NATURE_SAMPLES)에서 따로 하며, 없으면 SKIP 된다.
"""
import os
import subprocess
import time
from pathlib import Path
from threading import Event

import pytest

from app import bg_analysis as A
from app import bg_core as B
from app.core import VideoInfo, probe_video
from tests.test_bg_maker import (FFMPEG, FFPROBE, leftovers, make_clip, needs_ffmpeg, pixel, pump, quiet,  # noqa: F401
                                 sha, tkroot)


def vi(name="a.mp4", dur=30.0, w=1920, h=1080, fps=30.0):
    return VideoInfo(Path(name).resolve(), dur, 1000, w, h, fps, "h264", "aac", "yuv420p", "High", 48000, 2)


# ---------------- C07: 코덱·프로필·용량 예측 ----------------

def test_default_is_live_compatible_h264_even_for_4k():
    for q, src in ((B.QUALITY_FHD, vi()), (B.QUALITY_4K, vi(w=3840, h=2160))):
        s = B.choose_output_spec([src], q)
        assert s.codec == "h264" and s.live_compatible
        assert "-pix_fmt" in B.piece_command(Path("ffmpeg"), _piece(), s, Path("o.mp4"))


def test_hevc_is_explicit_choice_and_never_live_ready():
    s = B.choose_output_spec([vi()], B.QUALITY_FHD, codec=B.CODEC_HEVC, hevc_available=True)
    assert (s.codec, s.encoder, s.live_compatible) == ("hevc", "libx265", False)
    assert s.crf == B.HEVC_BASE_CRF and any("LIVE" in n for n in s.notes)
    cmd = B.piece_command(Path("ffmpeg"), _piece(), s, Path("o.mp4"))
    assert cmd[cmd.index("-tag:v") + 1] == "hvc1" and cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    fb = B.choose_output_spec([vi()], B.QUALITY_FHD, codec=B.CODEC_HEVC, hevc_available=False)
    assert fb.codec == "h264" and any("libx265" in n for n in fb.notes)


def test_4k_never_upscaled_or_labelled_from_fhd_source():
    s = B.choose_output_spec([vi(w=1920, h=1080)], B.QUALITY_4K)
    assert s.height == 1080 and not s.label.startswith("4K")


def _piece():
    return B.Piece("body", B.Segment(Path("a.mp4"), 1.0, 5.0), None, 5.0, "t")


def test_three_easy_profiles_and_cap_is_not_a_size_guarantee():
    keys = [p.key for p in B.ENCODE_PROFILES]
    assert keys[0] == B.PROFILE_BEST and {"balanced", "small"} <= set(keys)
    assert all(p.short for p in B.ENCODE_PROFILES)
    best = B.choose_output_spec([vi()])
    assert (best.crf, best.preset, best.maxrate_kbps) == (18, "fast", 0)  # 기본 고화질은 그대로
    small_spec = B.choose_output_spec([vi()], profile="small")
    bal_spec = B.choose_output_spec([vi()], profile="balanced")
    # 저용량은 균형과 같은 상한 → 세밀한 화면에서도 균형보다 커지지 않는다 (실환경: 상한 없을 때 499 MB > 435 MB 역전)
    assert small_spec.crf == 24 and small_spec.maxrate_kbps == bal_spec.maxrate_kbps == 8000
    assert small_spec.bytes_per_second <= bal_spec.bytes_per_second
    for key in ("balanced", "small"):
        s = B.choose_output_spec([vi()], profile=key)
        assert s.maxrate_kbps > 0 and s.bufsize_kbps == 2 * s.maxrate_kbps and "상한" in s.label
        cmd = B.piece_command(Path("ffmpeg"), _piece(), s, Path("o.mp4"))
        assert cmd[cmd.index("-maxrate") + 1] == f"{s.maxrate_kbps}k"
        assert s.bytes_per_second <= s.maxrate_kbps * 1000 * 1.08 / 8 + 1
        assert "보장" in get_note(key)  # '정확한 크기 보장 아님'을 화면 설명에 적는다
    small, bal = (B.choose_output_spec([vi()], profile=k).bytes_per_second for k in ("small", "balanced"))
    assert small < best.bytes_per_second and bal < best.bytes_per_second  # 둘 다 상한 기준 보수값
    assert "실제 용량 측정" in get_note("small")
    # 상한은 해상도·FPS에 비례 (4K 60fps는 더 큼)
    k4 = B.choose_output_spec([vi(w=3840, h=2160, fps=60)], B.QUALITY_4K, profile="balanced")
    assert k4.maxrate_kbps > 4 * B.choose_output_spec([vi()], profile="balanced").maxrate_kbps


def get_note(key):
    return B.get_profile(key).note


def test_old_saved_profile_key_falls_back_safely():
    assert B.get_profile("slow").key in {p.key for p in B.ENCODE_PROFILES}
    assert B.get_profile(None).key == B.PROFILE_BEST


def test_measured_rate_changes_estimate_and_keeps_headroom():
    spec = B.choose_output_spec([vi()])
    plan = B.build_bg_plan([B.Clip(vi("a.mp4", 40)), B.Clip(vi("b.mp4", 30))], 600, 1.0, spec)
    assert plan.size_range == (0, plan.expected_size)
    m = B.with_measured_rate(plan, 1_000_000)  # 8 Mbps
    assert m.expected_size == 600_000_000
    lo, hi = m.size_range
    assert lo < m.expected_size < hi == int(600_000_000 * B.MEASURE_SHOW_HIGH)
    # 실환경: 실제 = 실측 × 0.75~0.82 → 표시 범위 안에 들어가야 하고, 공간 검사(1.25배)는 범위 상한보다 넉넉해야 함
    assert all(lo <= int(600_000_000 * r) <= hi for r in (0.75, 0.82, 1.0))
    assert m.required_bytes >= hi
    assert m.required_bytes == B.peak_bytes(1_250_000 * m.cycle_duration, 1_250_000 * 600)
    pieces = B.measurement_pieces(plan)
    assert len(pieces) == 2 and {p.a.path.name for p in pieces} == {"a.mp4", "b.mp4"}
    assert all(p.length == B.MEASURE_SECONDS for p in pieces)


def test_stale_cleanup_removes_only_owned_part(tmp_path):
    out = tmp_path / "출력"
    out.mkdir()
    done = out / "완성_BG_10M.mp4"
    done.write_bytes(b"final")
    mine = out / "새영상_BG_10M.mp4.part.mp4"
    mine.write_bytes(b"part")
    other = out / "다른작업.mp4.part.mp4"
    other.write_bytes(b"other")
    tmp = out / f"{B.TEMP_PREFIX}dead"
    tmp.mkdir()
    (tmp / B.OWNER_FILE).write_text(mine.name, encoding="utf-8")
    old = time.time() - 13 * 3600
    for p in (mine, other, tmp, done):
        os.utime(p, (old, old))
    evil = out / f"{B.TEMP_PREFIX}evil"
    evil.mkdir()
    (evil / B.OWNER_FILE).write_text("..\\완성_BG_10M.mp4", encoding="utf-8")
    os.utime(evil, (old, old))
    assert B.cleanup_stale_temp(out) == 2
    assert not mine.exists() and not tmp.exists() and not evil.exists()
    assert done.read_bytes() == b"final" and other.exists()  # 완성 MP4·남의 임시 파일은 그대로


def test_stale_cleanup_removes_only_bg_owned_concat_dir(tmp_path):
    """반복 연결 도중 강제 종료되면 ①의 .PLVM_TEMP_*(concat.txt)도 남는다 (실환경 시험에서 확인).
    ④ 임시 폴더만 가리키는 것만 지우고, ① 영상 늘리기의 임시 폴더는 그대로 둔다."""
    from app.core import write_concat_file
    out = tmp_path / "출력 폴더"
    out.mkdir()
    bgtmp = out / f"{B.TEMP_PREFIX}abc"
    mine = out / ".PLVM_TEMP_mine"
    mine.mkdir()
    write_concat_file([bgtmp / "cycle.mp4"] * 3, mine / "concat.txt")
    long_job = out / ".PLVM_TEMP_long"  # ① 영상 늘리기: 사용자 영상을 가리킴
    long_job.mkdir()
    write_concat_file([out / "SET1.mp4", bgtmp / "cycle.mp4"], long_job / "concat.txt")
    extra = out / ".PLVM_TEMP_extra"  # 다른 파일이 같이 있으면 소유 불명 → 그대로
    extra.mkdir()
    write_concat_file([bgtmp / "cycle.mp4"], extra / "concat.txt")
    (extra / "note.txt").write_text("x", encoding="utf-8")
    elsewhere = out / ".PLVM_TEMP_elsewhere"  # 다른 폴더의 ④ 임시 폴더를 가리킴 → 그대로
    elsewhere.mkdir()
    write_concat_file([tmp_path / f"{B.TEMP_PREFIX}zzz" / "cycle.mp4"], elsewhere / "concat.txt")
    fresh = out / ".PLVM_TEMP_fresh"  # 최근 것(다른 창에서 진행 중일 수 있음) → 그대로
    fresh.mkdir()
    write_concat_file([bgtmp / "cycle.mp4"], fresh / "concat.txt")
    old = time.time() - 13 * 3600
    for p in (mine, long_job, extra, elsewhere):
        os.utime(p, (old, old))
    assert B.cleanup_stale_temp(out) == 1
    assert not mine.exists()
    assert long_job.exists() and extra.exists() and elsewhere.exists() and fresh.exists()


# ---------------- C06: 중복·전환·순서 (작은 숫자 배열만) ----------------

def _grid(v, pattern=0):
    if pattern:
        return bytes(min(255, v + (40 if (i + pattern) % 3 == 0 else 0)) for i in range(144))
    return bytes([v] * 144)


def _an(name, grids, sf=4.0, rgb=(100, 100, 100), seq=(), dh=()):
    return A.ClipAnalysis(Path(name), len(grids) / sf, sf, grids, mean_rgb=rgb, dhash_seq=seq, dhash=dh)


def test_trimmed_reencoded_copy_is_very_similar_but_other_scene_is_not():
    seq = tuple((0x9F3A_55C1_0F0F_1234 * (k + 3)) & (2**64 - 1) for k in range(40))
    full = _an("숲.mp4", [_grid(100)] * 160, seq=seq, dh=seq[4:40:8])
    trimmed = _an("숲_잘라서_재인코딩.mp4", [_grid(100)] * 80, seq=tuple(x ^ 0b101 for x in seq[10:30]),
                  dh=tuple(x ^ 1 for x in seq[12:30:4]))
    other = _an("바다.mp4", [_grid(100)] * 160, rgb=(40, 90, 160),
                seq=tuple((x * 7 + 12345) & (2**64 - 1) for x in seq), dh=(0, 2**63, 2**40, 77, 999))
    f = A.similarity(full, trimmed)
    assert f is not None and f.kind == "매우 유사" and "%" in f.note
    assert A.similarity(full, other) is None
    kinds = {(d.a.name, d.b.name) for d in A.find_duplicates([full, trimmed, other])}
    assert kinds == {("숲.mp4", "숲_잘라서_재인코딩.mp4")}


def test_transition_advice_flags_flash_and_duplicates_and_respects_range():
    dark = _an("밤.mp4", [_grid(20, 1)] * 40)
    bright = _an("낮.mp4", [_grid(200, 1)] * 40)
    mid = _an("노을.mp4", [_grid(110, 1)] * 40)
    t = A.transition_advice(dark, bright)
    assert t.verdict == A.TRANS_BAD and t.luma_jump > 150
    assert A.transition_advice(mid, _an("구름.mp4", [_grid(120, 2)] * 40)).verdict == A.TRANS_OK
    # 사용 구간 기준: 끝 부분만 어두운 영상 — 전체를 쓰면 어색, 밝은 앞부분만 쓰면 자연스러움
    fading = _an("해질녘.mp4", [_grid(120, 1)] * 30 + [_grid(10, 1)] * 10)
    nxt = _an("들판.mp4", [_grid(125, 1)] * 40)
    assert A.transition_advice(fading, nxt).verdict == A.TRANS_BAD
    assert A.transition_advice(fading, nxt, 1.0, (0.0, 6.0)).verdict == A.TRANS_OK
    # 같은 파일(SHA)이 연달아: 어색함
    a, b = _an("a.mp4", [_grid(100, 1)] * 40), _an("a_copy.mp4", [_grid(100, 1)] * 40)
    a.sha256 = b.sha256 = "same"
    assert A.transition_advice(a, b).verdict == A.TRANS_BAD
    warns = A.transition_warnings([dark, bright, mid])
    assert any("밤.mp4 → 낮.mp4" in w for w in warns)


def test_suggest_order_separates_similar_scenes_including_wraparound():
    seq_a = tuple(range(1000, 1040))
    seq_b = tuple((x * 2654435761) & (2**64 - 1) for x in range(40))
    a1 = _an("호수1.mp4", [_grid(100, 1)] * 40, seq=seq_a)
    a2 = _an("호수2.mp4", [_grid(100, 1)] * 40, seq=seq_a)          # 호수1과 매우 유사
    b1 = _an("숲1.mp4", [_grid(110, 2)] * 40, rgb=(60, 140, 60), seq=seq_b)
    b2 = _an("숲2.mp4", [_grid(110, 2)] * 40, rgb=(60, 140, 60), seq=seq_b)  # 숲1과 매우 유사
    ans = [a1, a2, b1, b2]
    order = A.suggest_order(ans)
    assert order[0] == 0 and sorted(order) == [0, 1, 2, 3]
    names = [ans[k].path.name[:2] for k in order]
    assert all(names[k] != names[(k + 1) % 4] for k in range(4))  # 비슷한 장면이 이웃하지 않음 (마지막→처음 포함)
    ok = [a1, b1, a2, b2]
    assert A.suggest_order(ok) == [0, 1, 2, 3]  # 이미 좋은 순서는 그대로
    many = [_an(f"c{k}.mp4", [_grid(30 + k * 20, 1)] * 40) for k in (0, 5, 1, 4, 2, 3, 6, 8, 7, 9)]
    o = A.suggest_order(many)
    assert sorted(o) == list(range(10)) and o[0] == 0


def _warn_score(seq):
    w = {A.TRANS_BAD: 100, A.TRANS_CHECK: 1}
    return sum(w.get(t.verdict, 0) for t in A.transition_advices(seq))


def test_suggest_order_never_adds_visible_warnings():
    """실환경 6개 영상에서 연속값만 줄이는 순서가 '확인 필요'를 늘린 회귀 — 화면 판정 기준으로 원래 순서보다 나빠지면 안 됨.
    밝기가 조금씩 다른 장면 + 밝기 단계가 큰 장면을 섞어 여러 배치를 확인한다."""
    import itertools
    lumas = [40, 95, 150, 205, 120, 70]
    for perm in itertools.islice(itertools.permutations(range(6)), 0, 720, 37):
        ans = [_an(f"s{k}.mp4", [_grid(lumas[k], k % 3)] * 40, rgb=(lumas[k], lumas[k], 90 + 20 * k)) for k in perm]
        order = A.suggest_order(ans)
        assert order[0] == 0 and sorted(order) == list(range(6))
        assert _warn_score([ans[k] for k in order]) <= _warn_score(ans), perm


# ---------------- 실제 FFmpeg (합성 MP4) ----------------

SCENES = [  # (이름, lavfi 원본, 길이, FPS) — 길이·FPS·해상도가 모두 다르게
    ("01_숲_23.976.mp4", "testsrc2=s=640x360:r=24000/1001", 9, None),
    ("02_바다_25.mp4", "mandelbrot=s=640x360:r=25", 8, None),
    ("03_하늘_29.97.mp4", "gradients=s=640x360:r=30000/1001:speed=0.01", 10, None),
    ("04_들판_30.mp4", "cellauto=s=640x360:r=30:rule=110", 7, None),
    ("05_노을_50.mp4", "life=s=640x360:r=50:mold=10:ratio=0.2:death_color=#402010:life_color=#ffb060", 6, None),
    ("06_호수_60_480x360.mp4", "smptehdbars=s=480x360:r=60", 6, None),
]


@pytest.fixture
def six_scenes(tmp_path):
    d = tmp_path / "한글 경로" / "자연 풍경(합성)"
    d.mkdir(parents=True)
    return [make_clip(d / n, src, dur, audio=(k % 2 == 0)) for k, (n, src, dur, _) in enumerate(SCENES)]


@needs_ffmpeg
def test_real_six_clips_mixed_fps_10_and_3_minutes(six_scenes, tmp_path):
    before = {p: sha(p) for p in six_scenes}
    infos = [probe_video(p, Path(FFPROBE)) for p in six_scenes]
    assert len({round(i.fps, 2) for i in infos}) == 6
    spec = B.choose_output_spec(infos)
    assert (spec.width, spec.height, spec.codec) == (640, 360, "h264")
    clips = [B.Clip(i) for i in infos]
    clips[0] = B.with_range(clips[0], 1.0, 8.0)  # 구간 조정
    clips[2], clips[3] = clips[3], clips[2]      # 순서 조정
    for minutes in (10, 3):
        plan = B.build_bg_plan(clips, minutes * 60, 1.0, spec)
        assert plan.clip_count == 6 and len(plan.pieces) == 12
        out = tmp_path / "출력" / f"배경_{minutes}분.mp4"
        r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=plan, output=out,
                                   cancel_event=Event())
        assert abs(r.duration - minutes * 60) < 0.5 and r.fps == pytest.approx(spec.fps, abs=0.01)
        streams = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt",
                                  "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.split()
        assert len(streams) == 1 and set(streams[0].split(",")) == {"h264", "video", "yuv420p"}  # 영상만, LIVE 호환
        assert leftovers(out.parent) == []
    # 첫 장면(1~8초 사용) 본문 → 디졸브 → 둘째 장면: 경계 프레임이 두 장면의 중간 밝기
    a, b = pixel(out, 2.0), pixel(out, 7.4)
    assert a != b
    assert {p: sha(p) for p in six_scenes} == before  # 원본 그대로


@needs_ffmpeg
def test_real_reencoded_and_trimmed_copy_flagged(tmp_path):
    src = make_clip(tmp_path / "원본_숲.mp4", "testsrc2=s=640x360:r=30", 12, audio=False)
    copy = tmp_path / "재인코딩_잘라낸_사본.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", "3", "-i", str(src), "-t", "7", "-vf", "scale=426:240",
                    "-c:v", "libx264", "-crf", "30", "-preset", "veryfast", str(copy)], check=True)
    other = make_clip(tmp_path / "다른_바다.mp4", "mandelbrot=s=640x360:r=30", 12, audio=False)
    ans = [A.analyze_clip(Path(FFMPEG), p, probe_video(p, Path(FFPROBE)).duration) for p in (src, copy, other)]
    found = {(f.a.name, f.b.name): f for f in A.find_duplicates(ans)}
    assert ("원본_숲.mp4", "재인코딩_잘라낸_사본.mp4") in found
    assert found[("원본_숲.mp4", "재인코딩_잘라낸_사본.mp4")].kind == "매우 유사"
    assert not any("다른_바다.mp4" in k for pair in found for k in pair)
    t = A.transition_advice(ans[0], ans[1])
    assert t.verdict == A.TRANS_BAD and t.similar == "매우 유사"  # 사본이 연달아 오면 '어색함'으로 경고


@needs_ffmpeg
def test_real_static_vs_moving_camera_loop_fitness(tmp_path):
    static = make_clip(tmp_path / "고정.mp4", "testsrc2=s=640x360:r=30", 16, audio=False)
    moving = tmp_path / "이동_패닝.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=2400x360:r=30", "-t", "16",
                    "-vf", "crop=640:360:'min(t*110,1760)':0", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(moving)], check=True)
    a_s = A.analyze_clip(Path(FFMPEG), static, 16.0)
    a_m = A.analyze_clip(Path(FFMPEG), moving, 16.0)
    assert a_m.camera == A.CAMERA_MOVING and a_s.camera != A.CAMERA_MOVING
    assert A.recommend_loop(a_m, 1.0).verdict != A.VERDICT_GOOD
    assert a_m.drift > a_s.drift


@needs_ffmpeg
def test_real_measure_bitrate_close_to_actual_and_clean(six_scenes, tmp_path):
    infos = [probe_video(p, Path(FFPROBE)) for p in six_scenes[:5]]
    spec = B.choose_output_spec(infos, profile="balanced")
    plan = B.build_bg_plan([B.Clip(i) for i in infos], 120, 1.0, spec)
    work = tmp_path / "측정"
    bps = B.measure_bitrate(Path(FFMPEG), plan, Event(), work)
    assert bps > 0 and list(work.iterdir()) == []  # 시험 파일 정리
    r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE), plan=B.with_measured_rate(plan, bps),
                               output=tmp_path / "o.mp4", cancel_event=Event())
    actual = r.size / r.duration
    assert 0.4 < bps / actual < 2.5  # 짧은 시험 구간 기준 — 같은 자릿수 (공간 검사는 ×1.25 + 제작 중 재검사)


@needs_ffmpeg
def test_real_hevc_output_is_not_live_ready(tmp_path):
    from app.bg_core import hevc_available
    if not hevc_available(Path(FFMPEG)):
        pytest.skip("libx265 없음")
    from app import live_ready
    src = make_clip(tmp_path / "a.mp4", "testsrc2=s=640x360:r=30", 6, audio=False)
    src2 = make_clip(tmp_path / "b.mp4", "mandelbrot=s=640x360:r=30", 6, audio=False)
    infos = [probe_video(p, Path(FFPROBE)) for p in (src, src2)]
    spec = B.choose_output_spec(infos, codec=B.CODEC_HEVC, hevc_available=True)
    r = B.run_background_build(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE),
                               plan=B.build_bg_plan([B.Clip(i) for i in infos], 30, 1.0, spec),
                               output=tmp_path / "hevc.mp4", cancel_event=Event())
    assert probe_video(r.output, Path(FFPROBE)).video_codec == "hevc"
    rep = live_ready.analyze_live_ready(r.output, Path(FFPROBE))
    assert any(i.code == "vcodec" for i in rep.issues)  # H.265는 LIVE READY 제약을 우회하지 않음


# ---------------- 화면 ----------------

@needs_ffmpeg
def test_window_steps_order_codec_measure(tkroot, six_scenes, tmp_path, quiet, monkeypatch):
    import app.bg_ui as U
    from app.settings import load_settings
    w = U.BackgroundMakerWindow(tkroot, tools=lambda: (FFMPEG, FFPROBE))
    try:
        w.withdraw()
        assert w.current_step() == 0 and "①" in w.step_hint.get()
        assert str(w.btn_measure.cget("state")) == "disabled"
        w.add_paths(six_scenes[:5])
        assert pump(w, lambda: not w.loading and len(w.analyses) == 5, timeout=120)
        assert w.current_step() == 5
        steps = [str(lb.cget("text")) for lb in w.step_labels]
        assert steps[1].startswith("✓") and "(선택)" in steps[3] and not steps[3].startswith("✓")  # 미리보기 안 했으면 ✓ 없음
        assert {int(lb.grid_info()["row"]) for lb in w.step_labels} == {0, 1}  # 두 줄 (⑦⑧이 잘리지 않게)
        assert all(w.tree.set(i, "next") in (A.TRANS_OK, A.TRANS_CHECK, A.TRANS_BAD) for i in w.tree.get_children())
        assert "예상 최종 크기 최대" in w.size_text.get() and "작업 중 최대" in w.size_text.get()
        # 쉬운 화질 선택 (라디오) + H.265 선택지
        w.profile.set(B.get_profile("balanced").label)
        w.codec.set(B.CODEC_HEVC)
        w._options_changed()
        assert w.plan.spec.profile == "balanced"
        if w._hevc_ok():
            assert w.plan.spec.codec == "hevc" and "LIVE READY가 아닙니다" in w.warn_text.get()
        assert load_settings()["bg_maker"]["codec"] == "hevc"
        w.codec.set(B.CODEC_H264)
        w._options_changed()
        assert w.plan.spec.codec == "h264"
        # 자동 순서 추천: 첫 영상 고정, 같은 영상 집합
        names = sorted(c.path.name for c in w.clips)
        first = w.clips[0].path
        assert w._auto_order()
        assert w.clips[0].path == first and sorted(c.path.name for c in w.clips) == names
        # ⑥ 실제 용량 측정
        w.outdir.set(str(tmp_path / "BG"))
        assert w.measure_size()
        assert str(w.btn_start.cget("state")) == "disabled"  # 측정 중 제작 불가 (FFmpeg 1개)
        assert pump(w, lambda: not w.measuring, timeout=120)
        assert w.plan.measured_bps > 0 and "실측" in w.size_text.get(), quiet
        w.minutes.set(3)
        w._options_changed()
        assert w.plan.measured_bps > 0  # 길이만 바꾸면 실측값 유지
        w.profile.set(B.get_profile("small").label)
        w._options_changed()
        assert w.plan.measured_bps == 0  # 화질을 바꾸면 다시 측정해야 함
        played = []
        monkeypatch.setattr(U, "_open_path", played.append)
        assert w.start(confirm=False)
        assert w.current_step() == 6
        assert pump(w, lambda: not w.running, timeout=240)
        assert w.current_step() == 7 and w.last_output.exists(), getattr(w, "error", quiet)
        w._open_output()
        assert played == [w.last_output] and w.played
    finally:
        w.destroy()
