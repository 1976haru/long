# 무료 Cloud LIVE (OCI Always Free) — 사용자/개발 안내

## 한눈에

```text
영상 선택 → (필요하면) LIVE READY 파일 만들기 → 무료 Cloud 처음 설정 → [▶ 24H LIVE 시작]
```

- 무료 Cloud에서 방송하면 **PC를 꺼도 방송이 계속**됩니다.
- 무료 Cloud를 쓸 수 없으면 **[내 PC에서 LIVE]** 한 번으로 PC 송출로 바꿉니다.

## 실행 우선순위

1. OCI Always Free Cloud (DIRECT COPY)
2. 내 PC DIRECT COPY
3. 내 PC 하드웨어 인코더 — 다음 단계 예정 (현재 미구현)
4. 내 PC libx264 재인코딩 (고급, 사용자가 직접 선택할 때만)

무료 Cloud를 쓸 수 없으면 유료 Cloud를 추천하거나 만들지 않고, 내 PC 모드로 안내합니다.

## 무료 안전 원칙 (FREE SAFETY)

- 이 프로그램은 **유료 Cloud 자원을 자동 생성하지 않습니다.**
- OCI/AWS/GCP/Azure 등 Cloud API를 호출하지 않습니다 (서버 생성, 결제, shape, 스토리지, 크기 변경 모두 없음).
- Oracle 가입·로그인·서버 생성은 사용자가 브라우저에서 직접 합니다. 프로그램은 공식 페이지를 열어주기만 합니다.
- 서버가 무료인지 프로그램이 단정하지 않습니다 → **"무료 여부를 Oracle Console에서 확인하세요."**
- 서버 생성 화면에서 **"Always Free Eligible" 표시**를 반드시 확인하세요.
- GPU Cloud, 유료 VM, 유료 스토리지를 만들지 않습니다.

## 서버 만들 때 고를 값

| 항목 | 값 |
|---|---|
| 이미지 | Ubuntu LTS |
| Shape (우선) | Ampere A1 (VM.Standard.A1.Flex), 1 OCPU / 1GB 이상 |
| Shape (A1 자리가 없을 때) | VM.Standard.E2.1.Micro (Always Free) |
| SSH 키 | Private Key 저장 → 도우미 3단계에서 선택 |
| 네트워크 | 공용 IP, 보안 목록에서 22번(SSH) 허용 (기본값) |

리전 안내: Always Free 자원은 가입 때 정한 홈 리전에서만 만들 수 있고, 홈 리전은 나중에 바꿀 수 없습니다.
South Korea North(Chuncheon) 등 일부 리전은 A1 무료 용량이 부족하다는 보고가 많습니다. 정확한 조건은 Oracle 공식 문서와 Console 표시를 따르세요.

무료 자리가 없을 때: **"무료 서버 자리가 현재 없습니다. 비용이 발생하는 서버를 만들지 말고 [내 PC에서 LIVE]를 사용하세요."**

## 유휴 회수(idle reclamation) / 용량 부족 — 숨기지 않음

- Oracle은 일정 기간 사용량(CPU·네트워크·메모리)이 낮은 Always Free 인스턴스를 회수할 수 있습니다. DIRECT COPY 송출은 부하가 매우 낮아 해당될 수 있습니다. 기준은 Oracle 공식 문서를 확인하세요.
- 이 프로그램은 회수 정책을 피하기 위한 가짜 CPU 부하, busy loop, 인위적 트래픽을 **만들지 않습니다.**
- Cloud 연결 실패 시 화면에는 "무료 Cloud 서버를 사용할 수 없습니다." + **[다시 확인] / [내 PC에서 LIVE]** 만 표시합니다.

## LIVE READY / DIRECT COPY

- LIVE READY = H.264(yuv420p) + AAC(44.1/48kHz 스테레오) + keyframe ≤ 4초(권장 2초) + 정상 타임스탬프.
- 분석은 ffprobe만 사용하고 초반 60초 packet 헤더만 읽습니다 (frame 디코딩 없음).
- LIVE READY 파일은 재인코딩 없이 그대로 송출합니다: `-re -stream_loop -1 -i FILE -c:v copy -c:a copy -f flv RTMPS`.
- 아니면 **[LIVE READY 파일 만들기]**: PC에서 한 번만 변환 → `원본이름_LIVE_READY.mp4` (원본 유지). H.264 / 30fps / 2초 GOP / AAC 128k 44.1kHz 스테레오, 1080p 이하 해상도 유지, 소리 길이를 영상에 맞춤.
- 24시간 Cloud에서 재인코딩하지 않습니다 (Cloud worker는 DIRECT COPY 전용).

측정 (이 개발 PC, 1080p30 8Mbps 파일, FFmpeg 7.1):

| 방식 | FFmpeg RAM | CPU |
|---|---|---|
| DIRECT COPY | 약 16 MB | 약 0% |
| TRANSCODE (libx264 veryfast) | 약 926 MB | 약 61% (1코어 기준) |

## 서버 구조

```text
/opt/long-live/worker/long_live_worker.py   (root 소유, 0644)
/opt/long-live/media/                       (업로드 사용자 소유, longlive 읽기)
/opt/long-live/state/status.json            (longlive, 0644, 원자적 교체)
/opt/long-live/logs/worker.log              (512KB × 2 회전)
/etc/long-live/live.json                    (root:longlive 0640, secret 없음)
/etc/long-live/stream.key                   (longlive 0600)
/etc/systemd/system/long-live.service
```

- 서비스 사용자 `longlive` (로그인 불가). FFmpeg를 root로 실행하지 않습니다.
- systemd: `Restart=on-failure`, `RestartSec=5`, 설정 오류(exit 3)는 재시작 반복 안 함, `KillMode=mixed`(worker가 FFmpeg에 q), `MemoryMax=512M`, `ProtectSystem=strict`.
- 책임 분리: YouTube/FFmpeg 연결 문제 → worker watchdog (5/10/30/60초) / worker crash → systemd / 서버 reboot → `enable` 상태면 자동 복구.
- 서비스는 설치 때 켜지 않습니다. [LIVE 시작] 때 `enable + restart`, [LIVE 종료] 때 `disable --now`.

### 설정/Worker 버전 (Phase 3A)

- `live.json` schema v2: `media`가 목록이면 Playlist, `session_mode`(continuous/archive_safe), `session_id`.
- 단일 영상 + 계속 방송은 v1 worker와도 호환되는 형식으로 쓴다. Playlist/보관 안전 모드는 worker v2 필요 →
  구버전이면 시작하지 않고 "방송이 끝난 뒤 [무료 Cloud 자동 준비]를 다시 실행"을 안내한다 (운영 중 서버를 자동으로 바꾸지 않음).
- 보관 안전 모드: `state/session.json`에 세션 시작 시각 저장, 11:50에 정상 종료(exit 0), 완료된 세션은 재부팅 후에도 다시 송출하지 않음.

## 처음 설정 도우미가 하는 일 (상세 보기에서만 표시)

1. 서버 연결 (`ssh ... echo`)
2. 환경 확인 (Ubuntu/Debian, 아키텍처, 디스크 ≥ 2GB, python3, sudo, OCI metadata의 shape 표시)
3. FFmpeg 준비 (`apt-get install ffmpeg` — 없을 때만)
4. LIVE Worker 설치 (worker/서비스/스크립트를 stdin으로 업로드 → `install.sh`)
5. 자동 복구 설정 확인 (systemd unit 로드)
6. 완료 (`--self-check`)

## SSH / Stream Key 보안

- Windows 내장 `ssh.exe`(없으면 PATH의 ssh)를 argument list로 실행. `shell=True` 없음. Python SSH 라이브러리 없음.
- `BatchMode=yes`, 앱 전용 known_hosts(`StrictHostKeyChecking=accept-new`), destination 앞 `--`.
- 서버 IP/사용자 이름은 엄격하게 검사, 원격 경로는 `shlex.quote`, 서버 영상 이름은 영문/숫자/`._-`만 사용.
- SSH Private Key는 **경로만** settings.json에 저장 (내용은 읽거나 저장하지 않음).
- Stream Key는 SSH 명령줄에 넣지 않고 stdin으로 보내 `stream.key.part → chown/chmod 600 → mv` 원자적 교체.
- Windows OpenSSH는 키 파일 권한이 넓으면 거부합니다 → [키 파일 권한 고치기] (사용자 확인 후 `icacls`로 본인만 읽기).
- 한계: 서버 안에서 실행 중인 FFmpeg의 명령줄(송출 URL)은 같은 서버의 다른 사용자에게 `ps`로 보일 수 있습니다. 1인용 무료 서버를 전제로 하며, 다른 사람과 서버를 공유하지 마세요.

## 영상 업로드

로컬 SHA256 → 서버 SHA256 비교 → 같으면 생략 → 다르면 `이름.mp4.part`로 1MB씩 stdin 전송(진행률) → 서버 SHA256 검증 → `mv` 원자적 교체. 실패/중지 시 `.part`만 지우고 기존 영상은 그대로 둡니다.

## PC 프로그램 종료

- **Cloud LIVE 중**: "Cloud에서 LIVE가 계속 방송 중입니다. PC 프로그램만 종료할까요?" → **[PC만 종료 (권장)]** / [LIVE도 종료] / [취소]. PC만 종료는 서버에 아무 명령도 보내지 않습니다.
- **내 PC LIVE 중**: 기존처럼 확인 후 FFmpeg 정상 종료(q) → 프로그램 종료.

## 상태 확인

10초마다 SSH 1회로 상태를 읽습니다 (서버/Worker/LIVE/방송 시간/영상/방식/FPS/Bitrate/재접속/오류/디스크). 서버 로그는 [상세 보기]에서 요청할 때만 최근 50줄. PC 메모리에 무한 로그를 쌓지 않습니다 (작업 기록 200줄, FFmpeg 오류 30줄, 상태/재접속 기록 100건).

## 테스트 범위

- 실제 OCI/Oracle 계정 접속은 자동 테스트하지 않습니다 (가짜 SSH 서버로 명령/보안/업로드 검증).
- Cloud worker는 실제 FFmpeg로 로컬 FLV 출력 headless 테스트, WSL Ubuntu 24.04에서 상태/환경 스크립트와 SIGTERM→q 종료를 확인했습니다.
- **OCI REAL TEST: NOT TESTED**
