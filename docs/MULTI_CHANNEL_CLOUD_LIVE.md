# 여러 채널 동시 Cloud LIVE v1 (worker v4)

한 Playlist Studio에서 시니어 채널 + 일본 채널처럼 **최대 2개 YouTube 채널**을 무료 Cloud에서 동시에 송출한다.

```text
채널 A (시니어)  Playlist A + Stream Key A → long-live@senior.service → FFmpeg A → YouTube A
채널 B (일본)    Playlist B + Stream Key B → long-live@chili.service  → FFmpeg B → YouTube B
기본 채널        기존 1채널 그대로        → long-live.service        → (기존 경로)
```

## 1. 절대 보존 (기존 1채널)

- **기본 채널(`default`) = 기존 경로/서비스 그대로**: `/etc/long-live/live.json`, `/etc/long-live/stream.key`,
  `/opt/long-live/state/{status,session}.json`, `state/worker.lock`, `long-live.service`, PC `live_secret.dat`,
  `settings["youtube"]`, `youtube_token.dat`. 기존 호출(`start_live()`, `stop_live()`, `status()`)은 같은 명령을 보낸다.
- v3 예약 job(`profile_id` 없음) = 기본 채널. 기존 `state/jobs/<job_id>/` 작업 폴더도 그대로.
- PC의 FFmpeg 동시 1개(`FFMPEG_GUARD`), Long Video `-c copy` 경로, 대기열 5개는 바뀌지 않는다.
  동시 2개는 **Cloud 서버의 DIRECT COPY 송출에만** 해당한다.

## 2. 채널 Profile (PC, `app/live_channels.py`)

`settings.json "live_channels"` (비밀 없음):
`channel_profile_id, display_name, youtube_channel_id, stream_mode, media_playlist, cloud_profile,
oauth_profile_id, stream_key_store_id, schedule_rules, enabled`

- 채널 ID: `^[a-z][a-z0-9_]{0,31}$` (서버 경로/systemd 이름에 그대로 쓰므로 엄격). `default`는 예약어.
- **Stream Key**: 채널마다 `live_secret_<id>.dat` (Windows DPAPI). 기본 채널은 기존 `live_secret.dat`.
  두 채널 key를 같은 파일에 저장하지 않는다.
- **YouTube(OAuth)**: 채널 → `youtube_accounts` 프로필(`youtube_token_<프로필ID>.dat`) 연결.
  같은 Google Desktop OAuth Client JSON을 여러 채널에 써도 refresh token은 프로필마다 따로 (공유/덮어쓰기 없음).
  채널별 `client_file / channel_id / channel_title / stream_id`.
- **Migration**: 처음 채널을 추가할 때 1번 `settings.json` 원본을 `settings.json.bak-before-multichannel`로 백업한 뒤
  기본 채널만 기록 (기존 키는 그대로). 기존 `youtube_token.dat`가 있으면 `migrate_legacy_youtube_token()`이
  기본 채널용 프로필로 **복사**한다 (원본 token/설정은 지우지 않음).
- OAuth 없이도 채널마다 Stream Key 직접 입력으로 2채널 수동 LIVE가 된다.

## 3. 서버 경로 분리 (worker v4)

| | 기본 채널 | 채널 `<id>` |
|---|---|---|
| Stream Key | `/etc/long-live/stream.key` | `/etc/long-live/channels/<id>/stream.key` (0600 longlive:longlive) |
| 설정 | `/etc/long-live/live.json` | `/etc/long-live/channels/<id>/live.json` (0640 root:longlive) |
| 상태 | `/opt/long-live/state/` | `/opt/long-live/state/channels/<id>/` (status, session, playlist.ffconcat, worker.lock, jobs/) |
| 로그 | `/opt/long-live/logs/worker.log` | `/opt/long-live/logs/channels/<id>/worker.log` |
| 서비스 | `long-live.service` | `long-live@<id>.service` |

- 미디어는 공용 `/opt/long-live/media` (같은 SHA256 파일은 다시 올리지 않음, `upload_many`). Playlist manifest만 채널별.
- 로그/상태/job JSON에 Stream Key 없음. job에는 SHA256 앞 16자리 지문만. 관리 명령 `--live-status`는 key 존재 여부만.

## 4. 잠금 / 동시 송출 제한

- **같은 채널 중복 송출 차단**: 채널 `worker.lock` (기본 채널은 기존 `state/worker.lock` — 수동/예약 공유).
- **서버 전체 최대 2개**: `state/slots/live1.lock`, `live2.lock` (flock). 세 번째는 시작하지 않고 FAILED + exit 3:
  "현재 Cloud에서 LIVE 2개가 실행 중입니다. 동시 송출은 최대 2개입니다."
- slot 선택은 `state/slots/slots.guard`로 순서를 맞춘다 (같은 시각 예약 2개가 동시에 시작해도 둘 다 자리를 얻음).
- **두 번째 LIVE 시작 전 자원 확인** (첫 번째는 기존처럼 바로 시작): MemAvailable ≥ 200MB, 디스크 ≥ 512MB,
  load1 ≤ 1.5 × CPU, 실행 중 FFmpeg < 2. 부족하면 두 번째만 거부하고 **첫 번째 LIVE는 건드리지 않는다**.
- 프로세스가 죽으면 OS가 flock을 풀어 준다 (stale lock 없음). slot 파일의 pid/채널은 표시용.
- PC도 시작 전에 `--live-status`로 한 번 더 확인 (서버가 최종 판단).
- Cloud worker는 DIRECT COPY만 지원한다 → 동시 transcode는 구조적으로 없다 (LIVE READY 파일만).

## 5. systemd 구조 결정: `long-live@<id>.service` (template)

| 기준 | 하나의 서비스가 FFmpeg 2개 관리 | **template `long-live@<id>`** (선택) |
|---|---|---|
| 단순성 | 새 supervisor 코드 필요 | 기존 Worker 그대로 + `--profile` |
| restart 격리 | Python crash 시 두 채널 같이 중단 | 채널 A worker만 재시작, B는 그대로 |
| 상태 확인 | 자체 구현 | `systemctl is-active long-live@senior` |
| rollback | 서비스 교체 | `systemctl disable --now long-live@<id>` 만, 기본 서비스 무관 |

예약 LIVE는 기존 `long-live-scheduler.service` 1개가 채널별 job을 최대 2개까지 스레드로 실행한다
(기존 구조 유지, job마다 별도 Worker·잠금·작업 폴더. 한 job 실패/취소가 다른 job을 멈추지 않음).

## 6. 예약 (scheduler)

- job에 `profile_id` (없으면 기본 채널).
- **같은 채널 겹침 차단** (서버 `--add-job` + PC가 YouTube 예약을 만들기 전에 `schedule_conflict`로 먼저 확인 → 유령 예약 방지).
- **다른 채널 같은 시각 허용**, 단 같은 시각 전체 2개까지.
- 실행 단계에서도 같은 채널 동시 실행 / 전체 3번째는 FAILED (v3 시절 저장된 겹침 job 대비).

## 7. PC 화면

- LIVE 창 ③ YouTube 송출 맨 위 **[현재 채널 ▼] [채널 관리]** — 채널마다 영상 Playlist · Stream Key · YouTube 연결 · Cloud 상태가 따로.
  Cloud LIVE 중인 채널이 있어도 다른 채널로 바꿀 수 있다 (송출은 계속, 상태 확인도 계속).
- 채널 2개 이상이면 창 위에 "실시간 Cloud LIVE ● 시니어 LIVE 01:42:16 · ● 일본 LIVE 00:37:05"와
  "예상 Cloud 송출 대역폭: 약 XX Mbps" (25 Mbps 초과 시 경고).
- [■ LIVE 종료]는 **지금 채널만** 정상 종료. 앱 종료 시 [PC만 종료](기본)는 Cloud에 아무 명령도 보내지 않는다.
- 메인 화면: "실시간 LIVE 시니어 채널 ● 01:42:16 · 일본 채널 ● 00:37:05".
- YouTube 자동 세션(API 자동 교체)은 한 번에 한 채널만. 다른 채널은 Stream Key 직접 입력.
- 예약 LIVE 창: 채널이 2개 이상이면 [예약할 채널 ▼]. 채널의 YouTube 연결/송출 스트림으로 예약하고 job에 채널 ID.

## 8. 용량 계산 (REAL 배포 전 별도 검증 필수)

DIRECT COPY는 디코딩/인코딩이 없어 CPU가 거의 들지 않지만, **2개도 반드시 REAL Gate에서 따로 측정**한다.

| 항목 | 1채널 | 2채널 | 근거 / REAL에서 확인할 것 |
|---|---|---|---|
| 송출 대역폭 | 6.3 Mbps × 1.1 ≈ 6.9 | ≈ 13.9 Mbps | 영상 비트레이트 합 + 10% 오버헤드. OCI 서버 egress 한도는 Console에서 확인 |
| FFmpeg CPU | 매우 낮음 | 매우 낮음 ×2 | 로컬 soak 실측 (아래). OCI vCPU에서 `top`으로 다시 측정 |
| RAM | FFmpeg RSS + Python worker | ×2 | 로컬 soak 실측. unit별 `MemoryMax=512M`, 두 번째 시작 전 MemAvailable ≥ 200MB |
| 디스크 | 공용 media | 같은 파일 재사용 | 다른 영상이면 합계만큼 필요 (업로드 전 공간 확인 기존 로직) |

로컬 soak: `python tools/multi_live_soak.py --minutes 30` (결과는 완료 보고에 기록).

## 9. REAL OCI 배포 Gate (사용자 승인 후에만)

1. 현재 LIVE가 없을 때 [처음 설정 도우미] → [무료 Cloud 자동 준비] (worker v4 + `long-live@.service` 설치).
2. 기본 채널 1채널 LIVE regression (기존과 같은지).
3. 채널 2개 Stream Key 각각 저장 → 동시 송출 → 세 번째 거부 확인.
4. A 중지 → B 유지, B 중지 → A 유지.
5. 서버 `top`/`free -m`/`ss -ti`로 CPU/RAM/대역폭 실측 (최소 30분).
6. 예약: 다른 채널 같은 시각 2개.
Rollback: `sudo systemctl disable --now long-live@<id>.service` (기본 서비스/예약은 그대로).
