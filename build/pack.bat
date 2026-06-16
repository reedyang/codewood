@echo off
rem Build executable with PyInstaller for the current project.
rem This single executable provides both the terminal UI (default) and the
rem desktop GUI (run "codewood app"). Adjust as needed.

set ENTRY_SCRIPT=src\main.py

rem Include required resources: rg.exe, skills, src resources, and the
rem desktop GUI (frontend bundle + pywebview host modules).
rem Using multiple --add-data flags (Windows uses ';' as separator)
rem Include virtual environment packages from .venv-windows
rem PyInstaller will search this path for modules
set VENV_PATH=.venv-windows\Lib\site-packages

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
"%PYINSTALLER%" --onefile --name codewood ^
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

echo Build completed. Executable is in the "dist" folder.
echo Run "codewood" for the terminal UI or "codewood app" for the desktop GUI.
pause
