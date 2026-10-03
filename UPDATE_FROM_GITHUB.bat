@echo off
chcp 65001 >nul
cd /d "%~dp0"
git rev-parse --is-inside-work-tree >nul 2>nul
if %errorlevel% neq 0 (
  echo 이 폴더는 Git clone이 아닙니다.
  echo docs\DEVELOPMENT.md의 clone 절차를 먼저 진행하세요.
  pause
  exit /b 1
)
git pull --ff-only
pause
