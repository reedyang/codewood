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

# Build the desktop GUI frontend bundle first.
( cd desktop/frontend && npm install && npm run build )

# Source paths are relative to --specpath (build/codewood), matching pack.bat.
# Unix uses ':' as the --add-data separator instead of ';'.
# Note: ripgrep (rg) is NOT bundled on non-Windows; it is expected to be
# installed system-wide (e.g. via the package manager) and found on PATH.
#
# One-dir is used (instead of one-file) so each process runs directly without
# an extra self-extracting bootloader process: the GUI then uses two processes
# (app + serve backend) and the terminal UI uses one. Output is a single
# shippable folder dist/codewood/ (codewood, codewood-gui, _internal/).
#
# 1) codewood carries ALL terminal-UI and GUI logic (frontend bundle +
#    pywebview host included). Default = terminal UI; "codewood app" = GUI.
ARGS=(
  --onedir --name codewood
  --add-data "../../skills:skills"
  --add-data "../../src:src"
  --add-data "../../desktop/frontend/dist:frontend"
  --add-data "../../desktop/host:host"
  # pathex is resolved relative to the working dir (project root), unlike
  # --add-data sources which are relative to --specpath; so no "../../".
  --paths "$VENV_PATH"
  --collect-all webview
  --specpath "build/codewood"
)

# App icon: macOS uses .icns; Linux ignores it, so only pass when present.
ICON_ARGS=()
if [ "$(uname -s)" = "Darwin" ] && [ -f "build/app_icon.icns" ]; then
  ICON_ARGS=(--icon "../../build/app_icon.icns")
fi

"$PYINSTALLER" "${ICON_ARGS[@]}" "${ARGS[@]}" "$ENTRY_SCRIPT"

# 2) codewood-gui is a tiny launcher that bundles only the standard library
#    (no pywebview / prompt_toolkit / etc.) and simply starts "codewood app"
#    in a detached, window-free process. Mirrors codewood-gui.exe on Windows.
#    It is emitted INTO the codewood one-dir folder so it sits next to the
#    codewood executable (single shippable folder).
"$PYINSTALLER" "${ICON_ARGS[@]}" \
  --onefile --windowed --name codewood-gui \
  --distpath "dist/codewood" \
  --specpath "build/codewood-gui" \
  "desktop/host/launcher.py"

echo "Build completed. The shippable folder is \"dist/codewood\"."
echo "  codewood/codewood      - terminal UI (default) and \"codewood app\" for the GUI"
echo "  codewood/codewood-gui  - launch the GUI without a console window"
