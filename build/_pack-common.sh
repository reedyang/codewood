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

# Linux PyPI torch wheels pull CUDA + nvidia-* runtimes (several GB). The
# app only runs embeddings on CPU (cli/tools/embedding.py), so packaging
# must ship the CPU wheel or the Linux artifact dwarfs Windows/macOS.
#
# The CPU index publishes local versions (2.8.0+cpu), not bare 2.8.0.
# `pip install torch==2.8.0 --index-url .../whl/cpu` fails with
# "from versions: none" because ==2.8.0 does not match 2.8.0+cpu.
TORCH_SPEC="$(grep -E '^[[:space:]]*torch([=<>!~]|$)' "$REQ_FILE" | head -n 1 | sed 's/[[:space:]]*#.*//' | tr -d '[:space:]')"
[ -n "$TORCH_SPEC" ] || TORCH_SPEC="torch==2.8.0"
case "$TORCH_SPEC" in
  *"+cpu") TORCH_CPU_SPEC="$TORCH_SPEC" ;;
  torch==*) TORCH_CPU_SPEC="${TORCH_SPEC}+cpu" ;;
  *) TORCH_CPU_SPEC="torch==2.8.0+cpu" ;;
esac

install_linux_cpu_torch() {
  # Corporate HTTPS inspection (self-signed cert in the chain) breaks pip's
  # default SSL verify against download.pytorch.org. --trusted-host is scoped
  # to the PyTorch CPU CDN only; PyPI stays verified. Prefer PIP_CERT /
  # SSL_CERT_FILE pointing at the corporate CA when available.
  echo "Installing CPU-only $TORCH_CPU_SPEC (Linux CUDA wheels are several GB)..."
  "$VENV_PYTHON" -m pip install --force-reinstall "$TORCH_CPU_SPEC" \
    --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple \
    --trusted-host download.pytorch.org \
    --trusted-host download-r2.pytorch.org \
    || { echo "Failed to install CPU-only PyTorch. Aborting packaging." >&2
         echo "download.pytorch.org failed SSL verify (self-signed cert in chain)." >&2
         echo "Set PIP_CERT or SSL_CERT_FILE to your corporate CA bundle and retry." >&2
         exit 1; }
}

uninstall_linux_cuda_leftovers() {
  local leftover
  leftover="$("$VENV_PYTHON" -m pip freeze | sed -n 's/==.*//p' | grep -iE '^(nvidia-|cuda-|triton$)' || true)"
  if [ -n "$leftover" ]; then
    echo "Uninstalling leftover CUDA/NVIDIA packages so PyInstaller cannot bundle them:"
    echo "$leftover"
    # shellcheck disable=SC2086
    "$VENV_PYTHON" -m pip uninstall -y $leftover || true
  fi
}

if [ "$PACK_PLATFORM" = "linux" ]; then
  # CPU torch first, then the rest of requirements.txt without the torch pin
  # so pip does not replace 2.8.0+cpu with the PyPI CUDA wheel (torch==2.8.0).
  install_linux_cpu_torch
  REQ_NO_TORCH="$(mktemp)"
  grep -v -E '^[[:space:]]*torch([=<>!~]|$)' "$REQ_FILE" > "$REQ_NO_TORCH"
  echo "Installing/updating dependencies from \"$REQ_FILE\" (torch excluded; using $TORCH_CPU_SPEC)..."
  if ! "$VENV_PYTHON" -m pip install -r "$REQ_NO_TORCH"; then
    rm -f "$REQ_NO_TORCH"
    echo "Failed to install dependencies." >&2
    exit 1
  fi
  rm -f "$REQ_NO_TORCH"
  uninstall_linux_cuda_leftovers
  "$VENV_PYTHON" -c "import torch; cuda=getattr(torch.version,'cuda',None); v=torch.__version__; raise SystemExit('CUDA torch still installed: %s (cuda=%s). CPU wheel required for Linux packaging.' % (v, cuda) if cuda else 0)" \
    || { echo "CPU-only PyTorch check failed. Aborting packaging." >&2; exit 1; }
  echo "Using CPU-only PyTorch: $("$VENV_PYTHON" -c 'import torch; print(torch.__version__)')"
else
  echo "Installing/updating dependencies from \"$REQ_FILE\"..."
  "$VENV_PYTHON" -m pip install -r "$REQ_FILE" || { echo "Failed to install dependencies." >&2; exit 1; }
fi

# pkg_resources (pulled in by setuptools) imports jaraco at frozen startup.
# Ensure the standalone package is present so PyInstaller can collect it.
echo "Ensuring jaraco.text is available for the PyInstaller bundle..."
if ! "$VENV_PYTHON" -c "import jaraco.text" >/dev/null 2>&1; then
  "$VENV_PYTHON" -m pip install "jaraco.text>=3.7" || { echo "Failed to install jaraco.text." >&2; exit 1; }
fi

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
# shippable folder dist/codewood/ (codewood, thin codewood-gui launcher,
# _internal/). macOS additionally emits dist/codewood-gui.app for .dmg/.pkg.
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
  # setuptools>=70 loads jaraco through pkg_resources.extern. PyInstaller's
  # pyi_rth_pkgres hook imports it at process start; without these the
  # frozen binary dies with "The 'jaraco' package is required".
  --collect-all jaraco
  --collect-all setuptools
  --hidden-import pkg_resources
  --hidden-import jaraco
  --hidden-import jaraco.text
  --hidden-import jaraco.functools
  --hidden-import jaraco.context
  --hidden-import jaraco.collections
)

# App icon: macOS uses .icns; Linux ignores it, so only pass when present.
ICON_ARGS=()
if [ "$PACK_PLATFORM" = "macos" ] && [ -f "build/app_icon.icns" ]; then
  ICON_ARGS=(--icon "../../build/app_icon.icns")
fi

# 1) codewood (console, one-dir): carries ALL terminal-UI + serve + `app`
#    logic. dist/codewood/codewood is the CLI the user runs in the terminal.
#    Linux: exclude nvidia/triton so CUDA leftovers cannot enter the Analysis.
if [ "$PACK_PLATFORM" = "linux" ]; then
  "$PYINSTALLER" --onedir --noconfirm --name codewood \
    --specpath "build/codewood" "${DATA_ARGS[@]}" \
    --exclude-module nvidia --exclude-module triton \
    "$ENTRY_SCRIPT"
else
  "$PYINSTALLER" "${ICON_ARGS[@]}" --onedir --noconfirm --name codewood \
    --specpath "build/codewood" "${DATA_ARGS[@]}" "$ENTRY_SCRIPT"
fi

# 2) codewood-gui. macOS needs a full windowed .app (Dock / LaunchServices).
#    Linux matches Windows: a tiny stdlib-only launcher next to `codewood`
#    that runs `codewood app`. A second full onedir would duplicate the
#    payload and still omit the launcher from dist/codewood/.
if [ "$PACK_PLATFORM" = "macos" ]; then
  "$PYINSTALLER" "${ICON_ARGS[@]}" --windowed --noconfirm --name codewood-gui \
    --specpath "build/codewood-gui" "${DATA_ARGS[@]}" "$ENTRY_SCRIPT"
else
  # Drop a leftover full onedir from older pack.sh runs (several GB).
  rm -rf dist/codewood-gui
  "$PYINSTALLER" --onefile --windowed --noconfirm --name codewood-gui \
    --distpath "dist/codewood" --specpath "build/codewood-gui" \
    desktop/host/launcher.py
  chmod +x "dist/codewood/codewood-gui" 2>/dev/null || true
fi

# Drop CUDA/NVIDIA natives that still leaked into the Linux onedir.
prune_linux_gpu_runtime() {
  local root="$1"
  [ -d "$root" ] || return 0
  echo "Pruning CUDA/NVIDIA runtime files from $root..."
  find "$root" -type d \( -iname 'nvidia' -o -iname 'triton' \) -prune -exec rm -rf {} + 2>/dev/null || true
  find "$root" -type f \( \
      -iname '*libtorch_cuda*' -o \
      -iname '*libcudart*' -o \
      -iname '*libcublas*' -o \
      -iname '*libcudnn*' -o \
      -iname '*libcufft*' -o \
      -iname '*libcurand*' -o \
      -iname '*libcusolver*' -o \
      -iname '*libcusparse*' -o \
      -iname '*libnvrtc*' -o \
      -iname '*libnccl*' -o \
      -iname '*libnvJitLink*' -o \
      -iname '*libnvToolsExt*' \
    \) -delete 2>/dev/null || true
}
if [ "$PACK_PLATFORM" = "linux" ]; then
  prune_linux_gpu_runtime "dist/codewood"
fi

echo "PyInstaller build completed."
echo "  dist/codewood/codewood     - terminal UI (TUI) + serve backend (CLI)"
if [ "$PACK_PLATFORM" = "macos" ]; then
  echo "  dist/codewood-gui.app      - the desktop GUI (.app bundle)"
else
  echo "  dist/codewood/codewood-gui - thin windowed launcher (runs codewood app)"
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
