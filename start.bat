@echo off
setlocal

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_local.ps1"

set EXIT_CODE=%ERRORLEVEL%
echo.
if not "%EXIT_CODE%"=="0" (
  echo DoraYmon stopped with exit code %EXIT_CODE%.
) else (
  echo DoraYmon stopped.
)
pause
exit /b %EXIT_CODE%
