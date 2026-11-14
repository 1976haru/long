"""배경 영상의 원본 오디오 — 검사(ffprobe)와 '오디오 없는 사본'(스트림 복사) 만들기. ④⑤⑥ 공용.

원칙
- 오디오 '스트림'이 있는지로만 판단한다 (조용한 구간이 있다고 오디오가 없다고 보지 않음). 검사 실패는 '확인 불가'.
- ④ 배경 만들기·⑥ 플레이리스트는 영상 스트림만 쓰므로 원본 오디오는 항상 결과에 들어가지 않는다.
  이 모듈의 무음 사본은 사용자가 따로 원할 때만 만든다 (저장 공간을 늘리므로 자동으로 만들지 않음).
- 무음 사본은 영상 스트림을 그대로 복사한다 (-c:v copy -an -sn -dn). 컨테이너·코덱 조합이 맞지 않으면
  재인코딩하지 않고 이유를 알린다. 원본은 읽기만 하고, 임시 .part → 검증 → 이름 바꾸기로 끝낸다.
- 사본에는 원본의 권리 기록을 연결하지만, 오디오를 뺐다고 권리 문제가 해결되는 것은 아니라고 적는다.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Callable

from .core import BuildCancelled, BuildError, creationflags_no_window, parse_fps

PRESENT, NONE, UNKNOWN = "present", "none", "unknown"
LABEL_PRESENT = "🔊 오디오 있음 — 배경 제작 시 자동 제외"
LABEL_NONE = "🔇 오디오 없음 — 제거 불필요"
LABEL_UNKNOWN = "⚠ 오디오 확인 불가"
EXCLUDE_OPTION = "배경 영상의 원본 오디오 자동 제외 (권장)"
SILENT_OPTION = "오디오 없는 MP4를 별도로 저장"
SETTINGS_KEY = "bg_audio"            # {"exclude": true(항상), "save_silent_copy": false}
SILENT_SUFFIX = "_noaudio"
DERIVED_SUFFIX = ".derived.json"
DISK_FLOOR = 1024**3                 # 사본을 만든 뒤에도 1 GB 이상 남김
RIGHTS_NOTE = "오디오를 제거해도 영상의 저작권·이용 조건은 그대로입니다 (권리 문제가 해결된 것이 아님)."

# 실제 컨테이너(ffprobe format_name) → 스트림 복사로 담을 사본 형식. 파일 확장자가 아니라 실제 형식으로 판단한다
# (⑤ 다운로드 파일은 URL 확장자를 따르므로 내용과 다를 수 있음).
_MOV_FAMILY_MP4 = {"h264", "hevc", "av1", "mpeg4", "vp9"}   # MP4로 그대로 담을 수 있는 코덱
_MOV_FAMILY_MOV = {"prores", "mjpeg"}                       # MOV로만
_WEBM_CODECS = {"vp8", "vp9", "av1"}
_EXT_FAMILY = {".mp4": "mov", ".m4v": "mov", ".mov": "mov", ".webm": "mkv", ".mkv": "mkv"}


@dataclass(frozen=True)
class AudioStream:
    index: int
    codec: str
    channels: int
    language: str = ""

    @property
    def text(self) -> str:
        ch = {1: "모노", 2: "스테레오"}.get(self.channels, f"{self.channels}ch" if self.channels else "?ch")
        return f"{self.codec or '?'} {ch}" + (f" ({self.language})" if self.language and self.language != "und" else "")


@dataclass(frozen=True)
class AudioCheck:
    status: str                                   # present | none | unknown
    streams: tuple[AudioStream, ...] = ()
    error: str = ""

    @property
    def label(self) -> str:
        if self.status == PRESENT:
            return LABEL_PRESENT
        if self.status == NONE:
            return LABEL_NONE
        return f"{LABEL_UNKNOWN} — {self.error or '원인 미상'} (다시 검사)"

    @property
    def detail(self) -> str:
        if self.status != PRESENT:
            return self.label
        return f"{LABEL_PRESENT} · 오디오 {len(self.streams)}개: " + ", ".join(s.text for s in self.streams)

    @property
    def short(self) -> str:
        """목록 칸용 짧은 표시."""
        if self.status == PRESENT:
            return f"🔊 {len(self.streams)}개 · 제외" if len(self.streams) > 1 else f"🔊 {self.streams[0].codec} · 제외"
        return "🔇 없음" if self.status == NONE else "⚠ 확인 불가"

    def to_json(self) -> dict:
        return {"status": self.status, "error": self.error,
                "streams": [{"index": s.index, "codec": s.codec, "channels": s.channels, "language": s.language}
                            for s in self.streams]}

    @classmethod
    def from_json(cls, d) -> "AudioCheck | None":
        if not isinstance(d, dict) or d.get("status") not in (PRESENT, NONE, UNKNOWN):
            return None
        return cls(d["status"], tuple(AudioStream(int(s.get("index", 0)), str(s.get("codec", "")),
                                                  int(s.get("channels") or 0), str(s.get("language", "")))
                                      for s in d.get("streams") or []), str(d.get("error", "")))


def _run(cmd: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          stdin=subprocess.DEVNULL, creationflags=creationflags_no_window())


def check_audio(ffprobe: Path | None, path: Path) -> AudioCheck:
    """파일의 오디오 스트림 검사. 실행·해석 실패는 UNKNOWN(원인 포함) — 절대 '없음'으로 보지 않는다."""
    path = Path(path)
    if not ffprobe:
        return AudioCheck(UNKNOWN, error="ffprobe 없음")
    if not path.is_file():
        return AudioCheck(UNKNOWN, error="파일 없음")
    try:
        p = _run([str(ffprobe), "-v", "error", "-show_entries",
                  "stream=index,codec_type,codec_name,channels:stream_tags=language", "-of", "json", str(path)])
    except subprocess.TimeoutExpired:
        return AudioCheck(UNKNOWN, error="검사 시간 초과")
    except OSError as e:
        return AudioCheck(UNKNOWN, error=f"ffprobe 실행 실패 ({type(e).__name__})")
    if p.returncode != 0:
        last = (p.stderr or "").strip().splitlines()
        return AudioCheck(UNKNOWN, error="파일을 읽을 수 없음" + (f": {last[-1][:120]}" if last else ""))
    try:
        streams = json.loads(p.stdout or "{}").get("streams")
    except ValueError:
        return AudioCheck(UNKNOWN, error="검사 결과 형식 오류")
    if not isinstance(streams, list) or not streams:
        return AudioCheck(UNKNOWN, error="스트림 정보 없음 (손상 가능)")
    aud = tuple(AudioStream(int(s.get("index") or 0), str(s.get("codec_name") or ""), int(s.get("channels") or 0),
                            str((s.get("tags") or {}).get("language") or ""))
                for s in streams if s.get("codec_type") == "audio")
    return AudioCheck(PRESENT, aud) if aud else AudioCheck(NONE)


# ---------------- 무음 사본 ----------------

@dataclass(frozen=True)
class VideoStreamInfo:
    codec: str
    width: int
    height: int
    fps: float
    duration: float
    audio_streams: int
    frames: int = 0
    format_name: str = ""


def video_stream_info(ffprobe: Path, path: Path) -> VideoStreamInfo:
    p = _run([str(ffprobe), "-v", "error", "-show_entries",
              "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,duration,nb_frames"
              ":format=duration,format_name",
              "-of", "json", str(path)], 120)
    if p.returncode != 0:
        raise BuildError(f"영상을 읽을 수 없습니다: {Path(path).name}")
    d = json.loads(p.stdout or "{}")
    streams = d.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        raise BuildError(f"영상 스트림이 없습니다: {Path(path).name}")
    fmt = float((d.get("format") or {}).get("duration") or 0)
    try:
        dur = float(v.get("duration"))
    except (TypeError, ValueError):
        dur = fmt
    fps = parse_fps(v.get("avg_frame_rate")) or parse_fps(v.get("r_frame_rate"))
    try:
        frames = int(v.get("nb_frames") or 0)
    except ValueError:
        frames = 0
    return VideoStreamInfo(str(v.get("codec_name") or ""), int(v.get("width") or 0), int(v.get("height") or 0), fps, dur,
                           sum(1 for s in streams if s.get("codec_type") == "audio"), frames,
                           str((d.get("format") or {}).get("format_name") or ""))


def _family(src: Path, format_name: str = "") -> str | None:
    f = (format_name or "").lower()
    if "mp4" in f or "mov" in f:
        return "mov"
    if "matroska" in f or "webm" in f:
        return "mkv"
    if f:
        return None
    return _EXT_FAMILY.get(Path(src).suffix.lower())


def copy_format(src: Path, codec: str, format_name: str = "") -> tuple[str | None, str]:
    """(사본 확장자, 문제 문구). 스트림 복사로 담을 수 없으면 확장자 None + 이유 (자동 재인코딩하지 않음)."""
    fam = _family(src, format_name)
    shown = format_name or Path(src).suffix.lower() or "확장자 없음"
    if fam is None:
        return None, (f"'{shown}' 형식은 무음 사본(스트림 복사)을 지원하지 않습니다. MP4·MOV·WebM·MKV만 가능합니다.")
    if fam == "mov":
        if codec in _MOV_FAMILY_MP4:
            return ".mp4", ""
        if codec in _MOV_FAMILY_MOV:
            return ".mov", ""
        return None, (f"영상 코덱 {codec or '알 수 없음'}은(는) MP4/MOV로 그대로 복사해 담을 수 없어 무음 사본을 만들지 않습니다 "
                      "(화질 보존을 위해 자동 재인코딩하지 않음).")
    return (".webm" if codec in _WEBM_CODECS else ".mkv"), ""


def container_problem(src: Path, codec: str, format_name: str = "") -> str:
    """스트림 복사로 담을 수 없으면 이유 문구 (담을 수 있으면 빈 문자열)."""
    return copy_format(src, codec, format_name)[1]


def silent_copy_target(src: Path, out_dir: Path | None = None, ext: str | None = None) -> Path:
    src = Path(src)
    ext = ext or copy_format(src, "h264")[0] or src.suffix.lower()
    base = (Path(out_dir) if out_dir else src.parent) / f"{src.stem}{SILENT_SUFFIX}{ext}"
    cand, k = base, 2
    while cand.exists():
        cand = base.with_name(f"{base.stem}_{k}{base.suffix}")
        k += 1
    return cand


def sha256_file(path: Path, cancel: Event | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            if cancel is not None and cancel.is_set():
                raise BuildCancelled("사용자가 작업을 중지했습니다.")
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class SilentCopyResult:
    output: Path
    sidecar: Path
    source_sha256: str
    output_sha256: str
    codec: str
    width: int
    height: int
    fps: float
    duration: float
    size: int
    removed_streams: int
    warnings: tuple[str, ...] = field(default_factory=tuple)


def make_silent_copy(*, ffmpeg: Path, ffprobe: Path, src: Path, out: Path | None = None, cancel: Event | None = None,
                     progress: Callable[[float, str], None] | None = None,
                     disk_free: Callable[[Path], int] | None = None) -> SilentCopyResult:
    """영상 스트림만 복사한 '오디오 없는 사본'. 원본은 바꾸지 않는다. 실패·취소 시 이 작업의 .part만 지운다."""
    from .bg_core import run_ffmpeg_progress  # 순환 import 방지
    src = Path(src).resolve()
    cancel = cancel or Event()
    if not src.is_file():
        raise BuildError("원본 파일이 없습니다.")
    st0 = src.stat()
    info = video_stream_info(ffprobe, src)
    ext, why = copy_format(src, info.codec, info.format_name)
    if why:
        raise BuildError(why)
    out = Path(out).resolve() if out else silent_copy_target(src, ext=ext)
    if out == src:
        raise BuildError("원본과 같은 파일로 저장할 수 없습니다.")
    if out.exists():
        raise BuildError(f"이미 있는 파일입니다: {out.name} (덮어쓰지 않음)")
    out.parent.mkdir(parents=True, exist_ok=True)
    need = st0.st_size + DISK_FLOOR  # 사본 크기 ≤ 원본 크기 (오디오만 빠짐) + 여유
    free = (disk_free or (lambda d: shutil.disk_usage(d).free))(out.parent)
    if free < need:
        raise BuildError(f"저장 공간 부족: 남음 {free / 1024**3:.2f} GB / 필요 약 {need / 1024**3:.2f} GB "
                         f"(사본 최대 {st0.st_size / 1024**2:,.0f} MB + 여유 1 GB). 원본은 그대로입니다.")
    part = out.with_name(out.stem + ".part" + out.suffix)
    if part.exists():
        raise BuildError(f"이전 작업의 임시 파일이 남아 있습니다: {part.name}")
    report = progress or (lambda f, t: None)
    try:
        report(0.0, "원본 확인 중 (SHA256)")
        src_sha = sha256_file(src, cancel)
        report(0.15, "오디오를 빼고 영상만 복사하는 중 (재인코딩 없음)")
        cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-nostdin", "-i", str(src),
               "-map", "0:v:0", "-c:v", "copy", "-an", "-sn", "-dn", "-map_metadata", "0",
               "-progress", "pipe:1", "-nostats", str(part)]
        total = max(info.duration, 0.1)
        run_ffmpeg_progress(cmd, cancel, lambda s: report(0.15 + 0.6 * min(1.0, s / total), "영상만 복사하는 중"))
        if cancel.is_set():
            raise BuildCancelled("사용자가 작업을 중지했습니다.")
        report(0.8, "사본 검사 중")
        res = video_stream_info(ffprobe, part)
        problems = []
        if res.audio_streams != 0:
            problems.append(f"오디오 스트림 {res.audio_streams}개가 남음")
        if res.codec != info.codec:
            problems.append(f"영상 코덱이 {info.codec} → {res.codec}")
        if (res.width, res.height) != (info.width, info.height):
            problems.append("해상도가 다름")
        if info.fps and abs(res.fps - info.fps) > 0.01:
            problems.append(f"FPS {info.fps:g} → {res.fps:g}")
        tol = max(0.1, 2.0 / max(info.fps, 1.0))
        if info.duration and abs(res.duration - info.duration) > tol:
            problems.append(f"길이 {info.duration:.3f}초 → {res.duration:.3f}초")
        dec = _run([str(ffmpeg), "-hide_banner", "-v", "error", "-nostdin", "-i", str(part), "-map", "0:v:0",
                    "-t", "5", "-f", "null", "-"], 300)
        if dec.returncode != 0 or (dec.stderr or "").strip():
            problems.append("디코딩 시험 실패")
        if problems:
            raise BuildError("무음 사본 검증 실패: " + ", ".join(problems))
        st1 = src.stat()
        if (st1.st_size, st1.st_mtime_ns) != (st0.st_size, st0.st_mtime_ns):
            raise BuildError("작업 중 원본 파일이 바뀌었습니다. 사본을 만들지 않았습니다.")
        out_sha = sha256_file(part, cancel)
        part.replace(out)
        side = _write_derived(src, out, src_sha, out_sha, info, res)
        report(1.0, "완료")
        warns = []
        if not info.audio_streams:
            warns.append("원본에 오디오가 없어 사본은 원본과 같은 영상입니다 (불필요한 사본).")
        return SilentCopyResult(out, side, src_sha, out_sha, res.codec, res.width, res.height, res.fps, res.duration,
                                out.stat().st_size, info.audio_streams, tuple(warns))
    finally:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass


def _write_derived(src: Path, out: Path, src_sha: str, out_sha: str, info: VideoStreamInfo,
                   res: VideoStreamInfo) -> Path:
    rights = None
    for cand in (src.with_name(src.name + ".rights.json"),):
        try:
            rights = json.loads(cand.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    rec = {
        "kind": "plvm_derived", "version": 1, "operation": "원본 오디오 제거 (영상 스트림 복사, 재인코딩 없음)",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": {"path": str(src), "sha256": src_sha, "audio_streams": info.audio_streams},
        "output": {"path": str(out), "sha256": out_sha, "codec": res.codec, "width": res.width, "height": res.height,
                   "fps": res.fps, "duration": round(res.duration, 3)},
        "rights_from_source": rights, "rights_note": RIGHTS_NOTE,
    }
    side = out.with_name(out.name + DERIVED_SUFFIX)
    side.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    if rights is not None:  # ⑤ 라이브러리 규칙대로 같은 권리 기록을 사본 옆에도 둔다 (증빙 연결)
        out.with_name(out.name + ".rights.json").write_text(
            json.dumps({**rights, "derived_from": str(src), "note": RIGHTS_NOTE}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    return side
