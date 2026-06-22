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
        f"  /model <model_provider>:<name> <reasoning_level>\n"
        f"  /model reasoning <reasoning_level>\n"
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
        levels = list(agent._current_model_reasoning_levels() or [])
        if levels:
            active_level = str(agent._current_reasoning_level() or "")
            if active_level:
                print(_t(agent, "reasoning.current", level=active_level))
            print(_t(agent, "reasoning.available", levels=", ".join(levels)))
        configured = list(agent._get_configured_model_selectors() or [])
        if configured:
            print(_t(agent, "model.available"))
            for selector in configured:
                print(f"  - {selector}")
        else:
            print(_t(agent, "model.providers_missing_warning"))
        print(model_usage(agent))
        return True

    # ``/model reasoning <level>`` adjusts only the reasoning level.
    if parts[1].lower() == "reasoning":
        level = " ".join(parts[2:]).strip()
        print(agent._set_reasoning_level(level))
        return True

    # ``/model <provider>:<name> [reasoning_level]``
    selector = str(parts[1]).strip()
    if not selector:
        print(_t(agent, "model.name_missing_with_usage", usage=model_usage(agent)))
        return True
    if ":" not in selector:
        # Allow selectors with spaces (e.g. names containing spaces) by treating
        # everything as the selector when no trailing level is recognized.
        selector = " ".join(parts[1:]).strip()
        if ":" not in selector:
            print(_t(agent, "model.invalid_format_with_usage", usage=model_usage(agent)))
            return True
        print(agent._switch_model_by_selector(selector))
        return True

    level = " ".join(parts[2:]).strip()
    print(agent._switch_model_by_selector(selector))
    if level:
        print(agent._set_reasoning_level(level))
    return True
