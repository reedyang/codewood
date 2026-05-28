"""Console title helpers."""

from __future__ import annotations

import sys
from typing import Optional

from ..config.app_info import get_app_name


def set_windows_console_title(title: Optional[str] = None) -> None:
    """Best-effort: set console window title on Windows."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        resolved = str(title or "").strip() or get_app_name()
        ctypes.windll.kernel32.SetConsoleTitleW(resolved)
    except Exception:
        pass


def restore_app_console_title() -> None:
    """Best-effort: restore title to configured app name."""
    set_windows_console_title(get_app_name())

