#!/usr/bin/env bash
# Shared packaging steps for Linux/macOS. Sourced by _pack-linux.sh / _pack-mac.sh.
# Expects PACK_PLATFORM=linux|macos.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  echo "_pack-common.sh is sourced by _pack-linux.sh / _pack-mac.sh, not run directly." >&2
  exit 1
fi

: "${PACK_PLATFORM:?PACK_PLATFORM must be set to linux or macos before sourcing _pack-common.sh}"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

case "$PACK_PLATFORM:$(uname -s)" in
  macos:Darwin|linux:Linux) ;;
  *) echo "pack target '$PACK_PLATFORM' does not match the current OS ($(uname -s))." >&2; exit 1 ;;
esac

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

# ---- Ensure ripgrep (rg) and rg-version.txt exist so they can be bundled.
# ---- If either is missing, download the latest ripgrep release into bin/
# ---- (which also writes bin/rg-version.txt).
echo "Checking rg and rg-version.txt for bundling..."
if [ ! -f "bin/rg" ] || [ ! -f "bin/rg-version.txt" ]; then
  echo "rg or rg-version.txt not found in bin/. Downloading ripgrep..."
  if ! "$VENV_PYTHON" -c "import sys; sys.path.insert(0, '.'); from pathlib import Path; from cli.config.rg_downloader import ensure_rg_sync; sys.exit(0 if ensure_rg_sync(Path('bin')) else 1)"; then
    echo "Failed to download rg. Aborting packaging." >&2
    exit 1
  fi
fi
echo "rg and rg-version.txt ready for bundling."

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
# Note: ripgrep (bin/rg + bin/rg-version.txt) is downloaded above if missing
# and bundled into the package via the --add-data flags below.
#
# One-dir is used (instead of one-file) so each process runs directly without
# an extra self-extracting bootloader process: the GUI then uses two processes
# (app + serve backend) and the terminal UI uses one. Output is a single
# shippable folder dist/codewood/ (codewood, codewood-gui, _internal/).
#
# 1) codewood carries ALL terminal-UI and GUI logic (frontend bundle +
#    pywebview host included). Default = terminal UI; "codewood app" = GUI.

# ---- Download the embedding model before building so PyInstaller can bundle it ----
echo "Checking embedding model for offline bundle..."
MODEL_NAME="all-MiniLM-L6-v2"
if [ ! -f "models/$MODEL_NAME/config.json" ]; then
    echo "Downloading embedding model..."
    if ! "$VENV_PYTHON" -c "from sentence_transformers import SentenceTransformer; m = SentenceTransformer('$MODEL_NAME', device='cpu'); m.save('models/$MODEL_NAME')"; then
        echo "WARNING: Could not download embedding model. The package will require online HF access."
    fi
else
    echo "Embedding model already cached in models/$MODEL_NAME."
fi

DATA_ARGS=(
  --add-data "../../skills:skills"
  --add-data "../../additional-subagents:additional-subagents"
  --add-data "../../cli:cli"
  --add-data "../../desktop/frontend/dist:frontend"
  --add-data "../../desktop/host:host"
  --add-data "../../models:models"
  --add-data "../../bin/rg:bin"
  --add-data "../../bin/rg-version.txt:bin"
  # pathex is resolved relative to the working dir (project root), unlike
  # --add-data sources which are relative to --specpath; so no "../../".
  --paths "$VENV_PATH"
  --collect-all webview
  # tiktoken loads encodings (e.g. cl100k_base) lazily via the tiktoken_ext
  # namespace plugin, which PyInstaller's static analysis cannot see; without
  # these the packaged app silently drops to the heuristic token counter
  # (openai/tiktoken#43, #469 — still no built-in hook).
  --collect-all tiktoken
  --hidden-import tiktoken_ext
  --hidden-import tiktoken_ext.openai_public
)

# App icon: macOS uses .icns; Linux ignores it, so only pass when present.
ICON_ARGS=()
if [ "$PACK_PLATFORM" = "macos" ] && [ -f "build/app_icon.icns" ]; then
  ICON_ARGS=(--icon "../../build/app_icon.icns")
fi

# 1) codewood (console, one-dir): carries ALL terminal-UI + serve + `app`
#    logic. dist/codewood/codewood is the CLI the user runs in the terminal.
"$PYINSTALLER" "${ICON_ARGS[@]}" --onedir --noconfirm --name codewood \
  --specpath "build/codewood" "${DATA_ARGS[@]}" "$ENTRY_SCRIPT"

# 2) codewood-gui (windowed): the desktop GUI. On macOS --windowed produces a
#    .app bundle (Contents/MacOS + Frameworks/Python + Info.plist). On Linux it
#    produces a windowed onedir that also opens the GUI.
"$PYINSTALLER" "${ICON_ARGS[@]}" --windowed --noconfirm --name codewood-gui \
  --specpath "build/codewood-gui" "${DATA_ARGS[@]}" "$ENTRY_SCRIPT"

echo "PyInstaller build completed."
echo "  dist/codewood/codewood     - terminal UI (TUI) + serve backend (CLI)"
if [ "$PACK_PLATFORM" = "macos" ]; then
  echo "  dist/codewood-gui.app      - the desktop GUI (.app bundle)"
else
  echo "  dist/codewood-gui/codewood-gui - desktop GUI (windowed onedir)"
fi

# ---- Resolve the application version + platform tag so the portable archive
# ---- filename carries version + platform info (e.g.
# ---- CodeWood-0.0.1-linux-x86_64-portable.tar.gz).
APP_VERSION="$("$VENV_PYTHON" -c 'import sys; sys.path.insert(0, "cli"); from config.app_info import get_app_version; print(get_app_version())' 2>/dev/null || echo "0.0.0")"
[ -n "$APP_VERSION" ] || APP_VERSION="0.0.0"

OS_TAG="$PACK_PLATFORM"
ARCH_TAG="$(uname -m)"
# On Apple Silicon the shell may be running under Rosetta, where ``uname -m``
# reports x86_64 even though the build is truly arm64. Use the hardware arch so
# the artifact tag (and the PyInstaller target) is correct.
if [ "$PACK_PLATFORM" = "macos" ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
  ARCH_TAG="arm64"
fi
PLATFORM_TAG="${OS_TAG}-${ARCH_TAG}"
echo "Packaging artifacts for version $APP_VERSION ($PLATFORM_TAG)."

# ---- Portable archive: a self-contained, no-install bundle the user can
# ---- extract and run directly. macOS ships zip; both platforms ship tar, so
# ---- we emit a .tar.gz which preserves the executable bit on codewood/.
PORTABLE_ARCHIVE="dist/CodeWood-${APP_VERSION}-${PLATFORM_TAG}-portable.tar.gz"
rm -f "$PORTABLE_ARCHIVE"
echo "Creating portable archive \"$PORTABLE_ARCHIVE\"..."
tar -czf "$PORTABLE_ARCHIVE" -C dist codewood

# Track produced installer artifacts for the final summary.
INSTALLER_NOTES=()

pack_print_summary() {
  echo ""
  echo "Packaging completed. Artifacts in \"dist/\":"
  echo "  CodeWood-${APP_VERSION}-${PLATFORM_TAG}-portable.tar.gz  - portable, no-install bundle"
  local note
  for note in "${INSTALLER_NOTES[@]}"; do
    echo "$note"
  done
  echo "Note: the Windows .exe installer is produced by build/pack.bat on Windows."
}
