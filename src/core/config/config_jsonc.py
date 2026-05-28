"""Utilities for reading/writing Smart Shell JSONC config files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

CONFIG_JSONC_FILENAME = "config.jsonc"


def _strip_jsonc_comments(text: str) -> str:
    """Remove // and /* */ comments while preserving string contents."""
    s = str(text or "")
    out = []
    in_string = False
    in_line_comment = False
    in_block_comment = False
    quote_char = ""
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        nxt = s[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                out.append(ch)
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
            else:
                i += 1
            continue
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(s[i + 1])
                i += 2
                continue
            if ch == quote_char:
                in_string = False
                quote_char = ""
            i += 1
            continue
        if ch in ('"', "'"):
            in_string = True
            quote_char = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def load_config_jsonc(path: Path) -> Dict[str, Any]:
    """Load config from JSONC object text."""
    cfg_path = Path(path)
    text = cfg_path.read_text(encoding="utf-8")
    stripped = _strip_jsonc_comments(text).strip()
    if not stripped:
        return {}

    obj = json.loads(stripped)
    if not isinstance(obj, dict):
        raise ValueError("Invalid config.jsonc content: root must be a JSON object")
    return obj


def save_config_jsonc(path: Path, data: Dict[str, Any]) -> None:
    """Persist config data as pretty JSON text in .jsonc path."""
    cfg_path = Path(path)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    payload = data if isinstance(data, dict) else {}
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=2))
        f.write("\n")
