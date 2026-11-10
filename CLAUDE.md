# CLAUDE.md — Claude Code 작업 규칙

이 저장소는 Playlist Long Video Maker입니다.

핵심 요구사항:
1. FFmpeg `-c copy`를 기본 제작 경로에서 유지하세요. 자동 재인코딩을 넣지 마세요.
2. 회차 기준은 완전한 SET 단위로 끝나야 합니다.
3. 여러 SET에서 1회차는 SET 목록 전체 1바퀴입니다.
4. 자동 대기열은 최대 5개이며 작업은 반드시 순차 실행합니다.
5. 메모리 사용을 낮추기 위해 Python으로 영상 프레임을 디코딩하지 마세요.
6. 임시 출력은 `.part.mp4`로 만들고 검증 성공 후 최종 파일로 교체하세요.
7. 변경 후 `python -m pytest -q`, `python -m compileall -q .`, `git diff --check`를 실행하세요.
8. UI의 쉬운 흐름(SET 선택 → 회차 선택 → 대기열 추가 → 자동 시작)을 복잡하게 만들지 마세요.
9. 실사용 프로그램은 하나만 유지합니다. 별도 시험판 EXE·배포 폴더·바로가기·실행 아이콘을 만들지 마세요.
   - 실제 실행 경로: `RUN_WINDOWS.bat`(이 폴더 소스를 `py -3 main.py`로 실행)과 `dist\PlaylistLongVideoMaker_v0.3.exe`.
     설정·토큰·대기열은 `%APPDATA%\PlaylistLongVideoMaker` (settings.json, live_secret*.dat, youtube_token*.dat).
   - 기능 추가 시 순서: 코드 개발 → 자동 테스트 → 기존 프로그램 백업 → 기존 EXE 갱신 → 실행 검증 → 성공 확정 또는 원상복구.
   - 백업은 `dist\_recovery\before_update_<시각>\`(EXE + 설정 폴더 사본 + SHA256 manifest)에 두고, 복원 시험 후 교체합니다.
   - LIVE·예약 업로드·영상 제작이 진행 중이거나 프로그램이 실행 중이면 교체하지 말고 보고합니다. 실행 중인 EXE를 강제 종료하지 않습니다.
   - 새 EXE는 저장소 밖(임시 작업 폴더)에서 같은 파일명으로 빌드 → `--self-test`(격리 설정) 통과 → 같은 이름으로 교체 → 교체본 `--self-test`.
     실패하면 백업 EXE로 즉시 되돌립니다. 설정 파일은 초기화하지 않고, 새 설정 키는 기존 설정과 호환되게 추가합니다.

먼저 `README_KO.md`, `docs/MASTER_PLAN.md`, `AGENTS.md`를 읽고 작업하세요.
