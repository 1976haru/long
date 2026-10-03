# CLAUDE.md — Claude Code 작업 규칙

이 저장소는 Playlist Long Video Maker입니다.

핵심 요구사항:
1. FFmpeg `-c copy`를 기본 제작 경로에서 유지하세요. 자동 재인코딩을 넣지 마세요.
2. 회차 기준은 완전한 SET 단위로 끝나야 합니다.
3. 자동 대기열은 최대 5개이며 작업은 반드시 순차 실행합니다.
4. 메모리 사용을 낮추기 위해 Python으로 영상 프레임을 디코딩하지 마세요.
5. 임시 출력은 `.part.mp4`로 만들고 검증 성공 후 최종 파일로 교체하세요.
6. 변경 후 `python -m pytest -q`, `python -m compileall -q .`, `git diff --check`를 실행하세요.
7. UI 변경 시 기존 쉬운 흐름(SET 선택 → 회차 선택 → 대기열 추가 → 자동 시작)을 복잡하게 만들지 마세요.

먼저 `README_KO.md`, `docs/MASTER_PLAN.md`, `AGENTS.md`를 읽고 작업하세요.
