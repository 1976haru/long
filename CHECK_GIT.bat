@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Git 상태 ===
git status
echo.
echo === Remote ===
git remote -v
echo.
echo === 현재 브랜치 ===
git branch --show-current
echo.
pause
