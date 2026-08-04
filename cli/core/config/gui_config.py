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
# Display languages the GUI is allowed to render. Kept in sync with the
# frontend i18n bundle ("en" + "zh-Hans"). Validation is strict so a typo in
# the config file cannot pivot the UI into an unsupported locale.
_ALLOWED_LANGUAGES = ("en", "zh-Hans")
_MAX_IDS = 2000
_MAX_ID_LEN = 512

# Background image: the file is always stored next to the config as
# ``bg.<ext>`` so only the extension needs persisting. The extension allowlist
# doubles as the accepted upload type guard.
BACKGROUND_IMAGE_STEM = "bg"
_ALLOWED_BACKGROUND_EXTS = ("png", "jpg", "jpeg", "webp", "gif", "bmp")
_DEFAULT_BACKGROUND_OPACITY = 85

# Embedded console options (GUI-only). Font name is free text bounded for
# safety; buffer lines (xterm scrollback) is clamped to a sane range.
_DEFAULT_CONSOLE_FONT = ""
_DEFAULT_CONSOLE_BUFFER_LINES = 1000
_CONSOLE_BUFFER_MIN = 100
_CONSOLE_BUFFER_MAX = 100000
_MAX_CONSOLE_FONT_LEN = 128


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


def normalize_gui_language(value: Any) -> str:
    """Return a supported GUI display language code or ``""``.

    The GUI language is intentionally separate from the agent's
    ``display_language`` (which drives TUI prompts, logs and model system
    prompts). Values outside the allowlist are dropped rather than coerced so
    the frontend always sees a known locale.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    # Normalize common aliases to the canonical code set.
    lower = raw.lower()
    if lower in ("zh", "zh-cn", "zh_cn", "zh-hans", "zh_hans", "chinese"):
        return "zh-Hans"
    if lower in ("en", "en-us", "en_us", "english"):
        return "en"
    return raw if raw in _ALLOWED_LANGUAGES else ""


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
        "workspaceOrder": _normalize_ids(src.get("workspaceOrder")),
    }


def normalize_console_options(value: Any) -> Dict[str, Any]:
    """Return a normalized embedded-console options object for storage.

    Shape: ``{"fontFamily": <str>, "bufferLines": <int>}``. The font family is
    free text (a CSS/xterm font name) bounded in length; an empty value means
    "use the default monospace stack". Buffer lines is clamped to a safe range.
    """
    src = value if isinstance(value, dict) else {}
    font = str(src.get("fontFamily") or "").strip()[:_MAX_CONSOLE_FONT_LEN]
    try:
        buffer_lines = int(round(float(src.get("bufferLines"))))
    except (TypeError, ValueError):
        buffer_lines = _DEFAULT_CONSOLE_BUFFER_LINES
    buffer_lines = max(_CONSOLE_BUFFER_MIN, min(_CONSOLE_BUFFER_MAX, buffer_lines))
    return {"fontFamily": font, "bufferLines": buffer_lines}


def default_console_options() -> Dict[str, Any]:
    return {"fontFamily": _DEFAULT_CONSOLE_FONT, "bufferLines": _DEFAULT_CONSOLE_BUFFER_LINES}


def normalize_background_ext(value: Any) -> str:
    """Return a supported background image extension (lowercase, no dot) or ``""``."""
    raw = str(value or "").strip().lower().lstrip(".")
    if raw == "jpeg":
        raw = "jpeg"
    return raw if raw in _ALLOWED_BACKGROUND_EXTS else ""


def normalize_background_opacity(value: Any) -> int:
    """Clamp the opacity percentage to the inclusive range [0, 100]."""
    try:
        pct = int(round(float(value)))
    except (TypeError, ValueError):
        return _DEFAULT_BACKGROUND_OPACITY
    return max(0, min(100, pct))


def background_filename_for_ext(ext: str) -> str:
    """Return the canonical background file name ``bg.<ext>`` or ``""``."""
    safe_ext = normalize_background_ext(ext)
    return f"{BACKGROUND_IMAGE_STEM}.{safe_ext}" if safe_ext else ""


def normalize_background_filename(value: Any) -> str:
    """Validate a stored background file name and return it, or ``""``.

    The file name must be exactly ``bg.<allowed-ext>``. Any other value
    (path components, unexpected stem, unsupported extension) is rejected so a
    tampered config can't point the loader at an arbitrary file.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    # Reject anything that looks like a path; only a bare file name is allowed.
    if "/" in raw or "\\" in raw or raw in (".", ".."):
        return ""
    stem, _, ext = raw.rpartition(".")
    if stem != BACKGROUND_IMAGE_STEM:
        return ""
    safe_ext = normalize_background_ext(ext)
    return f"{BACKGROUND_IMAGE_STEM}.{safe_ext}" if safe_ext else ""


def normalize_background(value: Any) -> Dict[str, Any]:
    """Return a normalized background settings object for storage.

    Shape: ``{"fileName": "bg.<ext>"|"", "opacity": <0-100>}``. An empty
    ``fileName`` means "no background image". The extension is intentionally
    NOT stored separately — it is always derivable from ``fileName`` via
    :func:`background_ext_from_filename`.

    Accepts the legacy ``{"ext": ...}`` shape (pre-filename storage) so older
    configs keep working.
    """
    src = value if isinstance(value, dict) else {}
    opacity = (
        normalize_background_opacity(src.get("opacity"))
        if "opacity" in src
        else _DEFAULT_BACKGROUND_OPACITY
    )
    file_name = normalize_background_filename(src.get("fileName"))
    if not file_name:
        # Legacy fallback: derive the file name from a stored bare extension.
        file_name = background_filename_for_ext(src.get("ext"))
    return {
        "fileName": file_name,
        "opacity": opacity,
    }


def background_ext_from_filename(file_name: str) -> str:
    """Return the (validated) extension for a stored background file name."""
    safe = normalize_background_filename(file_name)
    return normalize_background_ext(safe.rpartition(".")[2]) if safe else ""


def background_image_path(config_dir: Path, ext: str) -> Path:
    """Path for the background image file given a (validated) extension."""
    safe_ext = normalize_background_ext(ext)
    if not safe_ext:
        raise ValueError("unsupported background image extension")
    return Path(config_dir) / f"{BACKGROUND_IMAGE_STEM}.{safe_ext}"


def remove_background_image_files(config_dir: Path) -> None:
    """Delete every ``bg.<ext>`` file in the config dir (across all allowed exts)."""
    base = Path(config_dir)
    for ext in _ALLOWED_BACKGROUND_EXTS:
        candidate = base / f"{BACKGROUND_IMAGE_STEM}.{ext}"
        try:
            if candidate.exists():
                candidate.unlink()
        except OSError:
            pass


def allowed_background_exts() -> tuple:
    return _ALLOWED_BACKGROUND_EXTS
