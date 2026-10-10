@echo off
setlocal EnableExtensions
cd /d "%~dp0\.."

echo ==========================================
echo  Taiko Fancy Arranger v3.0.0 Builder
echo  Output: Portable Windows folder
echo ==========================================
echo.

set "PYTHON_CMD="

py -3.12 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=py -3.12"

if not defined PYTHON_CMD (
    py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)

if not defined PYTHON_CMD (
    python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo ERROR: No suitable Python 3.10 or newer runtime was found.
    echo Recommended version: Python 3.12
    pause
    exit /b 1
)

echo Using Python:
%PYTHON_CMD% -c "import sys, platform; print(sys.executable); print(platform.python_version(), platform.architecture()[0])"
if errorlevel 1 goto :failed

echo.
echo [1/9] Preparing virtual environment...

if not exist ".venv\Scripts\python.exe" (
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 goto :failed
)

set "VENV_PYTHON=%CD%\.venv\Scripts\python.exe"
set "VENV_SCRIPTS=%CD%\.venv\Scripts"

echo.
echo [2/9] Installing build dependencies...

"%VENV_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 goto :failed

"%VENV_PYTHON%" -m pip install -r requirements-build.txt
if errorlevel 1 goto :failed

echo.
echo [3/9] Checking required localization files...

if not exist "translations\taiko_ja.ts" (
    echo ERROR: translations\taiko_ja.ts was not found.
    goto :failed
)

if not exist "i18n.py" (
    echo ERROR: i18n.py was not found.
    goto :failed
)

if not exist "settings.py" (
    echo ERROR: settings.py was not found.
    goto :failed
)

if not exist "settings_dialog.py" (
    echo ERROR: settings_dialog.py was not found.
    goto :failed
)

echo.
echo [4/9] Compiling Japanese translation...

if exist "%VENV_SCRIPTS%\pyside6-lrelease.exe" (
    "%VENV_SCRIPTS%\pyside6-lrelease.exe" ^
        translations\taiko_ja.ts ^
        -qm translations\taiko_ja.qm
) else (
    "%VENV_PYTHON%" -m PySide6.scripts.pyside_tool lrelease ^
        translations\taiko_ja.ts ^
        -qm translations\taiko_ja.qm
)

if errorlevel 1 (
    echo ERROR: Japanese translation compilation failed.
    goto :failed
)

if not exist "translations\taiko_ja.qm" (
    echo ERROR: translations\taiko_ja.qm was not created.
    goto :failed
)

echo Translation compiled:
dir "translations\taiko_ja.qm"

echo.
echo [5/9] Checking Python syntax...

"%VENV_PYTHON%" -m py_compile ^
    main.py ^
    ui\gui.py ^
    app\settings.py ^
    ui\settings_dialog.py ^
    app\i18n.py

if errorlevel 1 goto :failed

echo.
echo [6/9] Running tests...

"%VENV_PYTHON%" -m unittest discover -v
if errorlevel 1 goto :failed

echo.
echo [7/9] Cleaning previous build output...

if exist "build" rmdir /S /Q "build"
if exist "dist" rmdir /S /Q "dist"

echo.
echo [8/9] Building portable application...

"%VENV_PYTHON%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    TaikoFancyArranger.spec

if errorlevel 1 goto :failed

if not exist "dist\TaikoFancyArranger\TaikoFancyArranger.exe" (
    goto :missingexe
)

echo.
echo [9/9] Adding release documentation...

if exist "README.md" (
    copy /Y "README.md" "dist\TaikoFancyArranger\README.md" >nul
)

if exist "LICENSE" (
    copy /Y "LICENSE" "dist\TaikoFancyArranger\LICENSE" >nul
)

if exist "VERSION" (
    copy /Y "VERSION" "dist\TaikoFancyArranger\VERSION" >nul
)

rem Read the version rather than hardcoding it: the old literal path silently
rem shipped the previous release's notes for a whole version after each bump.
set "APP_VERSION="
if exist "VERSION" set /p APP_VERSION=<VERSION

if defined APP_VERSION if exist "docs\releases\v%APP_VERSION%.md" (
    copy /Y ^
        "docs\releases\v%APP_VERSION%.md" ^
        "dist\TaikoFancyArranger\RELEASE_NOTES.md" >nul
)

echo.
echo Verifying packaged Japanese translation...

if not exist "dist\TaikoFancyArranger\translations\taiko_ja.qm" (
    echo ERROR: Japanese translation was not packaged.
    echo Check the datas section in TaikoFancyArranger.spec.
    goto :failed
)

echo.
echo ==========================================
echo BUILD SUCCEEDED
echo ==========================================
echo.
echo Executable:
echo   dist\TaikoFancyArranger\TaikoFancyArranger.exe
echo.
echo Japanese translation:
echo   dist\TaikoFancyArranger\translations\taiko_ja.qm
echo.
echo Distribute the COMPLETE TaikoFancyArranger folder.
echo.

start "" "dist\TaikoFancyArranger"
pause
exit /b 0

:missingexe
echo.
echo ERROR: 