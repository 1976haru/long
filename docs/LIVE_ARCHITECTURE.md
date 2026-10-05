# LIVE ARCHITECTURE — v0.4 24H LIVE (Phase 1 + Phase 2)

## 목표

- 기존 장시간 MP4 제작(v0.3) 기능을 100% 유지한 채 YouTube 24/7 LIVE 송출을 제공한다.
- 단일 MP4를 무한 반복해 RTMP/RTMPS로 실시간 송출한다.
- Stream Key를 어떤 로그/예외/repr/messagebox/설정 파일에도 노출하지 않는다.
- FFmpeg 비정상 종료 시 자동 재접속하고, 사용자 종료/앱 종료 시 FFmpeg를 반드시 정상 종료한다.

## 비목표 (Phase 3+)

- 여러 MP4 플레이리스트 송출, 곡/SET 자동 전환
- 여러 채널 동시 송출
- YouTube Data/조회수/채팅/예약 방송 API, OBS 연동
- 해상도 변환(scale): 입력 해상도 그대로 송출한다

## 두 실행 경로

```text
Long Video (기존, 변경 없음)            LIVE (분리)
  app/core.py run_concat_copy()           app/live_ui.py  LiveWindow (Toplevel)
  concat demuxer + -c copy                app/live_controller.py  preflight / LiveController
  .part.mp4 → ffprobe 검증 → 교체          app/live_supervisor.py  watchdog
  MP4 파일                                 app/live_core.py  LiveProcess → RTMP/RTMPS
                 └── app/tooling.py FFMPEG_GUARD ──┘
                     (FFmpeg 동시 1개: 둘 중 하나만 실행)
```

- 장시간 제작의 `-c copy` 원칙은 제작 경로에만 적용된다. LIVE는 ingest 안정성을 위해 H.264/AAC 실시간 인코딩을 사용한다.
- LIVE는 `run_concat_copy()`를 사용하거나 변경하지 않는다.

## 모듈 구조

| 모듈 | 역할 |
|---|---|
| `app/live_profile.py` | `LiveConfig`(stream_key `repr=False`), `LIVE_PRESETS`, `recommend_preset`, `mask_secret`, `redact`, `StreamKeyStore` |
| `app/live_secrets.py` | `WindowsDpapiStreamKeyStore` (ctypes DPAPI), non-Windows fallback `SessionStreamKeyStore` |
| `app/live_core.py` | `build_output_url`, `build_live_command`, dry-run, `ProgressParser`/`LiveStats`, `LiveProcess`(graceful stop, kill-on-exit job) |
| `app/live_supervisor.py` | `LiveState`, `RETRY_DELAYS`, `LiveSupervisor` watchdog |
| `app/live_controller.py` | Tk 비의존 로직: `run_preflight`, `LiveController`(이벤트 큐, keep-awake, snapshot) |
| `app/live_ui.py` | `LiveWindow` — 위젯만 담당 (LIVE READY, 실행 위치, 송출 방식, Cloud 상태) |
| `app/live_ready.py` | `analyze_live_ready` (ffprobe만, 초반 60초 packet 헤더), `make_live_ready_file` (PC 1회 변환) |
| `app/cloud_model.py` | OCI Always Free 프로필/검증, ssh.exe 탐색/인자, 안전한 서버 파일명, SHA256 |
| `app/cloud_client.py` | SSH 원격 작업(준비 6단계, 업로드, 시작/종료, 상태, 로그), `CloudLiveController` |
| `app/cloud_setup_ui.py` | 초보자용 4 STEP 처음 설정 도우미 |
| `cloud/long_live_worker.py` | Linux 서버 worker (표준 라이브러리만, DIRECT COPY, watchdog, status.json) |
| `deploy/linux/` | `long-live.service`, `install.sh`, `uninstall.sh` |
| `app/tooling.py` | `FFMPEG_GUARD` 공용 실행 잠금, `KeepAwake` |

DIRECT COPY: `build_live_copy_command()` — `-re -stream_loop -1 -i FILE -map 0:v:0 -map 0:a:0 -c:v copy -c:a copy -progress pipe:1 -f flv TARGET` (인코더/필터/-r/-g/-b:v 없음). 송출 방식 기본값은 **자동/저부하**: LIVE READY → DIRECT COPY, 아니면 `LIVE READY 파일 만들기` 권장. libx264 TRANSCODE는 내 PC에서 사용자가 명시적으로 고를 때만. 무료 Cloud 상세: `docs/FREE_CLOUD.md`.

`app/ui.py`에는 `● 24H LIVE` 버튼, LIVE 창 열기, 종료 시 LIVE 정리 연결만 있다.

## FFmpeg LIVE 명령

```text
ffmpeg -hide_banner -loglevel warning
  -re -stream_loop -1 -i INPUT.mp4          # 실시간 pacing + 무한 반복
  -map 0:v:0 -map 0:a:0                      # 오디오 필수
  -c:v libx264 -preset veryfast -profile:v high -pix_fmt yuv420p
  -r 30 -b:v 8000k -minrate 8000k -maxrate 8000k -bufsize 16000k -x264-params nal-hrd=cbr   # CBR
  -g 60 -keyint_min 60 -sc_threshold 0      # keyframe = fps × 2초
  -c:a aac -b:a 128k -ar 44100 -ac 2
  -progress pipe:1 -nostats
  -f flv rtmps://.../live2/<STREAM_KEY>
```

- argument list로만 실행한다 (`shell=True` 금지).
- `-nostdin`을 쓰지 않는다: stdin pipe로 `q`를 보내 정상 종료한다.
- Python은 프레임을 읽지 않고 `-progress` 텍스트(fps/bitrate/out_time/speed)만 파싱한다.

## 송출 프로필 (입력 해상도 그대로)

| preset | 대상 입력 | video | audio |
|---|---|---|---|
| 720p 입력용 저부하 | ≤720p | 5000 kbps CBR | AAC 128k 44.1kHz |
| 1080p 입력용 안정형 (기본) | 1080p | 8000 kbps CBR | AAC 128k 44.1kHz |
| 1080p 입력용 고화질 | 1080p | 10000 kbps CBR | AAC 128k 44.1kHz |

해상도는 바꾸지 않으므로 UI는 "1080p 입력용"처럼 표기하고, 영상 선택 시 실제 높이에 맞는 preset을 자동 추천한다.

## 정상 종료 (graceful stop)

```text
LIVE 종료 / 창 닫기 / 앱 종료
 → supervisor user_stop (재접속 금지)
 → stdin "q" → wait 5s
 → terminate → wait 3s
 → kill → wait 3s
 → reader thread join, pipe close, guard 해제, keep-awake 해제
```

- `q` 종료 시 FFmpeg가 출력을 정상 마무리하므로 rc=0, 로컬 FLV도 ffprobe로 읽힌다 (Phase 1의 강제 종료/잘린 파일 문제 해결).
- GUI에서는 백그라운드 스레드로 stop하고 `after()`로 완료를 기다려 창이 멈추지 않는다.
- Windows Job Object(KILL_ON_JOB_CLOSE)에 FFmpeg를 넣어, 앱이 비정상 종료돼도 FFmpeg가 남지 않는다. `atexit`도 마지막 안전장치로 stop을 호출한다.

## 보안 원칙

- Stream Key를 소스, JSON, README, 테스트 실제값, 로그, 예외, 전체 명령 출력, messagebox, 창 제목에 남기지 않는다.
- URL 결합은 `build_output_url()` 한 곳에서만 한다. `rtmp://`, `rtmps://`만 허용, trailing slash 정리, 오류 메시지에 key 없음.
- 로그/dry-run/preflight/상태/오류는 `redact()`로 `********` 처리. supervisor/controller/LiveProcess 각 계층에서 이중으로 가린다.
- 입력창은 `show="●"`. [보기]는 토글이며 8초 후 자동으로 다시 숨긴다. 클립보드 자동 복사 없음.
- **저장**: "이 PC에 안전하게 기억" ON → `%APPDATA%\PlaylistLongVideoMaker\live_secret.dat`에 DPAPI blob만 저장 (현재 Windows 사용자만 복호화). OFF → 저장 파일 삭제, 메모리에서만 사용. `settings.json`에는 절대 저장하지 않는다. 다른 PC/계정에서 복사한 파일은 복호화 실패 → 무시.
- 비 Windows 환경은 저장 기능 비활성 (메모리 전용).

## Preflight (LIVE 시작 / 송출 설정 검사)

FFmpeg/ffprobe 존재 → 입력 파일 존재 → ffprobe 성공 · 영상 stream · 0초 아님 → **오디오 stream 필수** → 송출 주소(rtmp/rtmps) → Stream Key 입력·형식 → `FFMPEG_GUARD` 비어 있음 → supervisor STOPPED/FAILED → 명령 생성 + redaction 자체 검사. 하나라도 실패하면 시작하지 않고 한글 메시지로 원인을 보여준다. 실제 YouTube 연결은 하지 않는다.

## Supervisor

```text
STOPPED → STARTING → RUNNING
RUNNING --(예기치 않은 종료)--> RECONNECT_WAIT --(delay 경과)--> STARTING      (자동 재접속 ON)
RUNNING --(예기치 않은 종료)--> FAILED                                         (자동 재접속 OFF)
any --(사용자 STOP)--> STOPPING → STOPPED   (재접속 안 함)
STARTING --(FFmpeg 실행 불가/설정 오류)--> FAILED
```

- 재시도 지연: `5 → 10 → 30 → 60 → 60 …` 초. 60초 이상 정상 송출 후 끊기면 5초부터 다시.
- UI 표시: 대기 / 연결 중 / ● LIVE / 재연결 대기 / 종료 중 / 오류.
- LIVE 세션 동안(RECONNECT_WAIT 포함) `FFMPEG_GUARD`를 보유: 장시간 제작 시작 시 "현재 LIVE 송출 중입니다.", 제작 중 LIVE 시작 시 "현재 장시간 영상 제작 중입니다."

## UI thread safety

backend 스레드(watchdog, stop)는 `controller.events` 큐에만 쓴다. `LiveWindow`가 500ms `after()` 주기로 `drain_events()`/`snapshot()`을 읽어 위젯을 갱신한다. Windows 절전 방지(`KeepAwake`)는 Tk main thread에서 enable/disable한다 (SetThreadExecutionState는 스레드 단위).

## 실제 YouTube 송출

자동 테스트는 실제 YouTube에 연결하지 않는다 (로컬 FLV 출력으로 검증). 실제 송출 확인은 사용자가 GUI에 자신의 key를 직접 입력해서, YouTube Live Control Room의 비공개/일부공개 스트림으로 먼저 한다.

## Phase 3A — 여러 MP4 Playlist / 보관 안전 세션 / soak

### Playlist (DIRECT COPY 전용, 순차 무한 반복)

- 1~20개, 순서 변경/제거/전체 지우기, 같은 파일 중복 금지, shuffle 없음 (`app/live_playlist.py`).
- 모든 파일이 LIVE READY이고 서로 같아야 한다: 해상도, FPS, 영상 코덱, AAC 샘플레이트/채널. VFR·영상/소리 길이 차이도
  Playlist에서는 차단(경계마다 반복되므로). 다르면 자동 재인코딩하지 않고 "N번 영상의 … [LIVE READY 파일 만들기]" 안내.
- 송출: `-re -stream_loop -1 -f concat -safe 0 -i PLAYLIST.ffconcat -map 0:v:0 -map 0:a:0 -c:v copy -c:a copy`.
  A→B→C→A… 전체가 반복된다. 1개짜리 Playlist는 기존 단일 파일 명령 그대로.
- **실측 근거 (FFmpeg 7.1, 1080p30 H.264 + AAC 44.1k, 10초 × 3개)**
  - concat 항목 길이를 파일 길이 그대로 두면 A→B, B→C 경계마다 AAC 프레임이 8ms 겹쳐
    `Non-monotonic DTS` 경고가 났다 (FFmpeg가 보정하지만 경고 0이 목표).
  - 각 항목 `duration` = 파일 길이 + AAC 1프레임(1024/샘플레이트, 44.1k에서 23.2ms)으로 두면 경고 0,
    DTS 역행/중복 0, 영상 경계 간격 최대 57ms, 오디오 간격 최대 24ms.
  - 60회 반복(1800초, 경계 178개)에서 A/V 끝 차이가 ±29ms 안에서 오르내릴 뿐 누적되지 않았다.
- manifest: 내 PC는 설정 폴더의 `live_playlist.ffconcat`, 서버는 worker가 `/opt/long-live/state/playlist.ffconcat`를
  `.part → 이름 교체`로 만든다. 서버는 SAFE_MEDIA 규칙을 통과한 이름 + media 폴더 경로만 쓴다
  (`../`, 절대 경로, 따옴표/줄바꿈/셸 문자 금지). 항목 길이는 서버의 ffprobe 메타데이터로 계산.
- 상태: 송출 위치(out_time)와 항목 길이로 현재 영상 `3/8 파일명`, Playlist 회차를 계산 (재접속하면 그 연결 기준으로 다시 셈).

### 세션 관리 (`app/live_session.py`)

- **계속 방송** (기본): 기존과 같음.
- **보관 안전 모드**: 세션 시작부터 `ARCHIVE_SAFE_SECONDS = 42600` (11시간 50분)이 되면
  FFmpeg에 `q` → 정상 종료 → **재접속하지 않음** → `SESSION_LIMIT_REACHED` (화면: "보관 안전 종료 · 다음 세션 대기").
  - YouTube는 12시간을 넘는 LIVE를 보관하지 못할 수 있다. 11:50은 YouTube 공식 숫자가 아니라 이 프로그램이 정한 안전 여유값이다.
  - 같은 YouTube Broadcast에 다시 연결해 12시간을 넘기는 일이 없도록, 한도 검사는 종료/재접속 판단보다 먼저 한다.
  - 내 PC: `LiveSupervisor(session_limit=…)`. 한도에서 guard/절전 방지를 풀어 다른 작업이 가능해진다.
  - Cloud: worker가 `state/session.json`에 `session_id`와 시작 시각(서버 wall clock)을 저장한다.
    worker crash/재부팅 후에도 같은 세션의 시작 시각을 이어 써서 11:50이 리셋되지 않는다.
    한도에서 exit 0 (`Restart=on-failure`는 재시작하지 않음), 완료된 세션은 다시 시작돼도 송출하지 않는다.
    PC의 **[다음 세션 시작]**이 새 `session_id`로 설정을 쓰면 새 세션이 시작된다.
- 화면: `세션 시간 08:31:22 / 11:50:00`, `세션 종료까지 03:18:38`, 10분/5분/1분 전 상태 문구 (팝업 없음).
- **Phase 3B hook**: `SessionRolloverProvider` (`prepare_next_broadcast`, `complete_current_broadcast`,
  `get_next_ingest`). Phase 3A 기본은 `ManualSessionProvider` — "다음 YouTube LIVE를 준비한 뒤 [다음 세션 시작]".
  **새 YouTube Broadcast 자동 생성/종료는 Phase 3B(YouTube API)** 이며 이번 단계에는 없다.
  FFmpeg만 재시작해서는 YouTube가 새 보관 영상을 만든다는 보장이 없기 때문이다.

### Cloud 설정 schema v2 / worker v2

```json
{"schema_version": 2, "media": ["01_LIVE_READY.mp4", "02_LIVE_READY.mp4"], "play_mode": "sequential",
 "ingest_url": "rtmps://...", "mode": "copy", "session_mode": "continuous", "session_id": "..."}
```

- worker는 v1(`"media": "file.mp4"`)도 그대로 읽는다 (문자열 → `[문자열]`).
- PC는 **단일 영상 + 계속 방송**이면 `media`를 문자열로 써서 기존 v1 worker와도 호환된다.
  Playlist/보관 안전 모드는 worker v2가 필요하며, 구버전이면 시작하지 않고(서버 설정도 바꾸지 않고)
  "방송이 끝난 뒤 [무료 Cloud 자동 준비]를 다시 실행" 안내만 한다. 자동 업데이트하지 않는다.
- Playlist 업로드: 파일별 SHA256 → 서버에 같은 파일이 있으면 생략 → 없는 파일 합계 + 여유(256MB + 5%)로 저장 공간을 먼저 확인
  → 하나씩 `.part` 업로드(1MB 단위, 진행률 `2/8 보내는 중 42%`) → 서버 SHA256 검증 → 이름 교체. 기존 서버 파일은 지우지 않는다.

### 자원 (OCI E2.1.Micro: 1 OCPU, 1GB)

- FFmpeg 1개, Python은 frame/미리보기/파형 처리 없음, 기록은 bounded(오류 30, 상태·재접속 100), status 5초, 로그 회전, MemoryMax=512M.
- 영상 파일 수가 늘어도 Python 메모리는 파일 크기에 비례하지 않는다 (SHA256/업로드 모두 1MB 스트리밍).
- `monthly_transfer_bytes()`: 예) 6.3 Mbps × 24시간 × 30일 ≈ 2.04 TB/월/1채널 (외부 API 없이 계산만, 향후 다채널 경고용).

### 24시간 soak (`tools/live_soak.py`)

- 일반 pytest는 24시간을 기다리지 않는다: fake clock으로 24시간 상태 전이를 빠르게 검증 (`tests/test_live_session.py`).
- 실제 측정은 사용자가 별도 실행: `python tools/live_soak.py --hours 24 --mode playlist-copy`
  (`single-copy` / `playlist-copy` / `worker-playlist`). YouTube/OCI에 연결하지 않고 로컬 sink로 DIRECT COPY.
- interval마다 CSV, 끝에 JSON 요약: Python/FFmpeg RSS, thread, handle/fd, 재접속, 송출 위치, Playlist 회차, 마지막 오류, 출력/로그 크기.
  시작 직후(warm-up)를 빼고 앞 10% 대비 마지막 10% 중앙값이 20% 그리고 20MB(handle은 50개) 넘게 늘면 SUSPECT.
- 단계: 짧은 smoke → 2h → 6h → 12h → 24h. **24h soak이 끝나기 전에는 "24시간 안정성 검증 완료"라고 쓰지 않는다.**

## 단계

- Phase 1 (완료): backend — 명령, 보안, LiveProcess, watchdog, dry-run, guard
- Phase 2 (완료): LIVE 창, preflight, DPAPI 저장, graceful q stop, keep-awake, close lifecycle
- Phase 3A (완료): 다중 MP4 Playlist DIRECT COPY, 보관 안전 세션(11:50 안전 종료), Cloud 설정 v2, soak 도구
- Phase 3B (코드 완료, 실제 YouTube 미검증): YouTube API 자동 Broadcast 교체 — 재사용 stream 1개, 11:40 다음 방송 준비, 11:50 complete→live, 실패 시 현재 방송 유지. 자세한 내용: `docs/YOUTUBE_ROLLOVER.md`
- 다음: 실제 YouTube 비공개 3분+3분 교체 smoke → Cloud credential Gate(서버 단독 교체) → 실제 OCI 배포 → 6/12/24h soak → 다채널
