"""⑤ 선택 다운로드 + 라이브러리.

- 사용자가 고른 영상만 받는다 (자동·대량 다운로드 없음). 권리 상태가 '상업적 이용 차단'·'확인 불가'면 받지 않는다.
- 허용 호스트 + redirect 매 단계 재검사, .part 이어받기(같은 주소·서버가 206을 줄 때만), 429/시간 초과 재시도,
  시작 전·도중 디스크 여유 검사, SHA256, 같은 파일 중복 방지, 기존 파일 덮어쓰기 금지.
- 받은 뒤 ffprobe(해상도·FPS·코덱·길이) + 앞부분 디코딩 시험. 권리 증빙은 영상 옆 .rights.json 과 library.json에 남긴다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .core import creationflags_no_window
from .settings import load_settings
from .stock_net import NetError, open_url
from .stock_rights import RightsReview, review
from .stock_sources import LONG_MIN, MODE_LONG, Candidate, Rendition

LIBRARY_FILE = "library.json"
DISK_FLOOR = 1024**3            # 받은 뒤에도 최소 1 GB는 남긴다
UNKNOWN_SIZE_GUESS = 2 * 1024**3
MAX_FILE = 20 * 1024**3
CHUNK = 1024 * 1024
RETRIES = 3


def default_library_dir() -> Path:
    saved = (load_settings().get("stock_search") or {}).get("library")
    if isinstance(saved, str) and saved.strip():
        return Path(saved)
    if os.name == "nt" and Path("D:/").exists():
        return Path("D:/PlaylistStudio_StockLibrary")
    return Path.home() / "Videos" / "PlaylistStudio_StockLibrary"


class DownloadError(RuntimeError):
    pass


@dataclass
class Probe:
    width: int = 0
    height: int = 0
    fps: float = 0.0
    codec: str = ""
    duration: float = 0.0
    playable: bool = False
    error: str = ""

    @property
    def is_4k(self) -> bool:
        return max(self.width, self.height) >= 3840 and min(self.width, self.height) >= 2160


def probe_file(ffprobe: Path, ffmpeg: Path | None, path: Path) -> Probe:
    try:
        p = subprocess.run([str(ffprobe), "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=codec_name,width,height,avg_frame_rate,r_frame_rate:format=duration", "-of", "json",
                            str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                           creationflags=creationflags_no_window())
        data = json.loads(p.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return Probe(error=f"ffprobe 실행 실패: {type(e).__name__}")
    st = (data.get("streams") or [{}])[0] if data.get("streams") else {}
    if not st:
        return Probe(error="영상 스트림이 없습니다 (손상되었거나 영상이 아님).")

    def rate(s):
        try:
            a, b = str(s).split("/")
            return float(a) / float(b) if float(b) else 0.0
        except (ValueError, ZeroDivisionError):
            return 0.0
    pr = Probe(int(st.get("width") or 0), int(st.get("height") or 0),
               round(rate(st.get("avg_frame_rate")) or rate(st.get("r_frame_rate")), 3), str(st.get("codec_name") or ""),
               float((data.get("format") or {}).get("duration") or 0.0))
    if ffmpeg:
        try:
            d = subprocess.run([str(ffmpeg), "-hide_banner", "-v", "error", "-nostdin", "-t", "3", "-i", str(path),
                                "-map", "0:v:0", "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120, creationflags=creationflags_no_window())
            pr.playable = d.returncode == 0
            if not pr.playable:
                pr.error = "디코딩 시험 실패: " + (d.stderr.strip().splitlines() or ["알 수 없음"])[-1][:150]
        except (OSError, subprocess.TimeoutExpired) as e:
            pr.error = f"디코딩 시험 실패: {type(e).__name__}"
    else:
        pr.playable = pr.width > 0
    return pr


def sha256_file(path: Path, cancel: threading.Event | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK * 4), b""):
            if cancel is not None and cancel.is_set():
                raise DownloadError("취소했습니다.")
            h.update(chunk)
    return h.hexdigest()


def _safe_name(s: str, n: int = 60) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", s)
    s = re.sub(r"\s+", "_", s.strip()).strip("._")
    return (s or "video")[:n]


def _ext(url: str) -> str:
    e = Path(urlsplit(url).path).suffix.lower()
    return e if e in (".mp4", ".webm", ".ogv", ".mkv", ".mov", ".m4v") else ".mp4"


class Library:
    """다운로드한 영상 목록 (library.json, 임시 파일 → 교체로 저장). 파일을 임의로 지우지 않는다."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.RLock()

    @property
    def file(self) -> Path:
        return self.root / LIBRARY_FILE

    def entries(self) -> list[dict]:
        with self._lock:
            try:
                data = json.loads(self.file.read_text(encoding="utf-8"))
                return [e for e in data.get("entries", []) if isinstance(e, dict)]
            except (OSError, ValueError, AttributeError):
                return []

    def _save(self, entries: list[dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_name(f"{LIBRARY_FILE}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.file)

    def add(self, entry: dict) -> None:
        with self._lock:
            es = self.entries()
            es.append(entry)
            self._save(es)

    def update(self, path: str, changes: dict) -> dict | None:
        """경로가 같은 항목에 값만 더한다 (다른 항목·파일은 건드리지 않음)."""
        with self._lock:
            es = self.entries()
            hit = None
            for e in es:
                if e.get("path") == path:
                    e.update(changes)
                    hit = e
            if hit is not None:
                self._save(es)
            return hit

    def add_derived(self, source: dict, result) -> dict:
        """무음 사본을 별도 항목으로 등록 (원본 항목·파일은 그대로, 관계·SHA256 기록)."""
        from .media_audio import NONE, RIGHTS_NOTE
        entry = {k: v for k, v in source.items() if k not in ("status", "note")}
        entry.update({
            "key": f"{source.get('key', '')}#noaudio", "path": str(result.output), "size": result.size,
            "sha256": result.output_sha256, "derived_from": {"path": source.get("path"), "sha256": result.source_sha256,
                                                             "operation": "원본 오디오 제거 (영상 스트림 복사)"},
            "audio": {"status": NONE, "error": "", "streams": []},
            "probe": {**(source.get("probe") or {}), "width": result.width, "height": result.height, "fps": round(result.fps, 3),
                      "codec": result.codec, "duration": round(result.duration, 3)},
            "warnings": [RIGHTS_NOTE],
        })
        with self._lock:
            es = self.entries()
            es.append(entry)
            for e in es:
                if e.get("path") == source.get("path"):
                    e["silent_copy"] = {"path": str(result.output), "sha256": result.output_sha256}
            self._save(es)
        return entry

    def find_key(self, key: str) -> dict | None:
        return next((e for e in self.entries() if e.get("key") == key and Path(e.get("path", "")).is_file()), None)

    def find_sha(self, sha: str) -> dict | None:
        return next((e for e in self.entries() if e.get("sha256") == sha and Path(e.get("path", "")).is_file()), None)

    def similar(self, c: Candidate, dur: float) -> list[str]:
        """같은 영상의 다른 해상도 / 같은 제작자·같은 길이 → 유사 경고 (삭제하지 않음)."""
        out = []
        for e in self.entries():
            if not Path(e.get("path", "")).is_file():
                continue
            if e.get("provider") == c.provider and e.get("source_id") == c.source_id:
                out.append(f"같은 원본의 다른 파일: {Path(e['path']).name}")
            elif (c.creator and e.get("creator") == c.creator and dur and
                  abs(float((e.get("probe") or {}).get("duration") or 0) - dur) < 0.6):
                out.append(f"같은 제작자·같은 길이 (유사 가능): {Path(e['path']).name}")
        return out


def _need_space(folder: Path, remaining: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(folder).free
    if free < remaining + DISK_FLOOR:
        raise DownloadError(f"저장 공간이 부족합니다 (남은 {free / 1024**3:.1f} GB, 필요 약 "
                            f"{(remaining + DISK_FLOOR) / 1024**3:.1f} GB). 다른 라이브러리 폴더를 고르세요.")


def download(c: Candidate, rend: Rendition, library: Library, *, mode: str, ffprobe: Path | None, ffmpeg: Path | None,
             cancel: threading.Event, progress: Callable[[float, str], None] | None = None, opener=None, resolver=None,
             check_dns: bool = True, sleep: Callable[[float], None] = time.sleep) -> dict:
    """후보 1개 다운로드 → 검사 → 라이브러리 기록. 반환: 라이브러리 항목 (status: 완료/이미 받음/중복)."""
    rv: RightsReview = review(c.provider, c.license_name, c.license_url, c.creator, c.title, c.page_url)
    if not rv.downloadable:
        raise DownloadError(f"권리 상태 [{rv.state}] — 기본 다운로드가 차단되었습니다. {' '.join(rv.reasons)}")
    have = library.find_key(c.key)
    if have and have.get("rendition") == rend.text:
        return {**have, "status": "이미 받음"}
    folder = library.root / _safe_name(c.provider, 30)
    folder.mkdir(parents=True, exist_ok=True)
    base = f"{_safe_name(c.title, 50)}_{_safe_name(c.source_id.replace('File:', ''), 40)}_{rend.width}x{rend.height}"
    target = folder / (base + _ext(rend.url))
    k = 2
    while target.exists():  # 기존 파일 덮어쓰기 금지
        target = folder / f"{base}_{k}{_ext(rend.url)}"
        k += 1
    part = folder / (base + _ext(rend.url) + ".part")
    side = part.with_name(part.name + ".json")
    expected = rend.size or 0
    have_bytes = 0
    if part.exists():  # 이어받기: 같은 URL로 시작한 이 프로그램의 .part만
        try:
            meta = json.loads(side.read_text(encoding="utf-8"))
            if meta.get("url") == rend.url and meta.get("key") == c.key:
                have_bytes = part.stat().st_size
            else:
                part.unlink()
        except (OSError, ValueError):
            part.unlink(missing_ok=True)
    _need_space(folder, (expected or UNKNOWN_SIZE_GUESS) - have_bytes)
    side.write_text(json.dumps({"url": rend.url, "key": c.key, "started": time.time()}), encoding="utf-8")
    attempt = 0
    while True:
        if cancel.is_set():
            raise DownloadError("취소했습니다. (받은 부분은 .part로 남아 다음에 이어받습니다)")
        headers = {"Range": f"bytes={have_bytes}-"} if have_bytes else {}
        try:
            r = open_url(rend.url, c.provider, "download", headers, timeout=30, opener=opener, resolver=resolver,
                         check_dns=check_dns)
        except NetError as e:
            if e.status == 416 and have_bytes:  # 서버가 이어받기 위치를 거부 → 처음부터
                part.unlink(missing_ok=True)
                have_bytes = 0
                continue
            retry = e.code in ("NETWORK", "RATE_LIMIT") or (e.code == "HTTP_ERROR" and e.status >= 500)
            if not retry or attempt >= RETRIES:
                raise DownloadError(e.message) from None
            attempt += 1
            sleep(min(60.0, e.retry_after) if e.retry_after else 2.0 * 2 ** attempt)
            continue
        try:
            if have_bytes and r.status != 206:  # 이어받기 미지원 → 처음부터 다시
                have_bytes = 0
                mode_w = "wb"
            else:
                mode_w = "ab" if have_bytes else "wb"
            total = expected
            cl = r.headers.get("Content-Length") or r.headers.get("content-length")
            if cl and str(cl).isdigit():
                total = have_bytes + int(cl)
            if total > MAX_FILE:
                raise DownloadError("파일이 너무 큽니다 (20 GB 초과) — 받지 않았습니다.")
            if total:
                _need_space(folder, total - have_bytes)
            done = have_bytes
            last_check = time.monotonic()
            with open(part, mode_w) as f:
                while True:
                    if cancel.is_set():
                        raise DownloadError("취소했습니다. (받은 부분은 .part로 남아 다음에 이어받습니다)")
                    try:
                        chunk = r.stream.read(CHUNK)
                    except (OSError, TimeoutError):
                        raise NetError("NETWORK") from None
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if done > MAX_FILE:
                        raise DownloadError("파일이 너무 큽니다 (20 GB 초과).")
                    if progress:
                        progress(done / total if total else 0.0, f"{done / 1024**2:,.0f} MB" +
                                 (f" / {total / 1024**2:,.0f} MB" if total else ""))
                    if time.monotonic() - last_check > 2.0:
                        last_check = time.monotonic()
                        if shutil.disk_usage(folder).free < DISK_FLOOR:
                            raise DownloadError("저장 공간이 거의 없어 다운로드를 멈췄습니다 (.part 보존).")
            if total and done < total:
                raise NetError("NETWORK", "short read")
            break
        except NetError as e:
            have_bytes = part.stat().st_size if part.exists() else 0
            if attempt >= RETRIES:
                raise DownloadError(e.message + " (받은 부분은 .part로 남아 다음에 이어받습니다)") from None
            attempt += 1
            sleep(2.0 * 2 ** attempt)
        finally:
            try:
                r.stream.close()
            except Exception:
                pass
    if progress:
        progress(1.0, "검사 중 (SHA256 · ffprobe)")
    sha = sha256_file(part, cancel)
    dup = library.find_sha(sha)
    if dup:
        part.unlink(missing_ok=True)  # 방금 받은 같은 파일만 지움 (기존 파일은 그대로)
        side.unlink(missing_ok=True)
        return {**dup, "status": "중복", "note": f"같은 파일이 이미 있습니다: {Path(dup['path']).name}"}
    pr = probe_file(ffprobe, ffmpeg, part) if ffprobe else Probe(error="ffprobe 없음")
    from .media_audio import check_audio
    audio = check_audio(ffprobe, part)  # 원본 오디오 유무 (④⑥ 결과에는 항상 제외 — 기록·표시용)
    os.replace(part, target)
    side.unlink(missing_ok=True)
    warns = []
    if not pr.playable:
        warns.append("재생 검사 실패: " + (pr.error or "알 수 없음"))
    if rend.width and pr.width and (pr.width, pr.height) != (rend.width, rend.height):
        warns.append(f"실제 해상도 {pr.width}×{pr.height}가 메타데이터 {rend.text}와 다릅니다.")
    if rend.is_4k and not pr.is_4k:
        warns.append("메타데이터는 4K였지만 실제 파일은 4K가 아닙니다.")
    if pr.is_4k:
        warns.append("실제 파일 4K 확인 (원본 촬영이 4K인지·업스케일 여부는 판단할 수 없음).")
    if c.duration and pr.duration and abs(pr.duration - c.duration) > 1.5:
        warns.append(f"실제 길이 {pr.duration:.1f}초가 제공처 정보 {c.duration:.1f}초와 다릅니다.")
    if mode == MODE_LONG and pr.duration and pr.duration < LONG_MIN:
        warns.append("실제 길이가 3분 미만입니다 (장편 원본 조건 미달).")
    warns += library.similar(c, pr.duration)
    record = rv.record(provider=c.provider, source_url=c.page_url, creator=c.creator, creator_url=c.creator_url)
    target.with_name(target.name + ".rights.json").write_text(json.dumps(record, ensure_ascii=False, indent=1),
                                                              encoding="utf-8")
    entry = {
        "key": c.key, "provider": c.provider, "source_id": c.source_id, "title": c.title, "creator": c.creator,
        "page_url": c.page_url, "path": str(target), "size": target.stat().st_size, "sha256": sha,
        "rendition": rend.text, "meta": {"width": rend.width, "height": rend.height, "duration": c.duration},
        "probe": {"width": pr.width, "height": pr.height, "fps": pr.fps, "codec": pr.codec,
                  "duration": round(pr.duration, 3), "playable": pr.playable, "error": pr.error},
        "audio": audio.to_json(),
        "rights": record, "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": mode, "warnings": warns,
    }
    library.add(entry)
    return {**entry, "status": "완료"}
