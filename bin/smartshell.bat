@echo off
setlocal
title Smart Shell

set "SCRIPT_DIR=%~dp0"
set "ROOT_DIR=%SCRIPT_DIR%.."
set "ENTRY=%SCRIPT_DIR%..\src\main.py"
set "VENV_DIR=%ROOT_DIR%\.venv-windows"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "REQ_FILE=%ROOT_DIR%\requirements.txt"
set "PY_BOOTSTRAP="

where python >nul 2>nul
if not errorlevel 1 (
    set "PY_BOOTSTRAP=python"
    goto :prepare_venv
)

where py >nul 2>nul
if not errorlevel 1 (
    set "PY_BOOTSTRAP=py"
    goto :prepare_venv
)

echo Python executable not found. Please install Python or add it to PATH.
exit /b 9009

:prepare_venv
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo Virtual environment not found. Creating "%VENV_DIR%"...
    %PY_BOOTSTRAP% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo Failed to create virtual environment.
        exit /b 1
    )
) else (
    "%VENV_PYTHON%" "%ENTRY%" %*
    exit /b %ERRORLEVEL%
)

if not exist "%REQ_FILE%" (
    echo Requirements file not found: "%REQ_FILE%"
    exit /b 1
)

echo Installing dependencies from "%REQ_FILE%"...
"%VENV_PYTHON%" -m pip install -r "%REQ_FILE%"
if errorlevel 1 (
    echo Failed to install dependencies.
    exit /b 1
)

"%VENV_PYTHON%" "%ENTRY%" %*
exit /b %ERRORLEVEL%
