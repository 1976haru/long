@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "YouTube_Playlist_Studio_Friend_Test_v0.3.exe" (
  echo 프로그램 EXE가 없습니다. 압축을 완전히 풀어 주세요.
  pause
  exit /b 1
)
start "" "YouTube_Playlist_Studio_Friend_Test_v0.3.exe"
