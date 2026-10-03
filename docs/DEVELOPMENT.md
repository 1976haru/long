# 로컬 개발 — GitHub / Codex / Claude Code

저장소: `https://github.com/1976haru/long`

## 권장 로컬 위치

현재 사용 위치: `D:\03 long`

### 1) 현재 폴더가 Git clone인지 확인

```bat
cd /d "D:\03 long"
git status
git remote -v
```

`not a git repository`가 나오면 압축파일을 풀어둔 폴더입니다. 기존 폴더를 보존한 뒤 새로 clone하는 것이 가장 안전합니다.

```bat
cd /d D:\
ren "03 long" "03 long_backup"
git clone https://github.com/1976haru/long.git "03 long"
cd /d "D:\03 long"
```

### 2) 안정본과 개발 브랜치

- `main`: 검증된 안정본
- `develop`: Codex/Claude Code 수정용

최신본 받기:

```bat
git switch main
git pull --ff-only
git switch develop
git pull --ff-only
```

### 3) 검증

```bat
py -3 -m pip install pytest
py -3 -m pytest -q
py -3 -m compileall -q .
git diff --check
```

### 4) Codex

`D:\03 long` 루트에서 Codex를 실행합니다. `AGENTS.md` 규칙을 먼저 따르게 합니다.

### 5) Claude Code

같은 저장소 루트에서 Claude Code를 실행합니다. `CLAUDE.md`를 먼저 읽게 합니다.

### 6) 변경 push

```bat
git add .
git commit -m "Upgrade playlist long video maker"
git push origin develop
```

검증이 끝난 뒤 `develop → main`으로 병합합니다.

## 금지

- 기본 제작 경로에 `-c:v libx264`, `-c:a aac` 같은 재인코딩 추가 금지
- 대기열 병렬 FFmpeg 실행 금지
- 10시간 전체 영상을 Python 메모리로 읽기 금지
- 테스트 없이 main에 실험 코드 직접 push 금지
