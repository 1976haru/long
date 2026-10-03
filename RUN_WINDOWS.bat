@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 main.py
    goto :eof
)
where python >nul 2>nul
if %errorlevel%==0 (
    python main.py
    goto :eof
)
echo.
echo Python 3를 찾지 못했습니다.
echo Python 3 설치 후 다시 실행해주세요.
echo.
pause
