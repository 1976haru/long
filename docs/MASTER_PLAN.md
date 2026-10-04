# MASTER PLAN — Playlist Long Video Maker v0.3

## 목적

CapCut은 1SET 완성에만 사용하고 장시간 반복/연결은 PLVM이 담당한다.

```text
CapCut
  → 완성 SET MP4 (자막/이미지/파형/음원/구독요소 포함)
  → PLVM 회차 선택
  → 최대 5개 대기열
  → FFmpeg stream copy 순차 실행
  → 최종 MP4 검증
```

## 절대 원칙

1. 영상·오디오 재인코딩 금지. 기본 제작은 FFmpeg `-c copy`.
2. 서로 호환되지 않는 다중 SET은 자동 변환하지 않고 차단.
3. 회차 모드는 완전한 SET 순환까지만 출력. 중간 컷 금지.
4. 여러 SET의 1회차는 목록 전체 1바퀴.
5. 대기열 최대 5개. 동시에 실행하는 FFmpeg는 항상 1개.
6. 실패/중지 시 `.part.mp4`와 임시폴더 정리.
7. 성공 후 ffprobe로 길이/스트림 속성 검증.
8. 원본 MP4는 읽기 전용으로 취급.
9. 대기열은 설정 파일에 저장해 재실행 시 복구.

## v0.3 범위

- 단일/다중 SET
- 회차 1~100회
- 시간 기준 최대 12시간
- 최대 5개 자동 대기열
- 실패 후 다음 작업 계속 옵션
- 출력명 자동 생성/충돌 회피
- 디스크 여유 공간 검사
- 진행률/중지
- stream-copy 사후 검증
- Windows 절전 방지
- WinGet FFmpeg 자동 탐색
- 대기열 자동 저장/복구

## 두 실행 경로 (v0.4~)

```text
Long Video (v0.3 그대로)
  SET MP4 → 회차/시간 계획 → 대기열(최대 5) → run_concat_copy (-c copy) → .part.mp4 → ffprobe 검증 → MP4

LIVE (v0.4 Phase 1, 분리 모듈)
  단일 MP4 → LiveConfig → build_live_command (-re -stream_loop -1, H.264/AAC) → LiveProcess → RTMP/RTMPS
                                                         └ LiveSupervisor watchdog (5/10/30/60초 재접속)
```

- 두 경로는 코드를 공유하지 않는다. 공유하는 것은 FFmpeg 탐색(`tooling.py`)과 동시 실행 잠금(`FFMPEG_GUARD`)뿐이다.
- `-c copy` 무재인코딩 원칙은 Long Video 경로의 원칙이며 LIVE 때문에 변경하지 않는다.
- LIVE는 ingest 안정성을 위해 별도 인코딩 프로필을 사용한다.
- FFmpeg 동시 1개 원칙은 두 경로 전체에 적용된다.
- Stream Key는 settings.json에 저장하지 않는다.
- 상세 설계: `docs/LIVE_ARCHITECTURE.md`

## 향후 후보

- 드래그앤드롭
- 폴더 자동 스캔
- 완료 후 PC 종료
- 출력 매니페스트/SHA256 기록
- Windows EXE 자동 릴리스
