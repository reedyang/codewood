@echo off
setlocal
title Smart Shell

set "SCRIPT_DIR=%~dp0"
set "ENTRY=%SCRIPT_DIR%..\src\main.py"

where python >nul 2>nul
if not errorlevel 1 (
    python "%ENTRY%" %*
    exit /b %ERRORLEVEL%
)

where py >nul 2>nul
if not errorlevel 1 (
    py "%ENTRY%" %*
    exit /b %ERRORLEVEL%
)

echo Python executable not found. Please install Python or add it to PATH.
exit /b 9009
