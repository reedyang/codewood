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

set ENTRY_SCRIPT=cli\main.py

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

rem Include required resources: skills, cli resources, and the
rem desktop GUI (frontend bundle + pywebview host modules).
rem Note: ripgrep (rg) is downloaded at runtime on first launch if not
rem already present in bin/; it is no longer bundled at build time.
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

rem ---- Download the embedding model before building so PyInstaller can bundle it ----
echo Checking embedding model for offline bundle...
set "MODEL_NAME=all-MiniLM-L6-v2"
if not exist "models\%MODEL_NAME%\config.json" (
    echo Downloading embedding model...
    "%VENV_PYTHON%" -c "from sentence_transformers import SentenceTransformer; m = SentenceTransformer('%MODEL_NAME%', device='cpu'); m.save(r'models\%MODEL_NAME%')"
    if errorlevel 1 (
        echo WARNING: Could not download embedding model. The package will require online HF access.
    )
) else (
    echo Embedding model already cached in models\%MODEL_NAME%.
)

rem NOTE: --paths (pathex) is resolved relative to the current working
rem directory (the project root here), unlike --add-data sources which are
rem resolved relative to --specpath. So the venv path must NOT use "../../".
rem 1) codewood.exe (console, one-dir) carries ALL terminal-UI and GUI
rem    functionality. Output: dist\codewood\codewood.exe (+ _internal\).
rem    Uses the pre-generated spec file which includes the application manifest.
"%PYINSTALLER%" --noconfirm "build\\codewood\\codewood.spec"
if errorlevel 1 (
  echo codewood.exe build failed.
  exit /b 1
)

rem 2) codewood-gui.exe is a tiny windowed launcher. It bundles only the
rem standard library (no pywebview / prompt_toolkit / etc.) and simply starts
rem "codewood app" with no console window, so a double-click opens the GUI
rem without flashing a terminal window. It is emitted INTO the codewood
rem one-dir folder so it sits next to codewood.exe (single shippable folder).
"%PYINSTALLER%" --noconfirm --distpath "dist\\codewood" "build\\codewood-gui\\codewood-gui.spec"
if errorlevel 1 (
  echo codewood-gui.exe build failed.
  exit /b 1
)

rem ---- Remove Mark of the Web from built executables (motw can cause
rem ---- "untrusted mount point" errors when accessing junctions/symlinks) ----
echo Removing Mark of the Web from executables...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem dist\codewood\*.exe | Unblock-File -ErrorAction SilentlyContinue"

echo PyInstaller build completed. The shippable folder is "dist\codewood".
echo   codewood\codewood.exe       - terminal UI (default) and "codewood app" for the GUI
echo   codewood\codewood-gui.exe   - double-click to open the GUI without a console window

rem ---- Resolve the application version so the artifact filenames carry the
rem ---- version + platform info (e.g. CodeWood-0.0.1-windows-x64-...).
rem ---- Python writes the version to a temp file which is then read with
rem ---- "set /p". This avoids the cmd quote/';'-splitting pitfalls of capturing
rem ---- "python -c ..." output through a for /f back-quoted command (which had
rem ---- silently left APP_VERSION undefined and fell back to 0.0.0).
set VERSION_FILE=%TEMP%\codewood_version_%RANDOM%.txt
"%VENV_PYTHON%" -c "import sys, pathlib; sys.path.insert(0, 'cli'); from config.app_info import get_app_version; pathlib.Path(sys.argv[1]).write_text(get_app_version())" "%VERSION_FILE%"
set APP_VERSION=
if exist "%VERSION_FILE%" set /p APP_VERSION=<"%VERSION_FILE%"
del /f /q "%VERSION_FILE%" >nul 2>nul
if not defined APP_VERSION set APP_VERSION=0.0.0
set PLATFORM_TAG=windows-x64
echo Packaging artifacts for version %APP_VERSION% (%PLATFORM_TAG%).

rem ---- 3) Portable zip package. A self-contained, no-install bundle the user
rem ---- can unzip and run directly. Built with PowerShell's Compress-Archive so
rem ---- no extra tooling is required.
set PORTABLE_ZIP=dist\CodeWood-%APP_VERSION%-%PLATFORM_TAG%-portable.zip
if exist "%PORTABLE_ZIP%" del /f /q "%PORTABLE_ZIP%"
echo Creating portable zip "%PORTABLE_ZIP%"...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'dist\codewood\*' -DestinationPath '%PORTABLE_ZIP%' -Force"
if errorlevel 1 (
  echo Portable zip creation failed.
  exit /b 1
)

rem ---- 4) EXE installer via Inno Setup. The installer lets the user pick a
rem ---- per-user or all-users install, creates TUI + GUI shortcuts, and offers
rem ---- to register the install dir on PATH. ISCC.exe must be on PATH or at the
rem ---- default Inno Setup 6 install location.
set ISCC=ISCC.exe
where ISCC.exe >nul 2>nul
if errorlevel 1 (
  if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" (
    set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
  ) else if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" (
    set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
  ) else if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" (
    set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
  ) else (
    echo WARNING: Inno Setup compiler ^(ISCC.exe^) not found. Skipping EXE installer.
    echo          Install Inno Setup 6 from https://jrsoftware.org/isinfo.php to enable it.
    echo          The portable zip "%PORTABLE_ZIP%" was still produced.
    goto pack_done
  )
)
echo Building EXE installer with "%ISCC%"...
"%ISCC%" /DAppVersion=%APP_VERSION% /DSourceDir="..\dist\codewood" /DOutputDir="..\dist" "build\installer.iss"
if errorlevel 1 (
  echo EXE installer build failed.
  exit /b 1
)

:pack_done
echo.
echo Packaging completed. Artifacts in "dist\":
echo   CodeWood-%APP_VERSION%-%PLATFORM_TAG%-setup.exe       - EXE installer (per-user/all-users, shortcuts, PATH)
echo   CodeWood-%APP_VERSION%-%PLATFORM_TAG%-portable.zip    - portable, no-install bundle
