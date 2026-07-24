#!/usr/bin/env sh

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
ENTRY="$ROOT_DIR/cli/main.py"
APP_INFO="$ROOT_DIR/cli/config/app_info.py"
VENV_DIR="$ROOT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
REQ_FILE="$ROOT_DIR/requirements.txt"
PY_BOOTSTRAP=""

set_title_from_app_info() {
    app_name="$("$VENV_PYTHON" -c "import runpy;d=runpy.run_path(r'$APP_INFO');f=d.get('get_app_name');print(f() if callable(f) else '')" 2>/dev/null)"
    if [ -n "$app_name" ]; then
        printf '\033]0;%s\007' "$app_name"
    fi
}

run_main() {
    exec "$VENV_PYTHON" "$ENTRY" --executable-name "$(basename -- "$0")" "$@"
}

# Inlined former install.sh: install dependencies into the venv.
install_dependencies() {
    echo "Installing dependencies from \"$REQ_FILE\"..."
    "$VENV_PYTHON" -m pip install -r "$REQ_FILE" || { echo "Failed to install dependencies."; exit 1; }
    echo "Dependencies installed successfully."
}

if command -v python3 >/dev/null 2>&1; then
    PY_BOOTSTRAP="python3"
elif command -v python >/dev/null 2>&1; then
    PY_BOOTSTRAP="python"
else
    echo "Python executable not found. Please install Python or add it to PATH."
    exit 127
fi

download_embedding_model() {
    DOWNLOAD_SCRIPT="$SCRIPT_DIR/download_embedding_model.py"
    if [ ! -f "$DOWNLOAD_SCRIPT" ]; then
        return 0
    fi
    "$VENV_PYTHON" -c "import sys; sys.path.insert(0, r'$ROOT_DIR'); from cli.tools.embedding import _resolve_model_path, _EMBEDDING_MODEL_NAME; p=_resolve_model_path(_EMBEDDING_MODEL_NAME); exit(0 if p else 1)" >/dev/null 2>&1
    if [ $? -eq 0 ]; then
        return 0
    fi
    echo "Embedding model not found. Downloading..."
    "$VENV_PYTHON" "$DOWNLOAD_SCRIPT"
}


if [ ! -x "$VENV_PYTHON" ]; then
    echo "Virtual environment not found. Creating \"$VENV_DIR\"..."
    "$PY_BOOTSTRAP" -m venv "$VENV_DIR"
    if [ $? -ne 0 ]; then
        echo "Failed to create virtual environment."
        exit 1
    fi
fi

if [ ! -f "$REQ_FILE" ]; then
    echo "Requirements file not found: \"$REQ_FILE\""
    exit 1
fi

# Check for missing dependencies every time: the venv may have been created on a
# different platform (e.g. Windows) where some packages were not installed.
MISSING=$("$VENV_PYTHON" -c "
import subprocess, sys, re
r = subprocess.run([sys.executable, '-m', 'pip', 'list', '--format=freeze'], capture_output=True, text=True)
installed = {line.split('==')[0].lower() for line in r.stdout.strip().splitlines() if '==' in line}
with open('$REQ_FILE') as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name = re.split(r'[>=<!~]', line)[0].strip().lower()
        if name and name not in installed:
            print(name)
            sys.exit(1)
" 2>/dev/null)
if [ -n "$MISSING" ]; then
    echo "Missing dependencies detected."
    install_dependencies
fi

set_title_from_app_info
download_embedding_model
run_main "$@"