@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\build_friend_test_package.ps1"
if errorlevel 1 (
  echo.
  echo 친구 테스트판 만들기 실패
  pause
  exit /b 1
)
echo.
echo 완성: release_friend_test\YouTube_Playlist_Studio_Friend_Test_v0.3.zip
pause
