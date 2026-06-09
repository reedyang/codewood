@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "ROOT_DIR=%SCRIPT_DIR%.."
set "ENTRY=%SCRIPT_DIR%..\src\main.py"
set "APP_INFO=%SCRIPT_DIR%..\src\config\app_info.py"
set "VENV_DIR=%ROOT_DIR%\.venv-windows"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
:: ---- Check if environment or dependencies are missing ----
set "INSTALL_NEEDED="
if not exist "%VENV_DIR%\Scripts\activate.bat" set "INSTALL_NEEDED=1"
if not exist "%SCRIPT_DIR%rg.exe" set "INSTALL_NEEDED=1"

if defined INSTALL_NEEDED (
    echo Environment or dependencies missing. Running install.bat...
    call "%SCRIPT_DIR%install.bat"
    if errorlevel 1 exit /b %ERRORLEVEL%
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
