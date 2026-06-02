import re
from typing import Any, Dict, List


DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_OLLAMA_PORT = 11_434
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


def parse_bool_flag(value: Any, default_value: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
    return default_value


def parse_port(value: Any, default_value: int = DEFAULT_OLLAMA_PORT) -> int:
    if isinstance(value, bool):
        return default_value
    if isinstance(value, int):
        return value if 0 < value <= 65535 else default_value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return default_value
        if not raw.isdigit():
            return default_value
        port = int(raw)
        return port if 0 < port <= 65535 else default_value
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
        use_simulated_tools_raw: Any = False
        streaming_raw: Any = True
        if isinstance(item, str):
            model_name = item.strip()
        elif isinstance(item, dict):
            model_name = str(item.get("name") or "").strip()
            context_window_raw = item.get("context_window")
            use_simulated_tools_raw = item.get("use_simulated_tools", False)
            streaming_raw = item.get("streaming", True)
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
                "use_simulated_tools": parse_bool_flag(
                    use_simulated_tools_raw, default_value=False
                ),
                "streaming": parse_bool_flag(
                    streaming_raw, default_value=True
                ),
            }
        )
    return parsed
