#!/usr/bin/env sh

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
ENTRY="$ROOT_DIR/src/main.py"
VENV_DIR="$ROOT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
REQ_FILE="$ROOT_DIR/requirements.txt"
PY_BOOTSTRAP=""

if command -v python3 >/dev/null 2>&1; then
    PY_BOOTSTRAP="python3"
elif command -v python >/dev/null 2>&1; then
    PY_BOOTSTRAP="python"
else
    echo "Python executable not found. Please install Python or add it to PATH."
    exit 127
fi

if [ -x "$VENV_PYTHON" ]; then
    exec "$VENV_PYTHON" "$ENTRY" "$@"
fi

echo "Virtual environment not found. Creating \"$VENV_DIR\"..."
"$PY_BOOTSTRAP" -m venv "$VENV_DIR"
if [ $? -ne 0 ]; then
    echo "Failed to create virtual environment."
    exit 1
fi

if [ ! -f "$REQ_FILE" ]; then
    echo "Requirements file not found: \"$REQ_FILE\""
    exit 1
fi

echo "Installing dependencies from \"$REQ_FILE\"..."
"$VENV_PYTHON" -m pip install -r "$REQ_FILE"
if [ $? -ne 0 ]; then
    echo "Failed to install dependencies."
    exit 1
fi

exec "$VENV_PYTHON" "$ENTRY" "$@"
