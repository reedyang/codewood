"""GUI-only settings store (``config-gui.jsonl``).

The desktop GUI keeps presentation-only preferences (theme, pinned/archived
chats and workspaces) that must never affect the TUI or the model runtime. They
are stored in a dedicated file next to the main config so they stay isolated
from ``config.jsonc`` and survive a webview that clears localStorage.

The file holds a single JSON object. There is intentionally no migration from
the previous in-``config.jsonc`` location: GUI prefs are cheap to re-set and the
old fields are simply ignored.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict

GUI_CONFIG_FILENAME = "config-gui.jsonl"

_ALLOWED_THEMES = ("light", "dark", "system")
_MAX_IDS = 2000
_MAX_ID_LEN = 512


def gui_config_path(config_dir: Path) -> Path:
    return Path(config_dir) / GUI_CONFIG_FILENAME


def load_gui_config(config_dir: Path) -> Dict[str, Any]:
    """Return the GUI config object, or an empty dict if missing/invalid."""
    path = gui_config_path(config_dir)
    try:
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_gui_config(config_dir: Path, data: Dict[str, Any]) -> None:
    """Atomically write the GUI config object."""
    path = gui_config_path(config_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    # Write to a temp file in the same directory then replace, so a crash mid
    # write can't truncate the existing config.
    fd, tmp = tempfile.mkstemp(prefix=".config-gui-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, str(path))
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise


def normalize_theme(value: Any) -> str:
    theme = str(value or "").strip().lower()
    return theme if theme in _ALLOWED_THEMES else ""


def _normalize_ids(value: Any) -> list:
    out = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item:
                out.append(item[:_MAX_ID_LEN])
    return out[:_MAX_IDS]


def normalize_ui_prefs(prefs: Any) -> Dict[str, Any]:
    src = prefs if isinstance(prefs, dict) else {}
    return {
        "pinnedWorkspaceIds": _normalize_ids(src.get("pinnedWorkspaceIds")),
        "pinnedChatIds": _normalize_ids(src.get("pinnedChatIds")),
        "archivedChatIds": _normalize_ids(src.get("archivedChatIds")),
    }
