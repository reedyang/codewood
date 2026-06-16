@echo off
rem Build executables with PyInstaller for the current project.
rem Produces a single one-dir bundle folder dist\codewood\ containing:
rem   codewood.exe      - console build with ALL terminal-UI and GUI logic
rem                       (default = terminal UI; "codewood app" = desktop GUI)
rem   codewood-gui.exe  - tiny windowed launcher that opens the GUI with no
rem                       console window (it just runs "codewood app")
rem   _internal\        - shared runtime + bundled resources
rem One-dir is used (instead of one-file) so each process runs directly
rem without an extra self-extracting bootloader process: the GUI then uses
rem two processes (app + serve backend) and the terminal UI uses one.

set ENTRY_SCRIPT=src\main.py

rem ---- Prepare the Python virtual environment so all build/runtime
rem ---- dependencies (PyInstaller, pywebview, ...) are ready before packaging.
rem ---- Mirrors bin\codewood.bat: create .venv-windows if missing, then
rem ---- install requirements.txt into it.
set VENV_DIR=.venv-windows
set VENV_PYTHON=%VENV_DIR%\Scripts\python.exe
set REQ_FILE=requirements.txt

if exist "%VENV_DIR%\Scripts\activate.bat" goto venv_ready

echo Virtual environment not found. Creating "%VENV_DIR%"...
set PY_BOOTSTRAP=
where python >nul 2>nul && set PY_BOOTSTRAP=python
if not defined PY_BOOTSTRAP where py >nul 2>nul && set PY_BOOTSTRAP=py
if not defined PY_BOOTSTRAP (
  echo Python executable not found. Please install Python or add it to PATH.
  exit /b 9009
)
%PY_BOOTSTRAP% -m venv "%VENV_DIR%"
if errorlevel 1 (
  echo Failed to create virtual environment.
  exit /b 1
)

:venv_ready
if not exist "%REQ_FILE%" (
  echo Requirements file not found: "%REQ_FILE%"
  exit /b 1
)
echo Installing/updating dependencies from "%REQ_FILE%"...
"%VENV_PYTHON%" -m pip install -r "%REQ_FILE%"
if errorlevel 1 (
  echo Failed to install dependencies.
  exit /b 1
)

rem Include required resources: rg.exe, skills, src resources, and the
rem desktop GUI (frontend bundle + pywebview host modules).
rem Using multiple --add-data flags (Windows uses ';' as separator)
rem Include virtual environment packages from .venv-windows
rem PyInstaller will search this path for modules
set VENV_PATH=%VENV_DIR%\Lib\site-packages

rem Build the desktop GUI frontend bundle first (run from project root).
pushd desktop\frontend
call npm install
if errorlevel 1 (
  echo Frontend dependency install failed.
  popd
  exit /b 1
)
call npm run build
if errorlevel 1 (
  echo Frontend build failed.
  popd
  exit /b 1
)
popd

rem Prefer the project venv's PyInstaller so the GUI dependencies (pywebview,
rem pythonnet) are discoverable; fall back to one on PATH otherwise.
set PYINSTALLER=.venv-windows\Scripts\pyinstaller.exe
if not exist "%PYINSTALLER%" set PYINSTALLER=pyinstaller

rem NOTE: --paths (pathex) is resolved relative to the current working
rem directory (the project root here), unlike --add-data sources which are
rem resolved relative to --specpath. So the venv path must NOT use "../../".
rem 1) codewood.exe (console, one-dir) carries ALL terminal-UI and GUI
rem    functionality. Output: dist\codewood\codewood.exe (+ _internal\).
"%PYINSTALLER%" --onedir --noconfirm --name codewood ^
  --icon "../../build/app_icon.ico" ^
  --add-data "../../vendors/rg.exe;bin" ^
  --add-data "../../skills;skills" ^
  --add-data "../../src;src" ^
  --add-data "../../desktop/frontend/dist;frontend" ^
  --add-data "../../desktop/host;host" ^
  --paths "%VENV_PATH%" ^
  --collect-all webview ^
  --collect-all pythonnet ^
  --collect-all clr_loader ^
  --hidden-import clr ^
  --specpath "build\\codewood" ^
  "%ENTRY_SCRIPT%"
if errorlevel 1 (
  echo codewood.exe build failed.
  exit /b 1
)

rem 2) codewood-gui.exe is a tiny windowed launcher. It bundles only the
rem standard library (no pywebview / prompt_toolkit / etc.) and simply starts
rem "codewood app" with no console window, so a double-click opens the GUI
rem without flashing a terminal window. It is emitted INTO the codewood
rem one-dir folder so it sits next to codewood.exe (single shippable folder).
"%PYINSTALLER%" --onefile --noconfirm --noconsole --name codewood-gui ^
  --icon "../../build/app_icon.ico" ^
  --distpath "dist\\codewood" ^
  --specpath "build\\codewood-gui" ^
  "desktop\host\launcher.py"
if errorlevel 1 (
  echo codewood-gui.exe build failed.
  exit /b 1
)

echo Build completed. The shippable folder is "dist\codewood".
echo   codewood\codewood.exe       - terminal UI (default) and "codewood app" for the GUI
echo   codewood\codewood-gui.exe   - double-click to open the GUI without a console window
