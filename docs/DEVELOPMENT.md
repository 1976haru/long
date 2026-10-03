# 로컬 개발 — GitHub / Codex / Claude Code

저장소: `https://github.com/1976haru/long`

## 권장 로컬 위치

`D:\03 long`

기존 폴더가 단순 압축 해제본이면 먼저 백업한 뒤 Git 저장소로 새로 clone하는 방법이 가장 안전합니다.

```bat
cd /d D:\
ren "03 long" "03 long_backup"
git clone https://github.com/1976haru/long.git "03 long"
cd /d "D:\03 long"
```

정상 확인:

```bat
git status
git remote -v
py -3 -m pytest -q
```

## 작업 브랜치

main을 직접 실험용으로 쓰지 않습니다.

```bat
git checkout -b v0.4-dev
```

Codex 또는 Claude Code가 수정한 뒤:

```bat
py -3 -m pytest -q
git diff --check
git status
```

검증 후 commit/push:

```bat
git add .
git commit -m "Upgrade playlist queue workflow"
git push -u origin v0.4-dev
```

## Codex

저장소 루트에서 Codex를 실행합니다. Codex는 `AGENTS.md`를 먼저 따라야 합니다.

## Claude Code

저장소 루트에서 Claude Code를 실행합니다. Claude Code는 `CLAUDE.md`를 먼저 따라야 합니다.

## 금지

- `-c:v libx264`, `-c:a aac` 같은 재인코딩을 기본 제작 경로에 추가하지 말 것
- 대기열을 병렬 FFmpeg로 바꾸지 말 것
- 10시간 전체 영상을 메모리로 읽지 말 것
- 테스트 없이 main에 직접 push하지 말 것
