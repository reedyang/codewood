@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "ENTRY=%SCRIPT_DIR%src\main.py"

where python >nul 2>nul
if %ERRORLEVEL%==0 (
    python "%ENTRY%" %*
    exit /b %ERRORLEVEL%
)

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py "%ENTRY%" %*
    exit /b %ERRORLEVEL%
)

echo Python executable not found. Please install Python or add it to PATH.
exit /b 9009
