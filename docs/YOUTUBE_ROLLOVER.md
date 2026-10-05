# YouTube 자동 방송 교체 (Phase 3B)

11시간 50분마다 YouTube Broadcast만 새 것으로 바꾸고, 영상 송출(FFmpeg / Cloud worker)은 끊지 않고 계속한다.

> **현재 상태**: 코드와 자동 테스트(가짜 Google/YouTube 서버) 완료. **실제 YouTube API: 아직 테스트하지 않음.**
> 이번 버전의 자동 교체는 **PC 프로그램이 켜져 있을 때** 동작한다. Cloud 단독 자동 교체는 별도 단계(아래 "Cloud 구조").

## 사용 순서 (초보자)

1. LIVE 창 ③에서 **YouTube 자동 세션 (API 연결)** 선택 → **[YouTube 자동 세션 연결]**
2. 도우미 5단계: YouTube Data API v3 켜기 → 데스크톱 앱 OAuth 클라이언트 만들기 → JSON 선택 → **[Google 계정 연결]**(브라우저 로그인/허용) → 채널 확인
3. LIVE 제목/설명/공개 상태, 다음 세션 제목 규칙(동일 제목 유지 권장 / `| LIVE #02` 회차 추가)
4. ⑥ 세션 관리에서 **YouTube 자동 교체** 선택 → **[▶ 24H LIVE 시작]**

처음에는 반드시 **비공개/일부공개**로 짧게 테스트한다 (아래 "실제 테스트 절차").

## ⚠ Testing 상태 OAuth 앱 — 7일 만료

Google 공식 문서: 외부(External) 사용자 유형 + 게시 상태 **"Testing"** 프로젝트는 YouTube scope의 refresh token이 **7일 후 만료**된다.
만료되면 자동 교체가 멈추고 "YouTube 연결이 만료되었거나 취소되었습니다" 오류가 난다. 장기 자동 운영 전 Google Cloud Console에서
OAuth 앱 게시 상태를 확인할 것. 화면(도우미 STEP 2/5, 오류 메시지)에도 표시한다.

## OAuth / 보안

- 데스크톱 앱 OAuth 2.0: 시스템 브라우저 + `http://127.0.0.1:<임의 포트>` loopback (Google이 OOB copy/paste 방식을 지원 종료).
- PKCE(S256), state(CSRF) 검사(`hmac.compare_digest`), `access_type=offline` + `prompt=consent`.
- scope: `https://www.googleapis.com/auth/youtube` 하나 (LIVE 생성/바인드/전환에 필요한 최소).
- 프로그램은 Google 비밀번호를 받지 않는다.
- 저장:
  - refresh token → Windows DPAPI 암호화 파일 `%APPDATA%\PlaylistLongVideoMaker\youtube_token.dat` (Windows 외: 메모리만)
  - OAuth Client JSON → **파일 위치만** settings.json에 (내용/secret 복사 없음)
  - settings.json의 `youtube`: client_file, channel_id/title, stream_id, stream_mode, template — 비밀 키 이름은 저장 함수가 거부
  - Stream Key(= reusable stream의 `streamName`) → 저장하지 않음. 시작할 때 API로 받아 FFmpeg/Cloud(stdin)로만 전달, 화면 표시 없음
- access/refresh token, client secret, Authorization 헤더는 로그/repr/오류 메시지에 남기지 않는다.
- `.gitignore`: `client_secret*.json`, `oauth_client*.json`, `youtube_token*.json`, `youtube_token.dat`
- 의존성: **Python 표준 라이브러리만** (`urllib`, `http.server`). Google 대형 SDK를 넣지 않았다 — 필요한 REST 호출이 10개 미만이고
  EXE 크기/회귀 위험을 늘리지 않기 위해.

## 스트림 모드

| 모드 | 설명 |
|---|---|
| `MANUAL_STREAM_KEY` (기본) | 기존처럼 사용자가 Stream Key를 입력. 자동 교체 불가 (보관 안전 모드 11:50 종료만). |
| `YOUTUBE_API_MANAGED` | 프로그램이 만든 **재사용(reusable) liveStream** 하나에 계속 송출. 방송마다 stream을 새로 만들지 않는다. |

reusable stream: 저장된 stream id → (없으면) 이 프로그램이 만든 표식(`Created by Playlist Long Video Maker (reusable)`)의 stream → 그래도 없으면
`liveStreams.insert`(rtmp, 1080p, 30fps, `isReusable=true`). 공식 문서: *"a video stream may be bound to more than one broadcast"* →
다음 방송을 같은 stream에 미리 bind할 수 있다.

## Broadcast 설정

`liveBroadcasts.insert`: title(최대 100자, 빈 값/`<>` 거부), description, scheduledStartTime, privacyStatus(public/unlisted/private),
selfDeclaredMadeForKids, `enableAutoStart=false`, `enableAutoStop=false`, `recordFromStart=true`, `enableDvr=true`,
`monitorStream.enableMonitorStream=false`.
- AutoStart를 끄는 이유: 다음 방송을 이미 active인 같은 stream에 미리 bind하므로, 켜 두면 의도치 않게 바로 시작될 수 있다.
- AutoStop을 끄는 이유: 송출이 잠깐 끊겨도 방송 전체가 끝나지 않게, complete는 프로그램이 명시적으로 한다.
- monitor stream을 끄는 이유: 켜져 있으면 live 전에 testing 단계가 필수다(공식 문서). 끄면 ready → live로 바로 전환.

## 교체 흐름과 상태

```text
API_READY → LIVE ──11:40──> PREPARING_NEXT → NEXT_READY ──11:50──> ROLLING_OVER → VERIFYING_NEXT → LIVE (세션 +1)
                                  └ 실패 → ROLLOVER_FAILED (재시도 5/10/30/60초, 현재 방송 유지)
```

11:50 교체 순서: ① 다음 방송/바인딩 재확인 ② **stream status active 확인** ③ 현재 방송 `complete` (확인)
④ 다음 방송 `live` (liveStarting → live 확인) ⑤ 새 세션 시계.

안전 규칙 (테스트로 고정):
- **다음 방송이 준비(바인딩)되지 않았으면 현재 방송을 complete하지 않는다.** 기본 정책 "방송 지속 우선":
  `⚠ 다음 방송 준비 실패 · 현재 방송 유지 중 (12시간을 넘으면 보관되지 않을 수 있습니다)` 표시 + 재시도.
  ("보관 우선" 정책은 다음 단계 후보 — 이번에는 넣지 않음.)
- stream이 inactive면 complete하지 않는다.
- complete 후 다음 방송 live 전환이 실패해도 다음 방송 정보를 유지한 채 재시도한다 (방송 정보가 없는 상태가 생기지 않음).
- insert 후 bind가 실패하면 재시도 때 같은 방송을 bind만 한다 (방송/quota 낭비 없음).
- API 오류로 FFmpeg/worker를 멈추지 않는다 (이 모듈은 송출을 제어하지 않음). 자동 교체 모드에서 FFmpeg/worker는 11:50에 멈추지 않는다.
- 재시도: 429, 5xx, 403 rateLimitExceeded/userRateLimitExceeded/backendError, 네트워크 → 지수 backoff(1/2/4/8초) 후 교체 단계 재시도(5/10/30/60초).
  설정 오류(insufficientPermissions, liveStreamingNotEnabled, quotaExceeded, forbidden, invalid_grant)는 **무한 재시도하지 않고** 한글 오류 + [다시 시도] 대기.
  401은 access token을 한 번 갱신 후 재시도.

## Quota (기본 10,000 units/day)

비용: insert/bind/transition/liveStreams.insert 50, list 1.
**상시 polling을 10~15초로 하면** broadcast+stream 조회만으로 하루 11,520 units → 기본 한도 초과. 그래서:
- 실행기는 10초마다 로컬 시계만 확인하고, API는 **필요할 때만**: 상시 상태 확인 5분 간격, 준비/교체 중에만 3초 간격.
- 추정(`estimate_daily_quota`): 하루 약 2회 교체 + 5분 확인 ≈ **1,000 units/day** (한도의 약 10%). 화면에는 "YouTube API 정상" 정도만 표시.

## 실제 테스트 절차 (사용자가 직접, 공개 방송 금지)

1. 공개 상태 **비공개** 또는 **일부공개**, 짧은 영상 Playlist.
2. 개발용 짧은 세션: 프로그램 실행 전에
   `set PLVM_DEV_MODE=1` 그리고 `set PLVM_DEV_SESSION_SECONDS=180` (최소 60초). 운영값 42600(11:50)은 바뀌지 않는다.
3. ⑥ YouTube 자동 교체로 시작 → 약 2분에 "다음 세션 준비됨" → 3분에 교체 → YouTube Studio에서 방송 2개(첫 방송 종료/보관, 두 번째 LIVE) 확인.
4. 한 번 더 3분 뒤 교체 확인 후 종료 → 마지막 방송 complete 확인.

## Cloud 구조 (별도 Gate, 이번에 배포하지 않음)

목표는 PC를 꺼도 교체되는 것이다. 그러려면 서버에 장기 credential(refresh token + client)이 있어야 한다.
이번 commit은 Windows 쪽 구현과 암호화 credential 모델까지만 한다:
- `youtube_api.py` / `youtube_session.py` / `youtube_oauth.py`(DPAPI 제외 부분)는 표준 라이브러리만 써서 서버 worker에 그대로 옮길 수 있게 만들었다.
- 서버 저장 후보: root 소유 0600 + systemd `LoadCredentialEncrypted=`(systemd-creds, TPM/host key) 등. 평문 token 파일은 쓰지 않는다.
- 서버 전달: Stream Key와 같은 stdin 방식, 권한/폐기(revoke) 절차, Testing 7일 만료 감시를 함께 설계한 뒤 별도 Gate로 진행한다.
