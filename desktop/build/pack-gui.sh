#!/usr/bin/env bash
# Build the Code Wood desktop GUI (codewoodw) with PyInstaller on
# Linux/macOS. Mirrors desktop/build/pack-gui.bat. The backend codewood
# executable is NOT bundled; at runtime the GUI launches the sibling
# codewood executable in the same folder.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

VENV_DIR=".venv"
VENV_PATH="$(ls -d "$VENV_DIR"/lib/python*/site-packages 2>/dev/null | head -n 1 || true)"
if [ -z "$VENV_PATH" ]; then
  echo "Error: site-packages not found under $VENV_DIR/lib/python*/." >&2
  echo "Create the virtual environment first, e.g. python3 -m venv $VENV_DIR" >&2
  exit 1
fi

# 1. Build the frontend bundle.
( cd desktop/frontend && npm install && npm run build )

# 2. Package the host.
PYINSTALLER="$VENV_DIR/bin/pyinstaller"
[ -x "$PYINSTALLER" ] || PYINSTALLER="pyinstaller"

# Source paths after --specpath are resolved relative to build/codewoodw.
ARGS=(
  --onefile --name codewoodw --windowed
  --add-data "../../desktop/frontend/dist:frontend"
  --paths "../../$VENV_PATH"
  --paths "../../desktop/host"
  --collect-all webview
  --specpath "build/codewoodw"
)

if [ "$(uname -s)" = "Darwin" ] && [ -f "build/codewood.icns" ]; then
  ARGS=(--icon "../../build/codewood.icns" "${ARGS[@]}")
fi

"$PYINSTALLER" "${ARGS[@]}" "desktop/host/codewoodw.py"

echo "Build completed. Executable is in the \"dist\" folder."
echo "Place codewoodw next to the codewood executable before running."
