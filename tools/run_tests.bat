@echo off
setlocal
cd /d "%~dp0\.."
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

REM The full suite. Run it before tagging a release: the release workflow no
REM longer runs it (five hours of billed runner time), so this and
REM scriptsuild_windows.bat are the gate now.

set "QT_QPA_PLATFORM=offscreen"
"%PY%" -m unittest discover -v
if errorlevel 1 (
  echo.
  echo Tests FAILED. Do not tag a release.
  exit /b 1
)

echo.
echo All tests passed.
