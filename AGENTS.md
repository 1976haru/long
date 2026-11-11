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
- app/youtube_*.py: YouTube API 자동 Broadcast 교체 (표준 라이브러리 REST, OAuth loopback+PKCE, token은 DPAPI만, docs/YOUTUBE_ROLLOVER.md)
- app/cloud_*.py, cloud/, deploy/linux/: 무료 Cloud(OCI Always Free) LIVE (docs/FREE_CLOUD.md). 유료 Cloud 자원 생성/결제 API 호출 금지, shell=True 금지
- app/bg_core.py, app/bg_analysis.py, app/bg_ui.py: ④ 내 영상으로 배경 만들기. 디졸브(xfade)를 위해 **이 경로에서만** 사용자가 [▶ 제작 시작]을 누를 때 한 바퀴 분량을 재인코딩하고, 목표 길이 반복은 core.run_concat_copy(-c copy)를 그대로 호출한다. 분석은 FFmpeg 48×27 샘플만 사용. 기본 출력은 H.264 yuv420p(LIVE 호환), H.265는 사용자가 고를 때만. 화질 비교 도구: tools/bg_codec_bench.py
- app/stock_net.py, app/stock_sources.py, app/stock_rights.py, app/stock_keys.py, app/stock_download.py, app/stock_player.py, app/stock_ui.py: ⑤ 배경 영상 검색·다운로드 (Pexels·Pixabay·Wikimedia Commons·Internet Archive 공식 API만, 표준 라이브러리 urllib). 허용 호스트+redirect 재검사, 제공처별 간격·429·24시간 캐시, API 키는 DPAPI 파일만, 길이 모드(10~120초 / 확인된 180초 이상) 엄격 분리·자동 대체 금지, 권리 상태는 참고용(보증 문구 금지), 선택한 영상만 다운로드, 미리보기는 ffplay 프로세스 제어(Python 프레임 디코딩 없음)
- app/pl_core.py, app/pl_ui.py: ⑥ 음악 플레이리스트 (배경 MP4 + 음원 15곡 → MP4 + 곡 시작 12초 제목 SRT). 음원은 디코드로 실제 길이 확인, 오디오는 AAC 하나로 연결(공백 없음·크로스페이드는 선택 시만). 영상은 규격이 맞고 제목 입히기가 없으면 -c copy 반복만, 해상도·코덱 변경·제목 입히기 때만 인코딩. ④ 결과는 `*.plvm_bg.json`(한 바퀴 길이)으로 경계 키프레임 DTS에서만 반복. ①②③에는 보내기만 (자동 시작 금지)
- app/media_audio.py: 배경 원본 오디오 검사(ffprobe 스트림 기준 — 조용하다고 '없음'으로 보지 않음, 실패는 '확인 불가')와 무음 사본(-map 0:v:0 -c:v copy -an, 실제 컨테이너 기준, 안 맞으면 재인코딩 대신 이유 표시, .part → 검증 → 교체, SHA256·권리 기록 연결). ④는 영상만 매핑하고 결과에 오디오가 있으면 실패, ⑥은 결과 오디오가 음원 1개인지 검증
- tests/: 회차/무손실 연결 회귀 테스트
