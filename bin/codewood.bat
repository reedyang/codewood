@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "ROOT_DIR=%SCRIPT_DIR%.."
set "ENTRY=%SCRIPT_DIR%..\src\main.py"
set "APP_INFO=%SCRIPT_DIR%..\src\config\app_info.py"
set "VENV_DIR=%ROOT_DIR%\.venv-windows"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "REQ_FILE=%ROOT_DIR%\requirements.txt"
:: ---- Check if environment or dependencies are missing ----
set "INSTALL_NEEDED="
if not exist "%VENV_DIR%\Scripts\activate.bat" set "INSTALL_NEEDED=1"
if not exist "%SCRIPT_DIR%rg.exe" set "INSTALL_NEEDED=1"

if defined INSTALL_NEEDED (
    echo Environment or dependencies missing. Installing...
    call :install
    if errorlevel 1 exit /b %ERRORLEVEL%
 )
:: ---- Check Python dependencies via pip dry-run ----
if not defined INSTALL_NEEDED (
    if exist "%REQ_FILE%" (
        "%VENV_PYTHON%" -m pip install --dry-run -r "%REQ_FILE%" 2>&1 | findstr /R "^Collecting " >nul
        if not errorlevel 1 (
            echo Some Python dependencies are missing. Installing...
            call :install
            if errorlevel 1 exit /b %ERRORLEVEL%
        )
    )
)

call :set_title_from_app_info
call :run_main %*
exit /b %ERRORLEVEL%

:set_title_from_app_info
set "APP_NAME="
for /f "usebackq delims=" %%I in (`"%VENV_PYTHON%" -c "import runpy;d=runpy.run_path(r'%APP_INFO%');f=d.get('get_app_name');print(f() if callable(f) else '')" 2^>nul`) do (
    set "APP_NAME=%%I"
)
if defined APP_NAME title %APP_NAME%
exit /b 0

:run_main
"%VENV_PYTHON%" "%ENTRY%" --executable-name "%~nx0" %*
exit /b %ERRORLEVEL%

:: ---- Inlined former install.bat: create venv, install deps, copy rg.exe ----
:install
set "PY_BOOTSTRAP="
where python >nul 2>nul
if not errorlevel 1 (
    set "PY_BOOTSTRAP=python"
    goto :install_prepare_venv
)
where py >nul 2>nul
if not errorlevel 1 (
    set "PY_BOOTSTRAP=py"
    goto :install_prepare_venv
)
echo Python executable not found. Please install Python or add it to PATH.
exit /b 9009

:install_prepare_venv
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo Virtual environment not found. Creating "%VENV_DIR%"...
    %PY_BOOTSTRAP% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo Failed to create virtual environment.
        exit /b 1
    )
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

echo Copying rg.exe to bin directory...
copy /Y "%ROOT_DIR%\vendors\rg.exe" "%SCRIPT_DIR%"
if errorlevel 1 (
    echo Failed to copy rg.exe.
    exit /b 1
)

echo Installation completed successfully.
exit /b 0
