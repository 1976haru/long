@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  set PY=py -3
) else (
  set PY=python
)
%PY% -m pip install --upgrade pyinstaller tzdata
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name PlaylistLongVideoMaker_v0.3 --add-data "cloud\long_live_worker.py;cloud" --add-data "deploy\linux;deploy\linux" main.py
if %errorlevel% neq 0 (
  echo.
  echo EXE 빌드 실패
  pause
  exit /b 1
)
echo.
echo 완료: dist\PlaylistLongVideoMaker_v0.3.exe
echo EXE 옆에 ffmpeg.exe / ffprobe.exe를 두거나, 프로그램에서 기존 FFmpeg 위치를 지정하세요.
pause
