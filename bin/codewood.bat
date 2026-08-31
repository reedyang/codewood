@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "ROOT_DIR=%SCRIPT_DIR%.."
set "ENTRY=%SCRIPT_DIR%..\cli\main.py"
set "APP_INFO=%SCRIPT_DIR%..\cli\config\app_info.py"
set "VENV_DIR=%ROOT_DIR%\.venv-windows"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "REQ_FILE=%ROOT_DIR%\requirements.txt"
:: ---- Check if environment or dependencies are missing ----
:: The venv must use Python 3.9-3.13 (3.14+ may have compatibility issues).
:: The embedding backend uses ONNX Runtime for all platforms. An incompatible
:: venv is recreated automatically.
set "INSTALL_NEEDED="
set "VENV_INCOMPATIBLE="
if not exist "%VENV_DIR%\Scripts\activate.bat" set "INSTALL_NEEDED=1"
if not defined INSTALL_NEEDED (
    "%VENV_PYTHON%" -c "import sys; sys.exit(0 if sys.version_info < (3,14) else 1)" >nul 2>nul
    if errorlevel 1 (
        set "INSTALL_NEEDED=1"
        set "VENV_INCOMPATIBLE=1"
    )
)

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
call :download_model
call :run_main %*
exit /b %ERRORLEVEL%

:download_model
set "DOWNLOAD_SCRIPT=%SCRIPT_DIR%download_embedding_model.py"
if not exist "%DOWNLOAD_SCRIPT%" exit /b 0
"%VENV_PYTHON%" -c "import sys; sys.path.insert(0, r'%ROOT_DIR%'); from cli.tools.embedding import _resolve_model_path, _EMBEDDING_MODEL_NAME; p=_resolve_model_path(_EMBEDDING_MODEL_NAME); exit(0 if p else 1)" >nul 2>&1
if not errorlevel 1 exit /b 0
echo Embedding model not found. Downloading...
"%VENV_PYTHON%" "%DOWNLOAD_SCRIPT%"
exit /b 0

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

:: ---- Inlined former install.bat: create venv, install deps ----
:: Bootstrap with any Python 3.9-3.13. Prefer the "py" launcher with an
:: explicit version, then "python", then the "py" default -- each is
:: rejected if it is 3.14+.
:install
set "PY_BOOTSTRAP="
where py >nul 2>nul
if not errorlevel 1 (
    for %%P in (3.13 3.12 3.11 3.10) do (
        if not defined PY_BOOTSTRAP (
            py -%%P -c "import sys; sys.exit(0 if sys.version_info < (3,14) else 1)" >nul 2>nul
            if not errorlevel 1 set "PY_BOOTSTRAP=py -%%P"
        )
    )
)
if defined PY_BOOTSTRAP goto :install_prepare_venv
where python >nul 2>nul
if not errorlevel 1 (
    python -c "import sys; sys.exit(0 if sys.version_info < (3,14) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY_BOOTSTRAP=python"
)
if defined PY_BOOTSTRAP goto :install_prepare_venv
where py >nul 2>nul
if not errorlevel 1 (
    py -c "import sys; sys.exit(0 if sys.version_info < (3,14) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY_BOOTSTRAP=py"
)
if defined PY_BOOTSTRAP goto :install_prepare_venv
echo No compatible Python found. CodeWood requires Python 3.9-3.13
echo (Python 3.14+ is not supported yet). Please install Python 3.13
echo and add it to PATH.
exit /b 9009

:install_prepare_venv
if defined VENV_INCOMPATIBLE (
    echo Existing "%VENV_DIR%" was created with an incompatible Python. Recreating...
    rmdir /s /q "%VENV_DIR%"
    set "VENV_INCOMPATIBLE="
)
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

echo Installation completed successfully.
exit /b 0
