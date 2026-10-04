@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   YOUTUBE DOWNLOAD PEAK - Windows EXE Builder
echo ============================================================
echo.

rem ------------------------------------------------------------------
rem 0. ffmpeg binaries must be here BEFORE we spend time on pip installs,
rem    or the failure shows up several minutes later instead of instantly.
rem ------------------------------------------------------------------
set MISSING=0
if not exist "ffmpeg\ffmpeg.exe"  (echo   [MISSING] ffmpeg\ffmpeg.exe & set MISSING=1)
if not exist "ffmpeg\ffprobe.exe" (echo   [MISSING] ffmpeg\ffprobe.exe & set MISSING=1)
if not exist "ffmpeg\ffplay.exe"  (echo   [MISSING] ffmpeg\ffplay.exe & set MISSING=1)
if "%MISSING%"=="1" (
    echo.
    echo   Put ffmpeg.exe, ffprobe.exe and ffplay.exe directly inside the
    echo   "ffmpeg" folder next to this .bat file, then run it again.
    echo   See ffmpeg\PUT_FFMPEG_BINARIES_HERE.txt for where to get them.
    echo.
    pause
    exit /b 1
)
echo   ffmpeg.exe, ffprobe.exe, ffplay.exe - all found.
echo.

rem ------------------------------------------------------------------
rem 1. Find a Python
rem ------------------------------------------------------------------
set PYCMD=
where python >nul 2>nul && set PYCMD=python
if not defined PYCMD (
    where py >nul 2>nul && set PYCMD=py -3
)
if not defined PYCMD (
    echo   Python wasn't found on PATH.
    echo   Install it from https://www.python.org/downloads/
    echo   (tick "Add python.exe to PATH" during setup), then run this again.
    pause
    exit /b 1
)
echo   Using: %PYCMD%
echo.

rem ------------------------------------------------------------------
rem 2. Private virtual environment - keeps your normal Python install
rem    completely untouched. Reused on later runs, so this is
rem    a one-time cost.
rem ------------------------------------------------------------------
if not exist "build_venv\Scripts\python.exe" (
    echo   Creating build environment (first run only)...
    %PYCMD% -m venv build_venv
    if errorlevel 1 (
        echo   Failed to create the virtual environment.
        pause
        exit /b 1
    )
)

echo   Installing build dependencies - yt-dlp, pandas, mutagen, PyInstaller...
echo   (first run takes a few minutes; later runs are fast)
"build_venv\Scripts\python.exe" -m pip install --upgrade pip >nul
"build_venv\Scripts\python.exe" -m pip install --upgrade -r requirements.txt
if errorlevel 1 (
    echo.
    echo   Dependency install failed - see the error above.
    pause
    exit /b 1
)
echo.

rem ------------------------------------------------------------------
rem 3. Build
rem ------------------------------------------------------------------
echo   Building PEAK.exe ...
if exist "dist\PEAK" rd /s /q "dist\PEAK"
"build_venv\Scripts\pyinstaller.exe" PEAK.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo   Build failed - scroll up for the PyInstaller error.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Done. Your app:
echo       dist\PEAK\PEAK.exe
echo.
echo   Move/copy the WHOLE dist\PEAK folder as one unit if you
echo   relocate it - PEAK.exe needs the files sitting next to it.
echo ============================================================
echo.
start "" explorer "dist\PEAK"
pause
