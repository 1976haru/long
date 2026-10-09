# TRUE 예약 LIVE (Cloud 자동 송출) v1

오늘 예약을 준비하고 PC를 꺼도, 예약 시각에 무료 Cloud가 Playlist를 송출하고 YouTube가 자동으로 LIVE/종료한다.

## 구조

| 위치 | 하는 일 |
|---|---|
| PC `app/scheduled_live.py` | 준비 Pipeline (영상 검사 → Cloud 전송 → YouTube 예약 → Cloud job 등록 → read-back) |
| PC `app/youtube_live_schedule_ui.py` | 예약 LIVE 창 (초보자 빠른 예약, 진행 단계, 완료 카드, 취소/다시 시도/상태 확인) |
| PC `app/live_ready_batch.py` | Playlist 일괄 LIVE READY 변환 (`make_live_ready_file` 재사용, 원본 보관) |
| Cloud `cloud/long_live_worker.py --scheduler` | 예약 job 상태 기계 (`long-live-scheduler.service`, worker v3) |

- YouTube 예약 방송은 `enableAutoStart=true`, `enableAutoStop=true` (Cloud 예약 전용).
  기존 PC 직접 제어 LIVE / YouTube 예약만 / 자동 교체는 기본값 `false` 그대로.
- Cloud에는 Google OAuth token을 두지 않는다. 송출 시작 → YouTube 자동 LIVE, 송출 종료(FFmpeg `q`) → YouTube 자동 종료.
- Stream Key는 기존 보안 경로만: ssh stdin → `/etc/long-live/stream.key` (0600). job에는 SHA256 앞 16자리 지문만.
- Cloud job에는 로컬 경로(`D:\...`)가 없다: 서버 파일 이름 + SHA256 + 크기만. 영상 업로드는 기존 `upload_many` (SHA256 skip).

## 준비 순서 (PC)

1 영상 검사 · 2 LIVE READY · 3 Playlist 호환 · 4 Cloud 연결 + worker v3 확인 · 5 저장 공간 · 6 전송 · 7 SHA256 검증
· 8 manifest · 9 YouTube 재사용 stream 확인 → Cloud Key 맞추기 + scheduler 켜기 · 10 YouTube 예약(회차마다) + bind
· 11 Cloud job 저장 · 12 Cloud에서 다시 읽어 확인 (job_id / 시각 / 영상 수 / manifest / PENDING) + scheduler active

- Cloud 준비(1~9)가 실패하면 YouTube 예약을 만들지 않는다 (유령 예약 최소화).
- YouTube 예약 뒤 Cloud job이 실패하면 YouTube 예약은 지우지 않고 `PARTIAL` → [Cloud 준비 다시 시도].
- "PC를 꺼도 됩니다"는 모든 회차가 read-back으로 확인되고 scheduler가 active일 때만 표시한다.
- 반복 예약(7일 rolling / 최대 7개)은 회차마다 Cloud job 1개. 같은 Playlist는 다시 업로드하지 않는다.

## Cloud scheduler (worker v3)

- job 명세: `/etc/long-live/jobs/<job_id>.json` (PC가 `sudo python3 worker --add-job`, stdin JSON, 서버에서 검증)
- job 상태: `/opt/long-live/state/jobs/<job_id>.state.json`, 송출 작업 폴더 `/opt/long-live/state/jobs/<job_id>/`
- 상태: `PENDING → PREPARING → STARTING → LIVE → COMPLETE` / `CANCELLED` / `MISSED` / `FAILED` (+ `STOPPING`)
- T-120초 영상 크기·SHA256 확인, T-30초 최종 확인(파일, Stream Key 지문), **T(예약 시각)에 FFmpeg 시작**.
  미리 송출하면 enableAutoStart 때문에 방송이 일찍 LIVE가 될 수 있어 송출은 예약 시각에만 시작한다.
- 대기 중 sleep은 다음 이벤트까지 최대 30초 (busy polling 없음), 송출 중 상태 기록 5초.
- stop_at에 기존 Worker가 FFmpeg에 `q` → 정상 종료 → `COMPLETE`. Playlist는 기존 ffconcat + `-stream_loop -1` DIRECT COPY.
- 늦은 시작: 예약 시각 + 5분 이내면 시작(`late_seconds` 기록), 넘으면 `MISSED`.
- 재부팅/서비스 재시작: 상태 파일 유지. `LIVE`였던 job은 마지막 기록 후 5분 안이면 이어서 송출, 아니면 `FAILED`.
- 중복 방지: `state/scheduler.lock` (scheduler 1개, 두 번째는 exit 3), `state/worker.lock` (기본 채널의 수동 Cloud LIVE와
  예약 송출 중 하나만, 채널 profile은 `state/channels/<id>/worker.lock`), `state/slots/live1~2.lock` (서버 전체 동시 2개).
- 취소: PC가 `--cancel-job` → `<job_id>.cancel` 파일. 시작 전이면 `CANCELLED`, 송출 중이면 FFmpeg 정상 종료.

## 여러 채널 (worker v4)

- job의 `profile_id`(없으면 기본 채널)에 따라 채널 key(`/etc/long-live/channels/<id>/stream.key`)와
  작업 폴더(`state/channels/<id>/jobs/<job_id>/`)를 쓴다. 기본 채널은 위 경로 그대로.
- 같은 채널 겹침 예약은 저장 단계에서 거부, 다른 채널은 같은 시각 허용 (전체 동시 2개까지).
- scheduler는 채널마다 job을 동시에 실행하며 한 job의 실패/취소가 다른 job을 멈추지 않는다.
  상세: `docs/MULTI_CHANNEL_CLOUD_LIVE.md`.

## 알려진 주의점 (REAL Gate에서 확인)

- 같은 재사용 stream에 autoStart 예약이 여러 개 bind된 상태(반복 예약)에서 YouTube가 어느 방송을 시작하는지 실제 확인 필요.
- 예약 LIVE가 기다리는 동안 같은 Stream Key로 직접 송출하면 예약 방송이 일찍 시작될 수 있다 → LIVE 창에서 경고.
- AutoStop이 기대대로 되지 않으면 그때만 별도 종료 구조를 검토한다 (Cloud에 refresh token 저장은 하지 않음).
