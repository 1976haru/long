@echo off
chcp 65001 >nul
cd /d "%~dp0"
git fetch origin
git switch develop
git pull --ff-only origin develop
echo.
echo 현재 브랜치:
git branch --show-current
pause
