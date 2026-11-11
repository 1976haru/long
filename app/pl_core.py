"""⑥ 음악 플레이리스트 만들기 — 엔진 (UI 없음).

흐름: 배경 MP4(④ 결과 권장) + 음원 여러 곡(기본 15곡 1세트) → 곡 순서대로 이어 붙인 오디오 1개 →
배경 영상을 음악 길이에 맞게 반복/종료 → 플레이리스트 MP4 + 곡 시작 시점 12초 제목 SRT.

원칙
- 원본 음원·영상은 읽기만 한다 (덮어쓰기 금지). 임시 파일은 출력 폴더의 .PLVM_PL_TEMP_* 안에서만 만들고 지운다.
- 오디오: 곡을 순서대로 디코드해 하나의 AAC로 만든다 (곡 사이 공백 없음, MP3 인코더 지연은 FFmpeg가 제거).
  크로스페이드는 사용자가 고를 때만 (기본 없음).
- 영상: SRT만 만들 때와 같이 '영상에 손대지 않아도 되는' 경우(같은 해상도·코덱·H.264 yuv420p 등)는
  -c copy로 반복 연결만 한다 (재인코딩 없음). 해상도 변경·H.265 변환·제목 입히기를 고른 경우에만 영상을 인코딩한다.
- ④에서 만든 배경은 옆에 남긴 정보 파일(<이름>.mp4.plvm_bg.json)의 '한 바퀴 길이'로, 반복할 때 한 바퀴 경계
  (= ④의 디졸브로 이어지는 지점, 키프레임)에서만 이어 붙인다. 정보가 없으면 파일 전체를 반복하고 경계 경고를 띄운다.
- FFmpeg는 FFMPEG_GUARD로 ①④⑤·LIVE와 동시에 돌지 않는다 (UI 쪽에서 획득).
- Python으로 프레임을 디코딩하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, Sequence

from .bg_core import (
    CODEC_H264, CODEC_HEVC, QUALITY_4K, QUALITY_FHD, OutputSpec, _DiskWatch, _dir_bytes, _file_size, _h264_level,
    choose_output_spec, fps_expr, run_ffmpeg_progress,
)
from .core import (
    BuildCancelled, BuildError, VideoInfo, creationflags_no_window, enough_disk_space, escape_concat_path,
    format_duration, probe_video,
)

AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma")
SET_SIZE = 15                 # 기본 1세트 = 15곡 (더 적거나 많아도 만들 수 있지만 화면에 안내)
MAX_TRACKS = 99
TITLE_SECONDS = 12.0          # 곡 시작부터 제목 표시 시간
TITLE_END_GAP = 0.1           # 다음 곡 시작 이 시간 전에는 반드시 닫힌다
AUDIO_KBPS = (320, 256, 192)
DEFAULT_AUDIO_KBPS = 320
AUDIO_RATE = 48000
CROSSFADE_CHOICES = (0.0, 1.0, 2.0, 3.0)
GUARD_OWNER = "playlist"
TEMP_PREFIX = ".PLVM_PL_TEMP_"
OWNER_FILE = "owner.txt"
SIDECAR_SUFFIX = ".plvm_bg.json"   # ④ 결과 옆 정보 파일 (한 바퀴 길이)
AUDIO_TOLERANCE = 0.25        # 최종 오디오 길이 = 계산한 총 재생시간 ± 이 값
AV_TOLERANCE = 0.30           # 영상 길이와 오디오 길이 차이 허용
TITLE_FONT = "Yu Gothic"      # 일본어·한국어(대체 글꼴) 모두 표시 확인 (libass + Windows 글꼴)

_SRT_TIME = r"(\d{2}):([0-5]\d):([0-5]\d),(\d{3})"
_SRT_BLOCK = re.compile(rf"^(\d+)\r?\n{_SRT_TIME} --> {_SRT_TIME}\r?\n(.+?)(?:\r?\n)*$", re.S)
_LEADING_NUMBER = re.compile(r"^\s*(?:track|trk|tr)?\s*[\(\[]?\d{1,3}[\)\]]?\s*(?:[\.\-_、．。:：)\]]+\s*|\s+)", re.I)


# ---------------- 음원 ----------------

@dataclass(frozen=True)
class Track:
    path: Path
    title: str
    duration: float           # 실제 디코드로 확인한 재생시간 (초)
    probe_duration: float     # ffprobe가 알려 준 길이 (VBR MP3 등에서 다를 수 있음)
    codec: str
    sample_rate: int
    channels: int
    size: int
    fingerprint: str          # 중복 검사용 (크기 + 앞·뒤 1 MB SHA256)
    warnings: tuple[str, ...] = ()


def title_from_filename(name: str) -> str:
    """파일명에서 곡 제목: 확장자·앞 번호(01. / 01 - / 1_ / Track 03)·밑줄 정리. 일본어·한글 그대로."""
    stem = Path(name).stem if Path(name).suffix.lower() in AUDIO_EXTS else name
    t = _LEADING_NUMBER.sub("", stem, count=1)
    t = re.sub(r"[_]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -·.")
    return t or stem.strip()


def fingerprint(path: Path) -> str:
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with p.open("rb") as f:
        h.update(f.read(1024 * 1024))
        if size > 2 * 1024 * 1024:
            f.seek(-1024 * 1024, 2)
            h.update(f.read(1024 * 1024))
    return h.hexdigest()


def _run_capture(cmd: list[str], cancel: Event | None, timeout: float = 600) -> tuple[int, str, str]:
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                             encoding="utf-8", errors="replace", creationflags=creationflags_no_window())
    except OSError as e:
        raise BuildError(f"FFmpeg를 실행할 수 없습니다: {e}") from e
    end = time.monotonic() + timeout
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise BuildCancelled("사용자가 작업을 중지했습니다.")
            try:
                out, err = p.communicate(timeout=0.3)
                return p.returncode, out or "", err or ""
            except subprocess.TimeoutExpired:
                if time.monotonic() > end:
                    raise BuildError("시간 초과") from None
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)


def probe_audio(ffprobe: Path, path: Path) -> dict:
    cmd = [str(ffprobe), "-v", "error", "-select_streams", "a:0", "-show_entries",
           "stream=codec_name,sample_rate,channels,duration:format=duration", "-of", "json", str(path)]
    rc, out, err = _run_capture(cmd, None, 60)
    if rc != 0:
        raise BuildError(f"음원을 읽을 수 없습니다 (손상되었거나 음원 파일이 아님): {Path(path).name}")
    data = json.loads(out or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise BuildError(f"오디오가 없는 파일입니다: {Path(path).name}")
    s = streams[0]
    dur = 0.0
    for v in (s.get("duration"), (data.get("format") or {}).get("duration")):
        try:
            dur = float(v)
            break
        except (TypeError, ValueError):
            continue
    return {"codec": str(s.get("codec_name") or ""), "sample_rate": int(s.get("sample_rate") or 0),
            "channels": int(s.get("channels") or 0), "duration": dur}


def decode_duration(ffmpeg: Path, path: Path, cancel: Event | None = None) -> float:
    """끝까지 디코드해 실제 재생시간을 잰다 (손상 검사 겸용). 디코드 오류가 있으면 BuildError."""
    cmd = [str(ffmpeg), "-hide_banner", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0",
           "-f", "null", "-progress", "pipe:1", "-nostats", "-"]
    rc, out, err = _run_capture(cmd, cancel, 600)
    errs = [ln for ln in (err or "").splitlines() if ln.strip()]
    if rc != 0 or errs:
        raise BuildError(f"음원 디코드 중 오류 (손상 가능): {Path(path).name}\n" + "\n".join(errs[-3:]))
    vals = [int(m) for m in re.findall(r"^out_time_us=(\d+)", out, re.M)]
    if not vals or vals[-1] <= 0:
        raise BuildError(f"음원 길이를 확인할 수 없습니다: {Path(path).name}")
    return vals[-1] / 1_000_000


def analyze_track(ffmpeg: Path, ffprobe: Path, path: Path, cancel: Event | None = None, title: str | None = None) -> Track:
    path = Path(path)
    if not path.is_file():
        raise BuildError(f"음원 파일이 없습니다: {path.name}")
    info = probe_audio(ffprobe, path)
    dur = decode_duration(ffmpeg, path, cancel)
    warns = []
    if info["duration"] > 0 and abs(info["duration"] - dur) > 0.5:
        warns.append(f"파일 정보의 길이({format_duration(info['duration'])})와 실제 재생 길이({format_duration(dur)})가 "
                     "다릅니다 — 실제 길이로 계산합니다.")
    if dur < TITLE_SECONDS + 0.2:
        warns.append(f"곡이 {TITLE_SECONDS:g}초보다 짧아 제목 표시가 곡 길이만큼만 나옵니다.")
    return Track(path.resolve(), title if title is not None else title_from_filename(path.name), dur, info["duration"],
                 info["codec"], info["sample_rate"], info["channels"], path.stat().st_size, fingerprint(path),
                 tuple(warns))


def duplicate_findings(tracks: Sequence[Track]) -> list[str]:
    """같은 파일(내용)·같은 경로·같은 제목+길이를 경고 문구로. 제작을 막지는 않는다."""
    out: list[str] = []
    seen_fp: dict[str, int] = {}
    seen_title: dict[tuple[str, int], int] = {}
    for i, t in enumerate(tracks):
        if t.fingerprint in seen_fp:
            out.append(f"{i + 1:02d}번이 {seen_fp[t.fingerprint] + 1:02d}번과 같은 음원입니다 ({t.path.name}).")
        else:
            seen_fp[t.fingerprint] = i
            key = (t.title.strip().lower(), int(round(t.duration)))
            if key in seen_title:
                out.append(f"{i + 1:02d}번이 {seen_title[key] + 1:02d}번과 제목·길이가 같습니다 — 같은 곡인지 확인하세요.")
            else:
                seen_title[key] = i
    return out


# ---------------- 타임라인 · SRT ----------------

@dataclass(frozen=True)
class TimelineEntry:
    number: int
    title: str
    start: float
    end: float
    title_start: float
    title_end: float

    @property
    def label(self) -> str:
        return f"{self.number:02d}. {self.title}"


def build_timeline(tracks: Sequence[Track], crossfade: float = 0.0,
                   title_seconds: float = TITLE_SECONDS) -> tuple[list[TimelineEntry], float]:
    """곡 시작 시각과 제목 표시 구간. 크로스페이드가 있으면 다음 곡이 그만큼 일찍 시작한다."""
    if not tracks:
        return [], 0.0
    xf = max(0.0, float(crossfade))
    if xf > 0 and any(t.duration <= 2 * xf + 1 for t in tracks):
        raise ValueError(f"크로스페이드 {xf:g}초보다 충분히 긴 곡만 쓸 수 있습니다 (곡 길이 > {2 * xf + 1:g}초).")
    starts = []
    pos = 0.0
    for i, t in enumerate(tracks):
        starts.append(pos)
        pos += t.duration - (xf if i < len(tracks) - 1 else 0.0)
    total = pos
    entries = []
    for i, t in enumerate(tracks):
        start = starts[i]
        end = starts[i + 1] if i + 1 < len(tracks) else total
        cap = (starts[i + 1] - TITLE_END_GAP) if i + 1 < len(tracks) else total
        t_end = min(start + title_seconds, cap)
        entries.append(TimelineEntry(i + 1, (t.title or title_from_filename(t.path.name)).replace("\n", " ").strip(),
                                     start, end, start, max(start + 0.5, t_end) if cap - start >= 0.5 else cap))
    return entries, total


def srt_time(sec: float) -> str:
    ms = int(round(max(0.0, sec) * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(entries: Sequence[TimelineEntry]) -> str:
    blocks = [f"{k}\r\n{srt_time(e.title_start)} --> {srt_time(e.title_end)}\r\n{e.label}\r\n"
              for k, e in enumerate(entries, 1)]
    return "\r\n".join(blocks)


def _srt_sec(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def validate_srt(text: str, entries: Sequence[TimelineEntry] | None = None) -> list[str]:
    """SRT 형식·번호·시간 순서·겹침 검사 (+ 타임라인과 일치하는지). 문제 목록 (빈 목록 = 통과)."""
    problems = []
    blocks = [b for b in re.split(r"\r?\n\r?\n", text.strip()) if b.strip()]
    prev_end = -1.0
    for k, b in enumerate(blocks, 1):
        m = _SRT_BLOCK.match(b.strip())
        if not m:
            problems.append(f"{k}번째 자막 형식 오류")
            continue
        g = m.groups()
        if int(g[0]) != k:
            problems.append(f"{k}번째 자막 번호가 {g[0]}입니다")
        a, z = _srt_sec(*g[1:5]), _srt_sec(*g[5:9])
        if z <= a:
            problems.append(f"{k}번째 자막 끝 시각이 시작보다 빠릅니다")
        if a < prev_end:
            problems.append(f"{k}번째 자막이 앞 자막과 겹칩니다")
        prev_end = z
        if entries is not None and k <= len(entries):
            e = entries[k - 1]
            if abs(a - e.title_start) > 0.0015 or abs(z - e.title_end) > 0.0015:
                problems.append(f"{k}번째 자막 시각이 곡 시작과 다릅니다")
            if g[9].strip() != e.label:
                problems.append(f"{k}번째 자막 제목이 다릅니다")
            if k < len(entries) and z > entries[k].start - TITLE_END_GAP + 0.0015:
                problems.append(f"{k}번째 자막이 다음 곡까지 남습니다")
    if entries is not None and len(blocks) != len(entries):
        problems.append(f"자막 수 {len(blocks)}개 / 곡 수 {len(entries)}개")
    return problems


def subtitles_available(ffmpeg: Path) -> bool:
    """이 FFmpeg에 제목 입히기용 subtitles(libass) 필터가 있는가."""
    try:
        rc, out, _ = _run_capture([str(ffmpeg), "-hide_banner", "-filters"], None, 30)
    except BuildError:
        return False
    return rc == 0 and re.search(r"^\s*\S+\s+subtitles\s", out, re.M) is not None


def write_text_new(path: Path, text: str) -> Path:
    """새 파일로만 저장 (이미 있으면 _2, _3 …). UTF-8 (BOM 없음)."""
    path = Path(path)
    cand = path
    k = 2
    while cand.exists():
        cand = path.with_name(f"{path.stem}_{k}{path.suffix}")
        k += 1
    tmp = cand.with_name(cand.name + ".part")
    tmp.write_bytes(text.encode("utf-8"))
    tmp.replace(cand)
    return cand


# ---------------- 배경 ----------------

def sidecar_path(video: Path) -> Path:
    return Path(video).with_name(Path(video).name + SIDECAR_SUFFIX)


def write_bg_sidecar(output: Path, *, cycle: float, duration: float, size: int, width: int, height: int, fps: float,
                     codec: str, repeats: int) -> Path | None:
    """④ 완성 직후 호출: ⑥이 '한 바퀴 경계'에서만 반복하도록 한 바퀴 길이를 남긴다 (실패해도 ④ 결과에는 영향 없음)."""
    data = {"kind": "plvm_bg", "version": 1, "cycle_duration": round(float(cycle), 6), "duration": round(float(duration), 6),
            "size": int(size), "width": int(width), "height": int(height), "fps": float(fps), "codec": codec,
            "repeats": int(repeats)}
    p = sidecar_path(output)
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return p
    except OSError:
        return None


def read_bg_cycle(video: Path, info: VideoInfo) -> float | None:
    """④ 정보 파일이 이 영상과 맞을 때(크기·길이 일치)만 한 바퀴 길이를 돌려준다."""
    try:
        d = json.loads(sidecar_path(video).read_text(encoding="utf-8"))
        cyc = float(d["cycle_duration"])
        if d.get("kind") != "plvm_bg" or int(d["size"]) != info.size or abs(float(d["duration"]) - info.duration) > 0.5:
            return None
        return cyc if 1.0 <= cyc <= info.duration + 0.05 else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


@dataclass(frozen=True)
class LoopPlan:
    unit: float                # 반복 단위 길이 (초)
    outpoint: float | None     # 반복 단위가 파일 일부일 때 잘라 낼 한 바퀴 경계(표시 시각). 실제 자르기는
                               # 제작 때 그 경계 키프레임의 DTS로 한다 (concat outpoint는 DTS 기준이라 B-프레임 때문에)
    repeats: int               # 연결할 단위 수
    seams: tuple[float, ...]   # 출력 기준 반복 이음 시각
    natural: bool              # ④ 한 바퀴 경계에서 잇는가
    note: str


def plan_loop(bg: VideoInfo, cycle: float | None, total: float) -> LoopPlan:
    if total <= 0:
        raise ValueError("음악 길이가 0입니다.")
    if bg.duration + 1e-6 >= total:
        return LoopPlan(bg.duration, None, 1, (), True, "배경이 음악보다 길어 음악이 끝나는 지점에서 함께 끝납니다 (반복 없음).")
    if cycle:
        whole = max(1, int(math.floor(bg.duration / cycle + 1e-6)))
        unit = whole * cycle
        partial = unit < bg.duration - 0.05
        out = unit if partial else None
        reps = max(1, math.ceil(total / unit - 1e-9))
        seams = tuple(round(unit * k, 3) for k in range(1, reps) if unit * k < total)
        return LoopPlan(unit, out, reps, seams, True,
                        f"④에서 만든 배경의 한 바퀴({format_duration(cycle)}) 경계에서 이어 반복합니다 — 이음새도 ④의 디졸브 그대로입니다.")
    reps = max(1, math.ceil(total / bg.duration - 1e-9))
    seams = tuple(round(bg.duration * k, 3) for k in range(1, reps) if bg.duration * k < total)
    return LoopPlan(bg.duration, None, reps, seams, False,
                    "배경 영상 전체를 반복합니다. ④에서 만든 영상이 아니면 반복 지점(끝 → 처음)이 갑자기 바뀔 수 있습니다 — "
                    "④ 배경 만들기로 만든 영상을 권장합니다.")


# ---------------- 계획 ----------------

@dataclass(frozen=True)
class PlaylistPlan:
    tracks: tuple[Track, ...]
    entries: tuple[TimelineEntry, ...]
    total: float
    bg: VideoInfo
    loop: LoopPlan
    copy_video: bool            # True: 영상 재인코딩 없음 (-c copy)
    spec: OutputSpec            # 출력 규격 (copy일 때는 원본 규격 설명용)
    burn_titles: bool
    audio_kbps: int
    crossfade: float
    expected_size: int
    required_bytes: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def live_ready_candidate(self) -> bool:
        """② LIVE DIRECT COPY 후보(H.264 + yuv420p)인가. 실제 판정은 ②의 LIVE READY 검사가 한다."""
        if self.copy_video:
            return self.bg.video_codec == "h264" and self.bg.pix_fmt == "yuv420p"
        return self.spec.codec == CODEC_H264

    @property
    def video_label(self) -> str:
        if self.copy_video:
            return (f"영상 그대로 복사 (재인코딩 없음) · {self.bg.width}×{self.bg.height} {self.bg.video_codec.upper()} "
                    f"{self.bg.fps:g} FPS")
        return self.spec.label + (" + 제목 입히기" if self.burn_titles else "")


def _copy_ok(bg: VideoInfo, spec: OutputSpec, codec: str, burn: bool) -> bool:
    if burn:
        return False
    want = "hevc" if codec == CODEC_HEVC else "h264"
    return (bg.video_codec == want and bg.pix_fmt == "yuv420p" and (bg.width, bg.height) == (spec.width, spec.height))


def build_playlist_plan(tracks: Sequence[Track], bg: VideoInfo, *, cycle: float | None = None,
                        quality: str = QUALITY_FHD, profile: str = "best", codec: str = CODEC_H264,
                        burn_titles: bool = False, audio_kbps: int = DEFAULT_AUDIO_KBPS, crossfade: float = 0.0,
                        hevc_ok: bool = True) -> PlaylistPlan:
    if not tracks:
        raise ValueError("음원을 추가하세요.")
    if len(tracks) > MAX_TRACKS:
        raise ValueError(f"음원은 최대 {MAX_TRACKS}곡까지입니다.")
    if audio_kbps not in AUDIO_KBPS:
        raise ValueError("오디오 비트레이트를 선택하세요.")
    entries, total = build_timeline(tracks, crossfade)
    notes: list[str] = []
    if len(tracks) != SET_SIZE:
        notes.append(f"기본 1세트는 {SET_SIZE}곡입니다 (지금 {len(tracks)}곡).")
    spec = choose_output_spec([bg], quality, hevc_available=hevc_ok, profile=profile, codec=codec)
    if quality == QUALITY_4K and spec.height < 2160:
        notes.append("배경이 4K 원본이 아니어서 4K로 키우지 않습니다 (업스케일 영상을 4K로 표시하지 않음).")
    copy = _copy_ok(bg, spec, spec.codec, burn_titles)
    if copy:
        notes.append("영상은 다시 인코딩하지 않고 그대로 복사합니다 (화질 손실 없음, 빠름).")
    else:
        why = []
        if burn_titles:
            why.append("제목 입히기")
        if (bg.width, bg.height) != (spec.width, spec.height):
            why.append(f"해상도 {bg.width}×{bg.height} → {spec.width}×{spec.height}")
        if bg.video_codec != ("hevc" if spec.codec == CODEC_HEVC else "h264") or bg.pix_fmt != "yuv420p":
            why.append(f"코덱 {bg.video_codec}/{bg.pix_fmt} → {spec.codec}/yuv420p")
        notes.append("영상을 인코딩합니다: " + (", ".join(why) or "출력 규격 맞춤") + ".")
        notes.extend(n for n in spec.notes if n not in notes and "H.265" not in n)
    if spec.codec == CODEC_HEVC or (copy and bg.video_codec == "hevc"):
        notes.append("H.265는 저장·업로드용입니다 — ② LIVE에는 H.264 출력을 쓰세요.")
    loop = plan_loop(bg, cycle, total)
    if not loop.natural:
        notes.append(loop.note)
    audio_bps = audio_kbps * 1000 / 8 * 1.03
    video_bps = (bg.size / max(bg.duration, 0.1)) * 1.02 if copy else spec.bytes_per_second
    expected = int((audio_bps + video_bps) * total)
    audio_tmp = int(audio_bps * total)
    required = expected + audio_tmp + 64 * 1024**2
    return PlaylistPlan(tuple(tracks), tuple(entries), total, bg, loop, copy, spec, bool(burn_titles), int(audio_kbps),
                        float(crossfade), expected, required, tuple(notes))


# ---------------- 명령 ----------------

def audio_command(ffmpeg: Path, tracks: Sequence[Track], out: Path, kbps: int, crossfade: float = 0.0) -> list[str]:
    cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-nostdin"]
    for t in tracks:
        cmd += ["-i", str(t.path)]
    norm = "".join(f"[{i}:a:0]aresample={AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}];"
                   for i in range(len(tracks)))
    n = len(tracks)
    if n == 1:
        graph = norm + "[a0]anull[a]"
    elif crossfade > 0:
        chain, prev = "", "a0"
        for i in range(1, n):
            nxt = "a" if i == n - 1 else f"x{i}"
            chain += f"[{prev}][a{i}]acrossfade=d={crossfade:g}:c1=tri:c2=tri[{nxt}];"
            prev = nxt
        graph = norm + chain.rstrip(";")
    else:
        graph = norm + "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[a]"
    return cmd + ["-filter_complex", graph, "-map", "[a]", "-c:a", "aac", "-b:a", f"{kbps}k", "-ar", str(AUDIO_RATE),
                  "-ac", "2", "-progress", "pipe:1", "-nostats", str(out)]


def boundary_key_dts(ffprobe: Path, path: Path, at: float, fps: float) -> float | None:
    """표시 시각 at에 있는 키프레임 패킷의 DTS (없으면 None). B-프레임 영상은 DTS가 표시 시각보다 앞선다."""
    cmd = [str(ffprobe), "-v", "error", "-select_streams", "v:0", "-read_intervals", f"{max(0.0, at - 2):.3f}%{at + 2:.3f}",
           "-show_entries", "packet=pts_time,dts_time,flags", "-of", "csv=p=0", str(path)]
    rc, out, _ = _run_capture(cmd, None, 120)
    if rc != 0:
        return None
    tol = 0.5 / max(fps, 1.0)
    for line in out.split():
        parts = line.split(",")
        try:
            if len(parts) >= 3 and "K" in parts[2] and abs(float(parts[0]) - at) <= tol:
                return float(parts[1])
        except ValueError:
            continue
    return None


def write_bg_list(bg: VideoInfo, loop: LoopPlan, target: Path, cut_dts: float | None = None) -> None:
    """배경 반복 목록. 한 바퀴 경계에서 자를 때: outpoint = 경계 키프레임 DTS (그 앞 GOP 패킷만 남김),
    duration = 경계 표시 시각 (다음 반복의 시각이 정확히 이어지도록)."""
    lines = ["ffconcat version 1.0"]
    for _ in range(loop.repeats):
        lines.append(f"file '{escape_concat_path(bg.path)}'")
        if loop.outpoint is not None and cut_dts is not None:
            lines.append(f"outpoint {cut_dts:.6f}")
            lines.append(f"duration {loop.outpoint:.6f}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _escape_style(s: str) -> str:
    return s.replace("\\", "").replace("'", "").replace(",", " ")


def title_style(spec_height: int) -> str:
    size = max(16, int(round(22 * spec_height / 1080)))  # libass 기본 화면 높이(288) 기준 크기 → FHD에서 적당
    return f"FontName={_escape_style(TITLE_FONT)},FontSize={size},Outline=2,Shadow=1,Alignment=1,MarginL=40,MarginV=30"


def video_command(ffmpeg: Path, plan: PlaylistPlan, bg_list: Path, audio: Path, out: Path,
                  srt_name: str | None = None) -> list[str]:
    """bg_list·audio·out은 임시 폴더 안 상대 경로가 아니어도 된다. 제목 입히기는 cwd=임시 폴더에서 상대 이름(srt_name)으로."""
    cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-nostdin", "-f", "concat", "-safe", "0",
           "-i", str(bg_list), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0"]
    if plan.copy_video:
        cmd += ["-c", "copy"]
    else:
        s = plan.spec
        vf = (f"scale={s.width}:{s.height}:force_original_aspect_ratio=increase:flags=lanczos,crop={s.width}:{s.height},"
              f"setsar=1,fps={fps_expr(s.fps)},format=yuv420p")
        if plan.burn_titles and srt_name:
            vf += f",subtitles={srt_name}:charenc=UTF-8:force_style='{title_style(s.height)}'"
        gop = max(1, int(round(s.fps * 2)))
        cmd += ["-vf", vf, "-c:v", s.encoder, "-preset", s.preset, "-crf", str(s.crf), "-pix_fmt", "yuv420p",
                "-g", str(gop)]
        if s.codec == CODEC_HEVC:
            cmd += ["-tag:v", "hvc1", "-x265-params", "log-level=error"]
        else:
            cmd += ["-profile:v", "high", "-level:v", _h264_level(s)]
        if s.maxrate_kbps:
            cmd += ["-maxrate", f"{s.maxrate_kbps}k", "-bufsize", f"{s.bufsize_kbps}k"]
        cmd += ["-c:a", "copy"]
    return cmd + ["-t", f"{plan.total:.3f}", "-progress", "pipe:1", "-nostats", str(out)]


# ---------------- 검증 ----------------

@dataclass(frozen=True)
class StreamDurations:
    video: float
    audio: float
    width: int
    height: int
    codec: str
    pix_fmt: str
    fps: float


def probe_streams(ffprobe: Path, path: Path) -> StreamDurations:
    cmd = [str(ffprobe), "-v", "error", "-show_entries",
           "stream=codec_type,codec_name,duration,width,height,pix_fmt,avg_frame_rate:format=duration", "-of", "json",
           str(path)]
    rc, out, _ = _run_capture(cmd, None, 120)
    if rc != 0:
        raise BuildError(f"완성 파일을 읽을 수 없습니다: {Path(path).name}")
    d = json.loads(out or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    if v is None or a is None:
        raise BuildError("완성 파일에 영상 또는 오디오가 없습니다.")
    fmt = float((d.get("format") or {}).get("duration") or 0)

    def dur(s):
        try:
            return float(s.get("duration"))
        except (TypeError, ValueError):
            return fmt
    from .core import parse_fps
    return StreamDurations(dur(v), dur(a), int(v.get("width") or 0), int(v.get("height") or 0),
                           str(v.get("codec_name") or ""), str(v.get("pix_fmt") or ""), parse_fps(v.get("avg_frame_rate")))


def seam_check(ffmpeg: Path, path: Path, times: Sequence[float], cancel: Event | None = None, span: float = 3.0) -> None:
    """반복 이음 앞뒤를 실제로 디코드해 깨진 프레임이 없는지 확인 (FFmpeg 안에서만 디코드)."""
    for t in list(times)[:3]:
        cmd = [str(ffmpeg), "-hide_banner", "-v", "error", "-nostdin", "-ss", f"{max(0.0, t - span / 2):.3f}",
               "-i", str(path), "-t", f"{span:.3f}", "-map", "0:v:0", "-f", "null", "-"]
        rc, _, err = _run_capture(cmd, cancel, 300)
        if rc != 0 or err.strip():
            raise BuildError(f"반복 이음({format_duration(t)}) 디코드 검사 실패:\n{err.strip()[-400:]}")


# ---------------- 제작 ----------------

@dataclass(frozen=True)
class PlaylistResult:
    output: Path
    srt: Path | None
    duration: float
    audio_duration: float
    video_duration: float
    size: int
    width: int
    height: int
    fps: float
    codec: str
    copied_video: bool
    elapsed: float
    natural_loop: bool = True   # 반복이 ④ 한 바퀴 경계(또는 반복 없음)였는가
    loop_repeats: int = 1


def run_playlist_build(*, ffmpeg: Path, ffprobe: Path, plan: PlaylistPlan, output: Path, cancel_event: Event,
                       save_srt: bool = True, progress_cb: Callable[[float, str], None] | None = None) -> PlaylistResult:
    """오디오 합치기 → 배경 반복/종료 + 오디오 → .part.mp4 → 길이·싱크·이음 검사 → 최종 파일. 실패/중지 시 이 작업의
    임시 폴더와 .part.mp4만 지운다. 원본 음원·영상은 읽기만 한다."""
    output = Path(output).resolve()  # FFmpeg를 임시 폴더(cwd)에서 실행하므로 모든 경로는 절대 경로
    output.parent.mkdir(parents=True, exist_ok=True)
    sources = {t.path.resolve() for t in plan.tracks} | {plan.bg.path.resolve()}
    if output.resolve() in sources:
        raise BuildError("원본과 같은 파일명으로 저장할 수 없습니다.")
    if output.exists():
        raise BuildError(f"출력 파일이 이미 존재합니다: {output.name}")
    ok, free, need = enough_disk_space(output.parent, plan.required_bytes)
    if not ok:
        raise BuildError(f"저장 공간 부족: 남음 {free / 1024**3:.1f} GB / 필요 약 {need / 1024**3:.1f} GB "
                         f"(최종 약 {plan.expected_size / 1024**3:.2f} GB + 작업용 임시 파일). 원본과 기존 파일은 그대로입니다.")
    started = time.monotonic()
    temp_dir = output.parent / f"{TEMP_PREFIX}{uuid.uuid4().hex[:10]}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    part = output.with_name(output.name + ".part.mp4")
    try:
        (temp_dir / OWNER_FILE).write_text(part.name, encoding="utf-8")
    except OSError:
        pass
    stop = Event()
    watch = _DiskWatch(output.parent, temp_dir, cancel_event, stop)
    watch.remaining = lambda: max(0, plan.required_bytes - _dir_bytes(temp_dir) - _file_size(part))
    watch.start()
    a_w = 0.25 if not plan.copy_video else 0.55   # 진행률 가중치 (오디오 / 영상)

    def report(frac: float, text: str) -> None:
        if progress_cb:
            progress_cb(min(0.999, max(0.0, frac)), text)

    def run(cmd, base, weight, text, cwd=None):
        try:
            run_ffmpeg_progress(cmd, stop, lambda s: report(base + weight * min(1.0, s / plan.total), text), cwd=cwd)
        except BuildCancelled:
            watch.raise_reason()
        if stop.is_set() or cancel_event.is_set():
            watch.raise_reason()

    try:
        audio = temp_dir / "audio.m4a"
        report(0.0, f"음원 {len(plan.tracks)}곡 잇는 중 (곡 사이 공백 없음)")
        run(audio_command(ffmpeg, plan.tracks, audio, plan.audio_kbps, plan.crossfade), 0.0, a_w, "음원 잇는 중")
        a_dur = probe_audio(ffprobe, audio)["duration"]
        if abs(a_dur - plan.total) > AUDIO_TOLERANCE:
            raise BuildError(f"이어 붙인 음원 길이 확인 실패: 예상 {plan.total:.2f}초, 실제 {a_dur:.2f}초")
        srt_text = build_srt(plan.entries)
        probs = validate_srt(srt_text, plan.entries)
        if probs:
            raise BuildError("SRT 검사 실패: " + "; ".join(probs[:3]))
        srt_name = None
        if plan.burn_titles:
            srt_name = "titles.srt"
            (temp_dir / srt_name).write_bytes(srt_text.encode("utf-8"))
        bg_list = temp_dir / "bg.ffconcat"
        loop, cut = plan.loop, None
        if loop.outpoint is not None:
            cut = boundary_key_dts(ffprobe, plan.bg.path, loop.outpoint, plan.bg.fps)
            if cut is None:  # 경계에 키프레임이 없으면(④ 결과가 아닌 경우 등) 파일 전체 반복으로
                loop = plan_loop(plan.bg, None, plan.total)
        write_bg_list(plan.bg, loop, bg_list, cut)
        vtext = "배경 영상 반복 + 음악 합치는 중 (무손실 복사)" if plan.copy_video else "영상 인코딩 중" + (
            " (곡 제목 입히기)" if plan.burn_titles else "")
        run(video_command(ffmpeg, plan, bg_list, audio, part, srt_name), a_w, 0.95 - a_w, vtext, cwd=temp_dir)
        if not part.exists() or part.stat().st_size == 0:
            raise BuildError("완성 파일이 만들어지지 않았습니다.")
        report(0.96, "길이·싱크 검사 중")
        sd = probe_streams(ffprobe, part)
        if abs(sd.audio - plan.total) > AUDIO_TOLERANCE:
            raise BuildError(f"오디오 길이 검증 실패: 예상 {plan.total:.2f}초, 실제 {sd.audio:.2f}초")
        if abs(sd.video - sd.audio) > max(AV_TOLERANCE, 2.5 / max(sd.fps, 1.0)):
            raise BuildError(f"영상·오디오 길이가 다릅니다 (영상 {sd.video:.2f}초 / 오디오 {sd.audio:.2f}초)")
        if not plan.copy_video and (sd.width, sd.height) != (plan.spec.width, plan.spec.height):
            raise BuildError("완성 영상 해상도가 예상과 다릅니다.")
        if loop.seams:
            report(0.97, "반복 이음 디코드 검사 중")
            try:
                seam_check(ffmpeg, part, loop.seams, stop)
            except BuildCancelled:
                watch.raise_reason()
        part.replace(output)
        srt_path = write_text_new(output.with_suffix(".srt"), srt_text) if save_srt else None
        report(1.0, "완료")
        return PlaylistResult(output, srt_path, plan.total, sd.audio, sd.video, output.stat().st_size, sd.width,
                              sd.height, sd.fps, sd.codec, plan.copy_video, time.monotonic() - started,
                              loop.natural, loop.repeats)
    finally:
        watch.close()
        shutil.rmtree(temp_dir, ignore_errors=True)
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass


def cleanup_stale_temp(folder: Path, max_age_hours: float = 12.0) -> int:
    """강제 종료로 남은 이 기능의 임시 폴더(.PLVM_PL_TEMP_*)와 그 owner가 가리키는 .part.mp4만 지운다."""
    folder = Path(folder)
    n = 0
    now = time.time()
    try:
        dirs = [d for d in folder.iterdir() if d.is_dir() and d.name.startswith(TEMP_PREFIX)]
    except OSError:
        return 0
    for d in dirs:
        try:
            if now - d.stat().st_mtime < max_age_hours * 3600:
                continue
            owner = (d / OWNER_FILE).read_text(encoding="utf-8").strip() if (d / OWNER_FILE).exists() else ""
            if owner and owner.endswith(".part.mp4") and "/" not in owner and "\\" not in owner:
                p = folder / owner
                if p.exists():
                    p.unlink()
            shutil.rmtree(d, ignore_errors=True)
            n += 1
        except OSError:
            continue
    return n


def default_output_name(tracks: Sequence[Track], bg: VideoInfo | None) -> str:
    stamp = time.strftime("%Y%m%d_%H%M")
    return f"Playlist_{len(tracks)}곡_{stamp}.mp4"
