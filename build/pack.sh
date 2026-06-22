#!/usr/bin/env bash
# Build executable with PyInstaller for non-Windows platforms (Linux/macOS).
# Mirrors build/pack.bat. It prepares the Python virtual environment in
# <root>/.venv (creating it and installing requirements.txt if needed) before
# packaging. Run it from anywhere; the script resolves the project root itself:
#   bash build/pack.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

ENTRY_SCRIPT="cli/main.py"
VENV_DIR=".venv"
VENV_PYTHON="$VENV_DIR/bin/python"
REQ_FILE="requirements.txt"

# ---- Prepare the Python virtual environment so all build/runtime
# ---- dependencies (PyInstaller, pywebview, ...) are ready before packaging.
# ---- Mirrors bin/codewood.sh: create .venv if missing, then install
# ---- requirements.txt into it.
if [ ! -x "$VENV_PYTHON" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PY_BOOTSTRAP="python3"
  elif command -v python >/dev/null 2>&1; then
    PY_BOOTSTRAP="python"
  else
    echo "Python executable not found. Please install Python or add it to PATH." >&2
    exit 127
  fi
  echo "Virtual environment not found. Creating \"$VENV_DIR\"..."
  "$PY_BOOTSTRAP" -m venv "$VENV_DIR" || { echo "Failed to create virtual environment." >&2; exit 1; }
fi

if [ ! -f "$REQ_FILE" ]; then
  echo "Requirements file not found: \"$REQ_FILE\"" >&2
  exit 1
fi
echo "Installing/updating dependencies from \"$REQ_FILE\"..."
"$VENV_PYTHON" -m pip install -r "$REQ_FILE" || { echo "Failed to install dependencies." >&2; exit 1; }

# Locate the virtualenv site-packages (.venv/lib/pythonX.Y/site-packages).
VENV_PATH="$(ls -d "$VENV_DIR"/lib/python*/site-packages 2>/dev/null | head -n 1 || true)"
if [ -z "$VENV_PATH" ]; then
  echo "Error: site-packages not found under $VENV_DIR/lib/python*/." >&2
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
  --onedir --noconfirm --name codewood
  --add-data "../../skills:skills"
  --add-data "../../cli:cli"
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
  --onefile --noconfirm --windowed --name codewood-gui \
  --distpath "dist/codewood" \
  --specpath "build/codewood-gui" \
  "desktop/host/launcher.py"

echo "PyInstaller build completed. The shippable folder is \"dist/codewood\"."
echo "  codewood/codewood      - terminal UI (default) and \"codewood app\" for the GUI"
echo "  codewood/codewood-gui  - launch the GUI without a console window"

# ---- Resolve the application version + platform tag so the portable archive
# ---- filename carries version + platform info (e.g.
# ---- CodeWood-0.0.1-linux-x86_64-portable.tar.gz).
APP_VERSION="$("$VENV_PYTHON" -c 'import sys; sys.path.insert(0, "cli"); from config.app_info import get_app_version; print(get_app_version())' 2>/dev/null || echo "0.0.0")"
[ -n "$APP_VERSION" ] || APP_VERSION="0.0.0"

case "$(uname -s)" in
  Darwin) OS_TAG="macos" ;;
  Linux)  OS_TAG="linux" ;;
  *)      OS_TAG="$(uname -s | tr '[:upper:]' '[:lower:]')" ;;
esac
ARCH_TAG="$(uname -m)"
PLATFORM_TAG="${OS_TAG}-${ARCH_TAG}"
echo "Packaging artifacts for version $APP_VERSION ($PLATFORM_TAG)."

# ---- Portable archive: a self-contained, no-install bundle the user can
# ---- extract and run directly. macOS ships zip; both platforms ship tar, so
# ---- we emit a .tar.gz which preserves the executable bit on codewood/.
PORTABLE_ARCHIVE="dist/CodeWood-${APP_VERSION}-${PLATFORM_TAG}-portable.tar.gz"
rm -f "$PORTABLE_ARCHIVE"
echo "Creating portable archive \"$PORTABLE_ARCHIVE\"..."
tar -czf "$PORTABLE_ARCHIVE" -C dist codewood

echo ""
echo "Packaging completed. Artifacts in \"dist/\":"
echo "  CodeWood-${APP_VERSION}-${PLATFORM_TAG}-portable.tar.gz  - portable, no-install bundle"
echo "Note: the Windows .exe installer is produced by build/pack.bat on Windows."
