@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3) else (set PY=python)
%PY% -m pytest -q || goto :fail
%PY% -m compileall -q . || goto :fail
git diff --check || goto :fail
echo.
echo PASS - 테스트/컴파일/diff 검증 완료
pause
exit /b 0
:fail
echo.
echo FAIL - 위 오류를 먼저 수정하세요.
pause
exit /b 1
