#!/usr/bin/env sh

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
ENTRY="$ROOT_DIR/src/main.py"
APP_INFO="$ROOT_DIR/src/config/app_info.py"
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

if command -v python3 >/dev/null 2>&1; then
    PY_BOOTSTRAP="python3"
elif command -v python >/dev/null 2>&1; then
    PY_BOOTSTRAP="python"
else
    echo "Python executable not found. Please install Python or add it to PATH."
    exit 127
fi

if [ -x "$VENV_PYTHON" ]; then
    set_title_from_app_info
    run_main "$@"
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

MISSING=$(pip install --dry-run -r "requirements.txt" 2>&1 | \
    grep -v -E "Requirement already satisfied|^\[notice\]|--upgrade pip")
if echo "$MISSING" | grep -q "Could not find\|No matching distribution"; then
    echo "Missing dependencies detected. Running install.sh..."
    "$SCRIPT_DIR/install.sh" || { echo "install.sh failed."; exit 1; }
fi

set_title_from_app_info
run_main "$@"
