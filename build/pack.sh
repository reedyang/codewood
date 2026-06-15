#!/usr/bin/env bash
# Build executable with PyInstaller for non-Windows platforms (Linux/macOS).
# Mirrors build/pack.bat. The Python virtual environment lives in <root>/.env
# Run it from anywhere; the script resolves the project root itself:
#   bash build/pack.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

ENTRY_SCRIPT="src/main.py"
VENV_DIR=".venv"

# Locate the virtualenv site-packages (.venv/lib/pythonX.Y/site-packages).
VENV_PATH="$(ls -d "$VENV_DIR"/lib/python*/site-packages 2>/dev/null | head -n 1 || true)"
if [ -z "$VENV_PATH" ]; then
  echo "Error: site-packages not found under $VENV_DIR/lib/python*/." >&2
  echo "Create the virtual environment first, e.g. python3 -m venv $VENV_DIR" >&2
  exit 1
fi

# Prefer the venv's PyInstaller when available, otherwise fall back to PATH.
PYINSTALLER="$VENV_DIR/bin/pyinstaller"
[ -x "$PYINSTALLER" ] || PYINSTALLER="pyinstaller"

# Source paths are relative to --specpath (build/codewood), matching pack.bat.
# Unix uses ':' as the --add-data separator instead of ';'.
# Note: ripgrep (rg) is NOT bundled on non-Windows; it is expected to be
# installed system-wide (e.g. via the package manager) and found on PATH.
ARGS=(
  --onefile --name codewood
  --add-data "../../skills:skills"
  --add-data "../../src:src"
  --paths "../../$VENV_PATH"
  --specpath "build/codewood"
)

# App icon: macOS uses .icns; Linux ignores it, so only pass when present.
if [ "$(uname -s)" = "Darwin" ] && [ -f "build/codewood.icns" ]; then
  ARGS=(--icon "../../build/codewood.icns" "${ARGS[@]}")
fi

"$PYINSTALLER" "${ARGS[@]}" "$ENTRY_SCRIPT"

echo "Build completed. Executable is in the \"dist\" folder."
