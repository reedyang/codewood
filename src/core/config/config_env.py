import os
import re
import json
from typing import Any


_ENV_PLACEHOLDER_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _parse_env_value(raw_value: str) -> Any:
    text = raw_value.strip()
    if not text:
        return ""
    low = text.lower()
    if low in ("yes", "on"):
        return True
    if low in ("no", "off"):
        return False
    try:
        return json.loads(text)
    except Exception:
        return raw_value


def resolve_env_placeholder(value: str) -> Any:
    """Resolve `${ENV_NAME}` to environment variable value and auto-convert primitive types."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    m = _ENV_PLACEHOLDER_RE.match(text)
    if not m:
        return value
    raw = os.environ.get(m.group(1), "")
    return _parse_env_value(raw)


def resolve_string_values_in_data(data: Any) -> Any:
    """
    Recursively resolve `${ENV_NAME}` placeholders in string values.
    Non-string values keep their original type.
    """
    if isinstance(data, dict):
        return {k: resolve_string_values_in_data(v) for k, v in data.items()}
    if isinstance(data, list):
        return [resolve_string_values_in_data(v) for v in data]
    if isinstance(data, tuple):
        return tuple(resolve_string_values_in_data(v) for v in data)
    if isinstance(data, str):
        return resolve_env_placeholder(data)
    return data
