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
| `app/live_ui.py` | `LiveWindow` — 위젯만 담당 |
| `app/tooling.py` | `FFMPEG_GUARD` 공용 실행 잠금, `KeepAwake` |

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

## 단계

- Phase 1 (완료): backend — 명령, 보안, LiveProcess, watchdog, dry-run, guard
- Phase 2 (완료): LIVE 창, preflight, DPAPI 저장, graceful q stop, keep-awake, close lifecycle
- Phase 3 (예정): 다중 MP4 플레이리스트 무한 송출 + 곡/SET 자동 전환
