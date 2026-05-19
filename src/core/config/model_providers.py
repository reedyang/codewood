import re
from typing import Any, Dict, List


DEFAULT_CONTEXT_WINDOW = 128_000
_CTX_WINDOW_PATTERN = re.compile(r"^(\d+)([kK]?)$")


def parse_context_window(value: Any, default_value: int = DEFAULT_CONTEXT_WINDOW) -> int:
    if isinstance(value, bool):
        return default_value
    if isinstance(value, int):
        return value if value > 0 else default_value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return default_value
        m = _CTX_WINDOW_PATTERN.fullmatch(raw)
        if not m:
            return default_value
        num = int(m.group(1))
        if num <= 0:
            return default_value
        if m.group(2):
            num *= 1000
        return num
    return default_value


def parse_configured_models(
    params_raw: Dict[str, Any], default_context_window: int = DEFAULT_CONTEXT_WINDOW
) -> List[Dict[str, Any]]:
    models = params_raw.get("models")
    if not isinstance(models, list):
        return []

    parsed: List[Dict[str, Any]] = []
    for item in models:
        model_name = ""
        context_window_raw: Any = None
        if isinstance(item, str):
            model_name = item.strip()
        elif isinstance(item, dict):
            model_name = str(item.get("name") or "").strip()
            context_window_raw = item.get("context_window")
        else:
            model_name = str(item or "").strip()
        if not model_name:
            continue
        parsed.append(
            {
                "name": model_name,
                "context_window": parse_context_window(
                    context_window_raw, default_value=default_context_window
                ),
            }
        )
    return parsed
