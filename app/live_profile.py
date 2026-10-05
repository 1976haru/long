"""LIVE 송출 설정 모델과 Stream Key 보안 유틸리티.

장시간 MP4 제작 경로(app/core.py)와 완전히 분리된 LIVE 전용 모듈이다.
Stream Key는 repr/log/예외 메시지에 그대로 노출하지 않는다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

MASK = "********"
MODE_COPY = "copy"
MODE_TRANSCODE = "transcode"
_PARTIAL_FILL = "••••••••"
_PARTIAL_MIN_LEN = 12

YOUTUBE_RTMPS_INGEST = "rtmps://a.rtmps.youtube.com:443/live2"
YOUTUBE_RTMP_INGEST = "rtmp://a.rtmp.youtube.com/live2"


def mask_secret(value: str | None) -> str:
    """UI 표시용 마스킹. 충분히 긴 값만 앞/뒤 4자를 보여주고 나머지는 숨긴다."""
    if not value:
        return ""
    value = str(value).strip()
    if len(value) < _PARTIAL_MIN_LEN:
        return MASK
    return f"{value[:4]}{_PARTIAL_FILL}{value[-4:]}"


def redact(text: str, secrets: Iterable[str | None]) -> str:
    """로그/예외 문자열에서 secret을 완전히 가린다 (부분 노출 없음)."""
    for s in secrets:
        if s and s.strip():
            text = text.replace(s, MASK)
            text = text.replace(s.strip(), MASK)
    return text


@dataclass(frozen=True)
class LiveProfile:
    """재사용 가능한 송출 프로필. Stream Key는 포함하지 않는다."""

    name: str
    ingest_url: str
    video_bitrate_kbps: int = 8000
    audio_bitrate_kbps: int = 128
    fps: int = 30
    keyframe_seconds: int = 2
    reconnect: bool = True

    def to_config(self, input_path: Path, stream_key: str) -> "LiveConfig":
        return LiveConfig(
            input_path=Path(input_path),
            ingest_url=self.ingest_url,
            stream_key=stream_key,
            video_bitrate_kbps=self.video_bitrate_kbps,
            audio_bitrate_kbps=self.audio_bitrate_kbps,
            fps=self.fps,
            keyframe_seconds=self.keyframe_seconds,
        )


YOUTUBE_DEFAULT_PROFILE = LiveProfile(name="YouTube 1080p30 (RTMPS)", ingest_url=YOUTUBE_RTMPS_INGEST)


@dataclass(frozen=True)
class LivePreset:
    """UI 송출 품질 preset. 해상도는 바꾸지 않고 입력 해상도 그대로 송출한다."""

    key: str
    label: str
    input_height: int
    video_bitrate_kbps: int
    audio_bitrate_kbps: int = 128
    fps: int = 30
    keyframe_seconds: int = 2


LIVE_PRESETS = (
    LivePreset("720p30", "720p 입력용 저부하 (5 Mbps)", 720, 5000),
    LivePreset("1080p30", "1080p 입력용 안정형 (8 Mbps)", 1080, 8000),
    LivePreset("1080p30hq", "1080p 입력용 고화질 (10 Mbps)", 1080, 10000),
)
DEFAULT_PRESET_KEY = "1080p30"


def preset_by_key(key: str) -> LivePreset:
    for p in LIVE_PRESETS:
        if p.key == key:
            return p
    raise KeyError(key)


def recommend_preset(height: int) -> LivePreset:
    """입력 영상 높이에 맞는 preset 추천. 720p 이하는 저부하, 그 외는 1080p 안정형."""
    return preset_by_key("720p30" if 0 < height <= 720 else DEFAULT_PRESET_KEY)


@dataclass(frozen=True)
class LiveConfig:
    """실제 송출 세션 설정. stream_key는 repr에서 제외된다."""

    input_path: Path
    ingest_url: str
    stream_key: str = field(repr=False)
    video_bitrate_kbps: int = 8000
    audio_bitrate_kbps: int = 128
    fps: int = 30
    keyframe_seconds: int = 2
    audio_sample_rate: int = 44100
    # "copy" = DIRECT COPY (재인코딩 없음, LIVE READY 파일 전용) / "transcode" = libx264 실시간 인코딩
    mode: str = MODE_TRANSCODE
    # "" = 단일 파일 / "concat" = Playlist manifest(ffconcat). Playlist는 DIRECT COPY 전용.
    input_format: str = ""

    def __post_init__(self):
        object.__setattr__(self, "input_path", Path(self.input_path))

    @property
    def masked_key(self) -> str:
        return MASK if self.stream_key else ""


class LiveConfigError(ValueError):
    """LIVE 설정 오류. 메시지에 Stream Key를 넣지 않는다."""


def validate_live_config(config: LiveConfig, *, check_input: bool = True) -> None:
    if not (100 <= config.video_bitrate_kbps <= 51000):
        raise LiveConfigError("영상 비트레이트는 100~51000 kbps 범위여야 합니다.")
    if not (32 <= config.audio_bitrate_kbps <= 512):
        raise LiveConfigError("오디오 비트레이트는 32~512 kbps 범위여야 합니다.")
    if not (1 <= config.fps <= 60):
        raise LiveConfigError("FPS는 1~60 범위여야 합니다.")
    if not (1 <= config.keyframe_seconds <= 4):
        raise LiveConfigError("키프레임 간격은 1~4초 범위여야 합니다.")
    if config.mode not in (MODE_COPY, MODE_TRANSCODE):
        raise LiveConfigError("송출 방식이 올바르지 않습니다.")
    if config.input_format not in ("", "concat"):
        raise LiveConfigError("입력 형식이 올바르지 않습니다.")
    if config.input_format == "concat" and config.mode != MODE_COPY:
        raise LiveConfigError("여러 영상 Playlist는 DIRECT COPY(재인코딩 없음)로만 송출합니다.")
    if config.audio_sample_rate not in (44100, 48000):
        raise LiveConfigError("오디오 샘플레이트는 44100 또는 48000 이어야 합니다.")
    if check_input:
        p = config.input_path
        if not p.is_file():
            raise LiveConfigError(f"입력 영상을 찾을 수 없습니다: {p.name}")


class StreamKeyStore(Protocol):
    """Stream Key 저장 계층. settings.json과 분리해 향후 DPAPI/Credential Manager로 교체한다."""

    def get(self) -> str | None: ...

    def set(self, value: str) -> None: ...

    def clear(self) -> None: ...


class MemoryStreamKeyStore:
    """프로세스 메모리에만 보관 (디스크 저장 없음). Phase 1 기본값."""

    def __init__(self, value: str | None = None):
        self._value = value

    def get(self) -> str | None:
        return self._value

    def set(self, value: str) -> None:
        self._value = value

    def clear(self) -> None:
        self._value = None

    def __repr__(self) -> str:
        return f"MemoryStreamKeyStore(value={MASK if self._value else None!r})"


class EnvStreamKeyStore:
    """개발용: 환경변수에서만 읽는다. 저장하지 않는다."""

    def __init__(self, var: str = "PLVM_STREAM_KEY"):
        self.var = var

    def get(self) -> str | None:
        v = os.environ.get(self.var, "").strip()
        return v or None

    def set(self, value: str) -> None:
        raise NotImplementedError("환경변수 Stream Key 저장소는 읽기 전용입니다.")

    def clear(self) -> None:
        raise NotImplementedError("환경변수 Stream Key 저장소는 읽기 전용입니다.")
