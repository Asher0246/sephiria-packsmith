@echo off
setlocal
set "APP_ROOT=%~dp0"
set "APP_PYTHON=%~dp0runtime\python.exe"

if exist "%APP_PYTHON%" goto runtime_ready
set "APP_PYTHON=%~dp0runtime\Scripts\python.exe"

if not exist "%APP_PYTHON%" goto missing_runtime

:runtime_ready
pushd "%APP_ROOT%"
"%APP_PYTHON%" -m app.server %*
set "APP_EXIT_CODE=%ERRORLEVEL%"
popd

if "%APP_EXIT_CODE%"=="0" goto finished
echo.
echo The solver exited with error code %APP_EXIT_CODE%.
echo Review the error message above before closing this window.
pause
goto finished

:missing_runtime
echo Python runtime was not found at:
echo %APP_PYTHON%
echo Re-extract the release package, or run setup.ps1 in a source checkout.
pause

:finished
endlocal
