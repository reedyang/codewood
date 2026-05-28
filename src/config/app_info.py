"""Application metadata and branding helpers."""

from __future__ import annotations

import re
from typing import Dict

APP_INFO: Dict[str, str] = {
    "name": "Smart Shell",
    "version": "0.1.0",
    "author": "Reed Yang",
    "description": "Smart Shell AI Agent",
}


def get_app_name() -> str:
    name = str(APP_INFO.get("name") or "").strip()
    return name


def get_app_description() -> str:
    description = str(APP_INFO.get("description") or "").strip()
    return description or f"{get_app_name()} AI Agent"


def get_app_version() -> str:
    version = str(APP_INFO.get("version") or "").strip()
    return version or "0.1.0"


def get_app_display_version(prefix: str = "v") -> str:
    version = get_app_version()
    if version.lower().startswith(prefix.lower()):
        return version
    return f"{prefix}{version}"


def _slug_parts() -> list[str]:
    name = get_app_name()
    parts = [p for p in re.split(r"[^a-zA-Z0-9]+", name.lower()) if p]
    return parts or ["app"]


def get_app_slug_kebab() -> str:
    return "-".join(_slug_parts())


def get_app_slug_snake() -> str:
    return "_".join(_slug_parts())


def get_app_slug_compact() -> str:
    return "".join(_slug_parts())


def get_app_config_dirname() -> str:
    return f".{get_app_slug_compact()}"


def get_app_logger_root() -> str:
    return get_app_slug_compact()


def get_app_log_filename() -> str:
    return f"{get_app_logger_root()}.log"


def get_app_env_prefix() -> str:
    return get_app_slug_snake().upper()


def get_app_env_var(suffix: str) -> str:
    normalized_suffix = str(suffix or "").strip().upper().replace("-", "_")
    normalized_suffix = re.sub(r"[^A-Z0-9_]+", "_", normalized_suffix)
    normalized_suffix = re.sub(r"_+", "_", normalized_suffix).strip("_")
    if not normalized_suffix:
        return get_app_env_prefix()
    return f"{get_app_env_prefix()}_{normalized_suffix}"


def get_app_client_name() -> str:
    return get_app_slug_kebab()


def get_app_client_model_name() -> str:
    return f"{get_app_client_name()}-client"


def get_app_runtime_attr_name(suffix: str, *, leading_underscore: bool = False) -> str:
    raw = str(suffix or "").strip().lower().replace("-", "_")
    normalized = re.sub(r"[^a-z0-9_]+", "_", raw)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    base = get_app_slug_snake()
    name = f"{base}_{normalized}" if normalized else base
    return f"_{name}" if leading_underscore else name
