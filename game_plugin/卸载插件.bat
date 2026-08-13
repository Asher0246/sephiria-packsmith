@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
if errorlevel 1 (
  echo.
  echo Uninstallation failed. Read the message above, then close this window.
) else (
  echo.
  echo Uninstallation completed.
)
pause
endlocal
