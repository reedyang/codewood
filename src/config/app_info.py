"""Application metadata configuration."""

from __future__ import annotations

from typing import Dict

APP_INFO: Dict[str, str] = {
    "name": "Smart Shell",
    "version": "0.1.0",
    "author": "AI Assistant",
    "description": "Smart Shell AI Agent",
}


def get_app_name() -> str:
    name = str(APP_INFO.get("name") or "").strip()
    return name or "Smart Shell"


def get_app_version() -> str:
    version = str(APP_INFO.get("version") or "").strip()
    return version or "0.1.0"


def get_app_display_version(prefix: str = "v") -> str:
    version = get_app_version()
    if version.lower().startswith(prefix.lower()):
        return version
    return f"{prefix}{version}"
