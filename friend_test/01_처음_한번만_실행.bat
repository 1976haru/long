@echo off
chcp 65001 >nul
cd /d "%~dp0"
title YouTube Playlist Studio - 처음 실행

set "APP=YouTube_Playlist_Studio_Friend_Test_v0.3.exe"

echo.
echo ==========================================
echo  YouTube Playlist Studio 친구 테스트판
echo ==========================================
echo.
echo 처음 실행에 필요한 준비를 확인합니다.
echo.

if not exist "%APP%" (
  echo [오류] %APP% 파일이 없습니다.
  echo 압축파일을 완전히 푼 뒤 다시 실행해 주세요.
  pause
  exit /b 1
)

where ffmpeg >nul 2>nul
if %errorlevel%==0 goto RUNAPP

echo FFmpeg가 아직 설치되지 않았습니다.
echo 영상 늘리기와 LIVE에 필요한 무료 도구를 자동 설치합니다.
echo.

where winget >nul 2>nul
if not %errorlevel%==0 (
  echo [자동 설치 불가]
  echo 이 PC에서 winget을 찾지 못했습니다.
  echo 프로그램을 먼저 실행한 뒤 [FFmpeg 설정]에서 ffmpeg.exe를 직접 지정해 주세요.
  echo.
  pause
  goto RUNAPP
)

winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
if not %errorlevel%==0 (
  echo.
  echo FFmpeg 자동 설치에 실패했습니다.
  echo 프로그램 안의 [FFmpeg 설정]에서 직접 지정할 수 있습니다.
  pause
)

:RUNAPP
echo.
echo 프로그램을 실행합니다.
start "" "%APP%"
exit /b 0
