#!/usr/bin/env bash
# Dispatch packaging to the implementation for the current operating system.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

case "$(uname -s)" in
  Darwin) target="$SCRIPT_DIR/_pack-mac.sh" ;;
  Linux)  target="$SCRIPT_DIR/_pack-linux.sh" ;;
  *)
    echo "Unsupported platform: $(uname -s). Use macOS or Linux." >&2
    exit 1
    ;;
esac

if [ ! -f "$target" ]; then
  echo "Packaging script not found: $target" >&2
  exit 1
fi
exec bash "$target" "$@"
