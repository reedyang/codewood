"""Configuration package."""

from .app_info import APP_INFO, get_app_display_version, get_app_name, get_app_version
from .startup_tips import (
    DEFAULT_STARTUP_TIP,
    DEFAULT_STARTUP_TIP_ENTRY,
    format_tip_with_highlights,
    get_random_startup_tip_entry,
    get_random_startup_tip,
    load_startup_tip_entries,
    load_startup_tips,
    startup_tips_config_path,
)

__all__ = [
    "APP_INFO",
    "get_app_name",
    "get_app_version",
    "get_app_display_version",
    "DEFAULT_STARTUP_TIP",
    "DEFAULT_STARTUP_TIP_ENTRY",
    "startup_tips_config_path",
    "load_startup_tip_entries",
    "load_startup_tips",
    "get_random_startup_tip_entry",
    "get_random_startup_tip",
    "format_tip_with_highlights",
]
