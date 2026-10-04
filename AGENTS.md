# AGENTS.md — Codex 작업 규칙

## 프로젝트 목적
CapCut에서 완성한 SET MP4를 장시간 영상으로 반복 연결하는 저메모리 Windows 도구다.

## 절대 보존 규칙
- 기본 제작 경로는 FFmpeg `-c copy` stream copy만 사용한다.
- 영상/오디오 재인코딩을 자동으로 추가하지 않는다.
- 회차 모드에서는 마지막 SET을 자르지 않는다.
- 여러 SET에서 1회차는 선택된 SET 전체 순서 한 바퀴다.
- 대기열은 최대 5개이며 FFmpeg는 동시에 1개만 실행한다.
- 작업 실패/중지 시 임시파일을 정리한다.
- GUI thread를 장시간 작업으로 막지 않는다.
- 원본 MP4를 덮어쓰지 않는다.

## 수정 전
1. README_KO.md와 docs/MASTER_PLAN.md를 읽는다.
2. 관련 테스트를 먼저 확인한다.
3. 변경 범위를 최소화한다.

## 수정 후 필수 검증
```text
python -m pytest -q
python -m compileall -q .
git diff --check
```

## 중요 파일
- app/core.py: ffprobe, 회차/시간 계획, concat, 사후 검증
- app/ui.py: Tkinter UI, 최대 5개 순차 대기열
- app/tooling.py: FFmpeg 자동 탐색, Windows 절전 방지, FFmpeg 동시 실행 잠금
- app/live_*.py: v0.4 LIVE 송출 경로 (제작 경로와 분리, docs/LIVE_ARCHITECTURE.md). 로직은 live_controller.py, Tk 위젯은 live_ui.py
- app/cloud_*.py, cloud/, deploy/linux/: 무료 Cloud(OCI Always Free) LIVE (docs/FREE_CLOUD.md). 유료 Cloud 자원 생성/결제 API 호출 금지, shell=True 금지
- tests/: 회차/무손실 연결 회귀 테스트
