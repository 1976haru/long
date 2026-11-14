# 최종 EXE 배포 준비 (통합 03 이후 · 사용자 승인 후 1회)

> 이 문서는 **준비 자료**입니다. 통합 01~03 동안에는 EXE를 빌드·교체하지 않았습니다.
> 기존 `dist\PlaylistLongVideoMaker_v0.3.exe`는 그대로이며, 사용자가 ①~⑥ 화면을 직접 확인하고 승인한 뒤에만 아래 순서로 1회 진행합니다.
> 규칙의 원문은 `CLAUDE.md` 9번 (실사용 프로그램 1개 · 백업 → 같은 이름으로 교체 → 검증 → 확정 또는 원상복구).

## 1. 빌드 설정 점검 결과

| 항목 | 현재 상태 | 비고 |
|---|---|---|
| 진입점 | `main.py` (`--self-test <결과.json>` 지원) | 격리 설정에서 화면 자가 점검 |
| 빌드 방식 | PyInstaller `--onefile --windowed`, 이름 `PlaylistLongVideoMaker_v0.3` | `BUILD_WINDOWS_EXE.bat`, `PlaylistLongVideoMaker_v0.3.spec` |
| 포함 자료 | `cloud\long_live_worker.py`, `deploy\linux\`, `docs\*.html` | 그대로 |
| 새 모듈 ④⑤⑥ + 오디오 | `app/bg_*`, `app/stock_*`, `app/pl_*`, `app/media_audio.py` — 순수 Python, 표준 라이브러리(urllib·ctypes·json·hashlib)만 | 새 pip 의존성 없음. 창은 함수 안에서 import하지만 PyInstaller는 함수 안 import도 수집함 → 빌드 후 `--self-test`의 `bg_*`/`stock_window`/`playlist_window` 항목과 `bg_audio_ui`(④⑤ 오디오 표시·무음 사본 버튼)로 실제 포함 확인 |
| 금지 라이브러리 | `keyring`·`requests`·클라우드 SDK 미사용 (`tests/test_cloud_security.py`) | ⑤ 기존 키 호환은 Windows `CredReadW` 읽기 전용 |
| 개발 환경 | Python 3.14.4 · PyInstaller 6.22.3 | 같은 버전으로 빌드 권장 |
| tzdata | 빌드 bat에서 설치 | 예약 시간대 계산용 |

권장(선택): 빌드 명령에 `--hidden-import app.stock_ui --hidden-import app.pl_ui --hidden-import app.bg_ui`를 더하면 수집 누락 위험이 더 줄어듭니다 (동작 변화 없음).

## 2. FFmpeg 의존성 (EXE에 포함하지 않음)

프로그램은 이전 설정 → EXE 옆 `bin\` → PATH → WinGet Gyan.FFmpeg → `C:\ffmpeg\bin` 순으로 찾습니다. `ffmpeg.exe`·`ffprobe.exe`는 같은 폴더에 있어야 합니다.

| 기능 | 필요한 FFmpeg 구성 | 없을 때 |
|---|---|---|
| ① 영상 늘리기 / ② LIVE | ffmpeg·ffprobe (기본 기능) | 시작 불가 + 안내 |
| ④ 배경 만들기 / ⑥ 영상 인코딩 | libx264 | 제작 불가 |
| ④⑥ H.265 저장용 | libx265 | H.264로 만들고 안내 (④) / 선택지 비활성 안내 (⑥) |
| ⑤ 미리보기 재생 | `ffplay.exe` (같은 폴더) | 미리보기만 불가, 검색·다운로드는 가능 |
| ⑥ 곡 제목 입히기 | libass (`subtitles` 필터) | 제작 전에 "자막 입히기 불가" 안내, SRT 파일은 계속 저장 가능 |
| 배경 원본 오디오 검사·무음 사본 | ffprobe · ffmpeg 스트림 복사 (추가 구성 없음) | 검사는 '확인 불가'로 표시, 무음 사본만 불가 |

현재 개발 PC의 WinGet Gyan FFmpeg는 위 항목을 모두 포함합니다 (확인: libx264·libx265·libmp3lame·aac·subtitles·acrossfade·ffplay).

## 3. Windows 10/11 · Python 미설치 PC

- onefile EXE에 Python 런타임·Tcl/Tk·VC 런타임 DLL이 들어갑니다 → Python 설치 불필요
- 64비트 Windows 10(19045 확인)/11. 관리자 권한 불필요 (설정은 `%APPDATA%\PlaylistLongVideoMaker`)
- 첫 실행 시 onefile 압축 해제로 몇 초 걸릴 수 있음. 백신이 임시 폴더 실행을 늦출 수 있음
- FFmpeg는 별도 (위 2번). DPAPI(키·토큰 암호화)는 같은 Windows 사용자 계정에서만 풀립니다 → 다른 PC로 설정 폴더를 복사해도 키·토큰은 다시 넣어야 함

## 4. 파일명·경로 유지

- 실행 파일: `dist\PlaylistLongVideoMaker_v0.3.exe` (이름·위치 그대로 교체 — 바로가기·사용 습관 유지)
- 소스 실행: `RUN_WINDOWS.bat` (`py -3 main.py`) — 수정하지 않음
- 별도 시험판 EXE·배포 폴더·바로가기를 만들지 않음

## 5. 백업 계획 (교체 직전)

`dist\_recovery\before_update_<YYYYMMDD_HHMMSS>\` 에:
1. 현재 `PlaylistLongVideoMaker_v0.3.exe` 사본
2. `%APPDATA%\PlaylistLongVideoMaker` 폴더 전체 사본 (settings.json, live_secret*.dat, youtube_token*.dat, stock_key_*.dat, 대기열 등 — **내용을 열거나 출력하지 않음**)
3. `manifest.sha256` (위 모든 파일의 SHA256)
4. 복원 시험: 사본의 SHA256이 manifest와 같은지 확인

사용자 영상·음원·다운로드 라이브러리(`D:\PlaylistStudio_StockLibrary`)·출력 폴더는 프로그램이 바꾸지 않으므로 백업 대상이 아니며, 절대 지우지 않습니다.

## 6. 빌드 → 교체 → 검증 순서

교체 전 확인: 프로그램(EXE·RUN_WINDOWS.bat) 종료, LIVE·예약 업로드·영상 제작이 진행 중이 아님. 실행 중이면 **교체하지 않고 보고** (강제 종료 금지).

```bat
REM 0) 저장소 루트에서 (add-data 상대 경로가 저장소 기준이므로 --specpath를 저장소 밖으로 옮기지 않는다)
cd /d "D:\03 long"
py -3 -m app.manual_html

REM 1) 저장소 밖 임시 폴더로 빌드 (dist·build 위치만 바꿈)
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name PlaylistLongVideoMaker_v0.3 ^
  --distpath "D:\PLVM_EXE_BUILD_TMP\dist" --workpath "D:\PLVM_EXE_BUILD_TMP\work" ^
  --add-data "cloud\long_live_worker.py;cloud" --add-data "deploy\linux;deploy\linux" --add-data "docs\*.html;docs" ^
  --hidden-import app.stock_ui --hidden-import app.pl_ui --hidden-import app.bg_ui main.py

REM 2) 새 EXE 자가 점검 (격리 설정 폴더)
set PLVM_TEST_SETTINGS_DIR=D:\PLVM_EXE_BUILD_TMP\selftest_settings
"D:\PLVM_EXE_BUILD_TMP\dist\PlaylistLongVideoMaker_v0.3.exe" --self-test "D:\PLVM_EXE_BUILD_TMP\selftest_new.json"
set PLVM_TEST_SETTINGS_DIR=
```

`selftest_new.json`의 `all_ok`가 `true`이고 `bg_*`(`bg_audio_ui` 포함)·`stock_window`·`playlist_window`가 모두 OK일 때만 계속합니다.

3) 백업(5번) → 4) 같은 이름으로 교체 (`dist\PlaylistLongVideoMaker_v0.3.exe`) → 5) 교체본으로 같은 `--self-test` 다시 실행 →
6) 실제 설정 폴더 SHA256이 백업 manifest와 같은지 확인 (프로그램이 시작만으로 설정을 바꾸지 않았는지) → 7) 성공 확정, 임시 빌드 폴더 정리.

## 7. 실패 시 복원

- 2)에서 실패: 기존 EXE는 손대지 않았으므로 아무것도 하지 않음 (임시 빌드 폴더만 정리)
- 5)·6)에서 실패: 백업 폴더의 EXE를 같은 이름으로 즉시 되돌리고, SHA256으로 원본과 같은지 확인. 설정 폴더는 초기화하지 않음
  (프로그램이 설정을 바꾼 것이 확인된 경우에만, 사용자 확인 후 백업 사본에서 해당 파일만 복원)
- 이전 시도에서 확인된 주의점: PyInstaller `--specpath`를 저장소 밖으로 두면 `--add-data` 상대 경로가 그 폴더 기준이 되어 빌드가 실패합니다 → 위 명령처럼 `--distpath`/`--workpath`만 옮깁니다

## 8. 최종 체크리스트

- [ ] 사용자가 ①~⑥ 화면을 `RUN_WINDOWS.bat`으로 확인하고 승인함
- [ ] `py -3 -m pytest -q -rs` 전체 통과 · `py -3 -m compileall -q app tests` · `git diff --check`
- [ ] 프로그램 종료, LIVE·예약 업로드·제작 진행 중 아님
- [ ] 저장소 밖 임시 폴더 빌드 성공, 새 EXE `--self-test` all_ok
- [ ] 백업 폴더 + SHA256 manifest + 복원 시험
- [ ] 같은 이름으로 교체, 교체본 `--self-test` all_ok
- [ ] 실제 설정·토큰 파일 SHA256 변화 없음
- [ ] ④ 결과 오디오 0개 · ⑥ 결과 오디오 1개(음원만) — 실제 자연 영상(오디오 포함)으로 1회 확인
- [ ] 실제 LIVE 송출·YouTube 업로드는 사용자가 직접 (자동 실행 안 함)
