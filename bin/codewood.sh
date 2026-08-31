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

# Bootstrap with Python 3.9-3.13 (3.14+ may have compatibility issues).
PY_BOOTSTRAP=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$cand" >/dev/null 2>&1 \
        && "$cand" -c 'import sys; sys.exit(0 if sys.version_info < (3,14) else 1)' >/dev/null 2>&1; then
        PY_BOOTSTRAP="$cand"
        break
    fi
done
if [ -z "$PY_BOOTSTRAP" ]; then
    echo "No compatible Python found. Python 3.14+ is not supported yet."
    echo "Please install Python 3.13 or earlier."
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


if [ -x "$VENV_PYTHON" ] \
    && ! "$VENV_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info < (3,14) else 1)' >/dev/null 2>&1; then
    echo "Existing \"$VENV_DIR\" was created with an incompatible Python. Recreating..."
    rm -rf "$VENV_DIR"
fi

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

# Check for missing dependencies every time: the venv is platform-specific, so
# a freshly created venv has no packages installed yet.
# Use ``import numpy`` as a canary: the first run on a new venv will trigger a
# full ``pip install -r requirements.txt`` once.
if ! "$VENV_PYTHON" -c "import numpy" 2>/dev/null; then
    echo "Missing dependencies detected."
    install_dependencies
fi

set_title_from_app_info
download_embedding_model
run_main "$@"