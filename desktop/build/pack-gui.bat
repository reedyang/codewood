@echo off
rem Build the Code Wood desktop GUI (codewoodw.exe) with PyInstaller.
rem Steps: build the TypeScript frontend, then package the pywebview host.
rem The backend codewood.exe is intentionally NOT bundled; at runtime the
rem GUI launches the sibling codewood.exe in the same folder.

setlocal
rem Run from the project root regardless of where the script is invoked.
cd /d "%~dp0..\.."

set VENV_PATH=.venv-windows\Lib\site-packages

rem 1. Build the frontend bundle into desktop\frontend\dist
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

rem 2. Package the host into dist\codewoodw.exe
rem Paths after --specpath are resolved relative to build\codewoodw.
pyinstaller --onefile --name codewoodw --noconsole ^
  --icon "../../build/codewood.ico" ^
  --add-data "../../desktop/frontend/dist;frontend" ^
  --paths "../../%VENV_PATH%" ^
  --paths "../../desktop/host" ^
  --collect-all webview ^
  --specpath "build\\codewoodw" ^
  "desktop\host\codewoodw.py"

echo Build completed. Executable is in the "dist" folder.
echo Place codewoodw.exe next to codewood.exe before running.
pause
