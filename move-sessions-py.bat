@echo off
chcp 65001 >nul
setlocal
title DSH session mover (python)
cd /d "%~dp0"

set "PYEXE="
where python >nul 2>nul && set "PYEXE=python"
if not defined PYEXE if exist "C:\tools\Python\Python311\python.exe" set "PYEXE=C:\tools\Python\Python311\python.exe"
if not defined PYEXE where py >nul 2>nul && set "PYEXE=py"
if not defined PYEXE (
  echo [X] Python not found. Install Python 3, or edit PYEXE in this file.
  pause
  exit /b 1
)

rem check zstandard; the script falls back to Node.js if it is missing
"%PYEXE%" -c "import zstandard" >nul 2>nul
if errorlevel 1 (
  echo [!!] python module 'zstandard' not found - will fall back to Node.js.
  echo      To install it:  "%PYEXE%" -m pip install zstandard
  echo.
)

"%PYEXE%" "%~dp0move-sessions.py" %*
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" echo [X] exit code %RC%
pause
exit /b %RC%
