#!/usr/bin/env bash

# Resolve script directory and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$ROOT_DIR/.venv"
REQ_FILE="$ROOT_DIR/requirements.txt"

# Ensure virtual environment exists
if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "Virtual environment not found. Creating $VENV_DIR..."
    python3 -m venv "$VENV_DIR" || { echo "Failed to create virtual environment."; exit 1; }
fi

# Activate virtual environment
source "$VENV_DIR/bin/activate"

# Install required Python packages
pip install -r "$REQ_FILE" || { echo "Failed to install dependencies."; exit 1; }

echo "Dependencies installed successfully."
