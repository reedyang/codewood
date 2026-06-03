from __future__ import annotations

import shlex
from typing import Any


def _t(agent: Any, key: str, **kwargs: Any) -> str:
    from ..core.localization import get_display_language, translate

    return translate(key, get_display_language(agent), **kwargs)


def model_usage(agent: Any) -> str:
    return (
        f"{_t(agent, 'common.usage')}\n"
        f"  /model\n"
        f"  /model <model_provider>:<name>\n"
    )


def handle_model_builtin_command(agent: Any, builtin_line: str) -> bool:
    raw = str(builtin_line or "").strip()
    if not raw:
        return False
    parts = shlex.split(raw)
    if not parts or parts[0].lower() != "model":
        return False
    if len(parts) == 1 or parts[1].lower() in ("help", "-h", "--help"):
        current = str(agent._current_model_selector() or "")
        if current:
            print(_t(agent, "model.current", current=current))
        configured = list(agent._get_configured_model_selectors() or [])
        if configured:
            print(_t(agent, "model.available"))
            for selector in configured:
                print(f"  - {selector}")
        else:
            print(_t(agent, "model.providers_missing_warning"))
        print(model_usage(agent))
        return True

    selector = " ".join(parts[1:]).strip()
    if not selector:
        print(_t(agent, "model.name_missing_with_usage", usage=model_usage(agent)))
        return True
    if ":" not in selector:
        print(_t(agent, "model.invalid_format_with_usage", usage=model_usage(agent)))
        return True
    print(agent._switch_model_by_selector(selector))
    return True
