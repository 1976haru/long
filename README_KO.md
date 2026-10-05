# Playlist Long Video Maker v0.3

CapCut에서 **자막·이미지·파형·음원·구독/좋아요 요소까지 완성한 SET MP4**를 장시간 플레이리스트로 자동 조립하는 Windows 도구입니다.

## v0.3 핵심

- **회차 기준 반복**: 49분 26초짜리 SET을 10회 선택하면 SET 10개를 온전히 이어 약 `08:14:20`으로 끝납니다. 마지막 SET을 중간에서 자르지 않습니다.
- 여러 SET을 넣으면 **선택한 SET 전체 순서 1바퀴가 1회차**입니다.
- 시간 기준도 유지: 3/5/8/10/11시간 또는 직접 입력. 이 모드는 목표시간에서 끝나므로 마지막 SET이 중간에서 끊길 수 있습니다.
- **자동 대기열 최대 5개**: 서로 다른 작업을 등록하고 순서대로 자동 제작합니다.
- **저메모리 설계**: Python이 영상 프레임을 읽지 않고 FFmpeg 프로세스도 항상 1개만 실행합니다.
- **원본 화질·음질 보존**: `-c copy` stream copy만 사용하며 영상/오디오를 재인코딩하지 않습니다.
- 다중 SET 규격이 다르면 자동 변환하지 않고 작업을 막습니다.
- 완성 후 ffprobe로 재생시간과 주요 스트림 속성을 다시 검사합니다.
- `.part.mp4`와 임시 폴더를 사용해 실패/중지 시 불완전 파일을 정리합니다.
- 장시간 대기열 동안 Windows 절전 방지 옵션을 제공합니다.
- 대기열을 자동 저장해 프로그램 재실행 시 복구합니다.

## v0.4 개발 중 — 24H LIVE (단일 MP4 / 여러 MP4 Playlist)

상단 `● 24H LIVE` 버튼 → **24H Playlist LIVE Studio** 창에서 완성 MP4 1개 또는 여러 개(Playlist, 순서대로 반복)를 YouTube LIVE로 무한 반복 송출합니다.

1. `영상 선택` → 자동으로 **LIVE READY** 검사 (아니면 `LIVE READY 파일 만들기`로 PC에서 한 번만 변환)
2. 실행 위치: **무료 Cloud (권장, PC를 꺼도 방송 계속)** 또는 **내 PC**
3. 무료 Cloud는 처음 한 번 `처음 설정 도우미` (Oracle Always Free 서버 연결 → 자동 준비)
4. YouTube Live Control Room의 Stream Key 입력 (필요하면 `이 PC에 안전하게 기억`)
5. `▶ 24H LIVE 시작` → 상태 확인 → `■ LIVE 종료`

- LIVE READY 파일은 재인코딩 없이 그대로 송출합니다 (**DIRECT COPY**: CPU/RAM 매우 낮음).
- 이 프로그램은 **유료 Cloud 자원을 자동 생성하지 않습니다.** 무료 Cloud를 쓸 수 없으면 `내 PC에서 LIVE`를 사용하세요. 자세한 내용: `docs/FREE_CLOUD.md`

- 처음 테스트는 YouTube Live Control Room에서 **비공개/일부공개** 스트림으로 확인하세요.
- 기존 장시간 MP4 제작 기능과 사용법은 그대로입니다. LIVE는 별도 모듈(`app/live_*.py`)입니다.
- FFmpeg는 계속 동시에 1개만 실행됩니다: 제작 중에는 LIVE 불가, LIVE 중에는 제작 불가.
- 끊기면 5→10→30→60초 간격으로 자동 재접속, 종료 시 FFmpeg를 정상 종료합니다.
- Cloud LIVE 중 PC 프로그램을 닫으면 기본은 **PC만 종료** (Cloud 방송은 계속).
- **여러 영상 Playlist**: LIVE READY MP4 2~20개를 A→B→C→A… 순서로 재인코딩 없이 반복합니다. 모든 영상의 해상도/FPS/오디오가 같아야 합니다.
- **보관 안전 모드** (선택): YouTube는 12시간을 넘는 LIVE를 보관하지 못할 수 있어, 11시간 50분에서 송출을 안전 종료하고 다음 세션을 기다립니다. 11:50은 YouTube 공식 숫자가 아니라 이 프로그램의 안전 여유값입니다.
- **YouTube 자동 교체** (개발 중, 실제 YouTube 미검증): YouTube API로 연결하면 11시간 50분마다 새 방송으로 자동 교체하고 송출은 계속합니다. Google OAuth 앱이 Testing 상태면 연결이 7일 후 만료될 수 있습니다. 이번 버전은 PC 프로그램이 켜져 있어야 교체됩니다. 자세한 내용: `docs/YOUTUBE_ROLLOVER.md`
- Stream Key는 로그/설정 파일에 남기지 않으며, 기억 옵션은 Windows DPAPI 암호화 파일로만 저장합니다. 자세한 내용: `docs/LIVE_ARCHITECTURE.md`

## 가장 쉬운 사용법

1. `RUN_WINDOWS.bat` 실행
2. `＋ 영상 추가`에서 CapCut 완성 SET MP4 선택
3. **회차 기준**에서 `5회 / 10회 / 15회 / 20회` 또는 직접 회차 입력
4. 저장 위치 확인
5. `＋ 현재 설정을 대기열에 추가`
6. 다음 작업도 같은 방식으로 추가 (최대 5개)
7. `▶ 대기열 자동 시작`

### 회차 기준 예

- 원본 SET: `00:49:26`
- 10회: `08:14:20`
- 15회: `12:21:30`

회차 모드는 **SET 경계에서만 종료**하므로 자막/음악/영상이 중간에서 끊기지 않습니다.

## 화질 보존 원칙

핵심 FFmpeg 옵션:

```text
-c copy
```

영상/오디오를 다시 압축하지 않고 기존 압축 스트림을 새 MP4 컨테이너로 연결하므로 **재인코딩에 따른 세대 손실(generation loss)이 없습니다.**

## 메모리 사용 원칙

- Python은 영상 프레임을 디코딩하지 않습니다.
- 대기열 5개를 등록해도 FFmpeg는 항상 1개씩 순차 실행합니다.
- PyAV 같은 프레임 처리 라이브러리를 런타임에 추가하지 않습니다.
- 진행률은 FFmpeg `-progress pipe:1` 출력만 읽습니다.

## FFmpeg

프로그램은 이전 설정, 프로젝트 `bin`, PATH, WinGet Gyan.FFmpeg 설치 폴더 등을 자동 탐색합니다. 못 찾으면 `FFmpeg 설정`에서 `ffmpeg.exe`를 한 번 지정하세요. `ffprobe.exe`는 같은 폴더에 있어야 합니다.

## 테스트

```bat
py -3 -m pip install pytest
py -3 -m pytest -q
py -3 -m compileall -q .
```

## GitHub / Codex / Claude Code

- Codex 작업 규칙: `AGENTS.md`
- Claude Code 작업 규칙: `CLAUDE.md`
- 로컬 개발: `docs/DEVELOPMENT.md`
- 전체 설계: `docs/MASTER_PLAN.md`
- 기술 선택: `docs/TECH_NOTES.md`
