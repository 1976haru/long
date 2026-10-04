# LIVE ARCHITECTURE — v0.4 24H LIVE (Phase 1)

## 목표

- 기존 장시간 MP4 제작(v0.3) 기능을 100% 유지한 채 YouTube 24/7 LIVE 송출 기반을 추가한다.
- 단일 MP4를 무한 반복해 RTMP/RTMPS로 실시간 송출한다.
- Stream Key를 어떤 로그/예외/repr에도 노출하지 않는다.
- FFmpeg 비정상 종료 시 자동 재접속하는 watchdog을 둔다.

## 비목표 (Phase 1)

- LIVE 전용 GUI (Phase 2)
- 여러 SET 플레이리스트 송출, 중간 교체
- Stream Key 영구 저장 (DPAPI / Windows Credential Manager)
- YouTube API 연동, 방송 상태 조회
- 실제 YouTube 장시간 송출 검증

## 두 실행 경로

```text
Long Video (기존, 변경 없음)            LIVE (신규, 분리)
  app/core.py run_concat_copy()           app/live_profile.py LiveConfig
  concat demuxer + -c copy                app/live_core.py build_live_command()
  .part.mp4 → ffprobe 검증 → 교체          FFmpeg subprocess (LiveProcess)
  MP4 파일                                 RTMP/RTMPS ingest
                 └── app/tooling.py FFMPEG_GUARD ──┘
                     (FFmpeg 동시 1개: 둘 중 하나만 실행)
```

- 장시간 제작의 `-c copy` 원칙은 제작 경로에만 적용된다. LIVE는 ingest 안정성을 위해 H.264/AAC 실시간 인코딩을 사용한다.
- LIVE는 `run_concat_copy()`를 사용하거나 변경하지 않는다.

## 모듈 구조

| 모듈 | 역할 |
|---|---|
| `app/live_profile.py` | `LiveProfile`, `LiveConfig`(stream_key `repr=False`), `mask_secret`, `redact`, `StreamKeyStore` 저장 계층 |
| `app/live_core.py` | `build_output_url`, `build_live_command`, `prepare_live`(dry-run), `ProgressParser`/`LiveStats`, `LiveProcess` |
| `app/live_supervisor.py` | `LiveState`, `RETRY_DELAYS`, `LiveSupervisor` watchdog |
| `app/tooling.py` | `FfmpegExecutionGuard` / `FFMPEG_GUARD` 공용 실행 잠금 |

## FFmpeg LIVE 명령

```text
ffmpeg -hide_banner -nostdin -loglevel warning
  -re -stream_loop -1 -i INPUT.mp4          # 실시간 pacing + 무한 반복
  -map 0:v:0 -map 0:a:0?
  -c:v libx264 -preset veryfast -profile:v high -pix_fmt yuv420p
  -r 30 -b:v 8000k -maxrate 8000k -bufsize 16000k
  -g 60 -keyint_min 60 -sc_threshold 0      # keyframe = fps × 2초
  -c:a aac -b:a 192k -ar 44100 -ac 2
  -progress pipe:1 -nostats
  -f flv rtmps://.../live2/<STREAM_KEY>
```

- argument list로만 실행한다 (`shell=True` 금지).
- `-re`가 없으면 FFmpeg가 파일을 최대 속도로 밀어내므로 반드시 입력 앞에 둔다.
- Python은 프레임을 읽지 않고 `-progress` 텍스트(fps/bitrate/out_time/speed)만 파싱한다.

## 보안 원칙

- Stream Key를 소스, JSON, README, 테스트 실제값, 로그, 예외, 전체 명령 출력에 남기지 않는다.
- URL 결합은 `build_output_url()` 한 곳에서만 한다. `rtmp://`, `rtmps://`만 허용하고 trailing slash를 정리한다. 오류 메시지에는 key를 넣지 않는다.
- 로그/dry-run은 `redact()`로 key를 `********`로 완전히 가린다. UI 힌트용 `mask_secret()`은 충분히 긴 값만 앞/뒤 4자를 보여준다.
- FFmpeg stderr에 송출 URL이 찍혀도 `LiveProcess`가 저장 전에 key를 가린다.
- `settings.json`에 Stream Key를 저장하지 않는다. Phase 1은 `MemoryStreamKeyStore`(메모리) / `EnvStreamKeyStore`(`PLVM_STREAM_KEY`, 개발용)만 제공한다. Phase 2에서 같은 인터페이스로 DPAPI 저장소를 추가한다.

## Supervisor

```text
STOPPED → STARTING → RUNNING
RUNNING --(예기치 않은 종료)--> RECONNECT_WAIT --(delay 경과)--> STARTING
any --(사용자 STOP)--> STOPPING → STOPPED   (재접속 안 함)
STARTING --(FFmpeg 실행 불가/설정 오류)--> FAILED
```

- 재시도 지연: `5 → 10 → 30 → 60 → 60 …` 초 (bounded backoff, 무제한 재시도).
- 60초 이상 정상 송출 후 끊기면 backoff를 5초부터 다시 시작한다.
- `poll()` 기반이며 clock을 주입할 수 있어 테스트에서 sleep 없이 검증한다. `run_in_background()`가 watchdog 스레드를 띄워 GUI thread를 막지 않는다.
- LIVE 세션 동안(RECONNECT_WAIT 포함) `FFMPEG_GUARD`를 보유하므로 장시간 제작을 시작할 수 없다. 반대로 제작 대기열 실행 중에는 LIVE 시작이 `LiveBusyError`로 거부된다.
- 종료된 FFmpeg는 항상 `wait()`로 회수해 zombie를 남기지 않는다.

## Dry-run

```python
cmd, text = prepare_live(ffmpeg=Path("ffmpeg"), config=config)
print(text)
# LIVE command prepared
# input=CHILI_LAB_EP001.mp4
# target=rtmps://a.rtmps.youtube.com:443/live2/********
# stream_key=********
```

smoke 테스트는 `build_live_command(..., output_target=<로컬 파일>)`로 네트워크 없이 실제 FFmpeg 반복/정지/재시작을 검증한다.

## Phase 1 범위 (완료)

- LIVE 전용 모듈 분리, 단일 MP4 무한 반복 명령, Stream Key 보안, LiveProcess, 상태 파싱, watchdog, dry-run, 공용 실행 잠금, 테스트
- UI: 상단 `24H LIVE (개발 중)` 안내 버튼만 추가

## Phase 2 예정

- LIVE 전용 화면 (입력 선택, 프로필, Stream Key 입력/마스킹 표시, 시작/중지, 실시간 상태)
- DPAPI / Credential Manager Stream Key 저장소
- 여러 SET 플레이리스트 송출
- 송출 로그 파일 (마스킹 적용), 장시간 실송출 검증
- 장시간 송출 중 Windows 절전 방지 연동
