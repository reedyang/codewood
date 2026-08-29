import re
from typing import Any, Dict, List


DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_OLLAMA_PORT = 11_434
_CTX_WINDOW_PATTERN = re.compile(r"^(\d+)([kKmM]?)$")


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
        suffix = m.group(2)
        if suffix:
            if suffix in ("k", "K"):
                num *= 1000
            elif suffix in ("m", "M"):
                num *= 1_000_000
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


def parse_extra_headers(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}

    headers: Dict[str, str] = {}
    for raw_name, raw_value in value.items():
        name = str(raw_name or "").strip()
        if not name:
            continue
        if raw_value is None:
            continue
        value_text = str(raw_value).strip()
        if not value_text:
            continue
        headers[name] = value_text
    return headers


def parse_reasoning_effort(value: Any) -> List[str]:
    """Normalize a model's optional reasoning-effort list.

    Accepts a list of non-empty strings (order preserved, duplicates dropped).
    Anything else yields an empty list, meaning the model exposes no selectable
    reasoning effort.
    """
    if not isinstance(value, list):
        return []
    out: List[str] = []
    seen = set()
    for item in value:
        level = str(item or "").strip()
        if not level:
            continue
        key = level.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(level)
    return out


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
        streaming_raw: Any = True
        extra_headers_raw: Any = {}
        multimodal_raw: Any = True
        reasoning_effort_raw: Any = None
        thinking_raw: Any = True
        if isinstance(item, str):
            model_name = item.strip()
        elif isinstance(item, dict):
            model_name = str(item.get("name") or "").strip()
            context_window_raw = item.get("context_window")
            streaming_raw = item.get("streaming", True)
            extra_headers_raw = item.get("extra_headers", {})
            multimodal_raw = item.get("multimodal", True)
            reasoning_effort_raw = item.get("reasoning_effort")
            thinking_raw = item.get("thinking", True)
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
                "streaming": parse_bool_flag(
                    streaming_raw, default_value=True
                ),
                # Whether the model can accept image input. Defaults to True;
                # set ``"multimodal": false`` to hide image-input capability
                # of the ``read`` tool.
                "multimodal": parse_bool_flag(
                    multimodal_raw, default_value=True
                ),
                "extra_headers": parse_extra_headers(extra_headers_raw),
                # Optional reasoning effort levels the model supports (e.g.
                # ["low", "medium", "high"]). Empty when the model has no
                # selectable reasoning effort.
                "reasoning_effort": parse_reasoning_effort(reasoning_effort_raw),
                "thinking": parse_bool_flag(thinking_raw, default_value=True),
            }
        )
    return parsed
