@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
  echo.
  echo Installation failed. Read the message above, then close this window.
) else (
  echo.
  echo Installation completed.
)
pause
endlocal
