@echo off
rem Build executable with PyInstaller for the current project
rem Adjust as needed

set ENTRY_SCRIPT=src\main.py

rem Include required resources: rg.exe, skills, and src resources
rem Using multiple --add-data flags (Windows uses ';' as separator)
rem Include virtual environment packages from .venv-windows
rem PyInstaller will search this path for modules
set VENV_PATH=.venv-windows\Lib\site-packages

pyinstaller --onefile --name codewood ^
  --add-data "../../vendors/rg.exe;bin" ^
  --add-data "../../skills;skills" ^
  --add-data "../../src;src" ^
  --paths "../../%VENV_PATH%" ^
  --specpath "build\\codewood" ^
  "%ENTRY_SCRIPT%"

echo Build completed. Executable is in the "dist" folder.
pause
