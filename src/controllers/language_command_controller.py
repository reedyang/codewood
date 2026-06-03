from __future__ import annotations

import shlex
from typing import Any


def _t(agent: Any, key: str, **kwargs: Any) -> str:
    from ..core.localization import get_display_language, translate

    return translate(key, get_display_language(agent), **kwargs)


def language_usage(agent: Any) -> str:
    from ..core.localization import get_display_language, language_display_name

    lang = get_display_language(agent)
    current_name = language_display_name(lang)
    return (
        f"{_t(agent, 'common.usage')}\n"
        f"  /language <language code>\n\n"
        f"{_t(agent, 'language.current_label')} {current_name}\n"
        f"{_t(agent, 'language.available_label')}\n"
        f"  - en - {language_display_name('en')}\n"
        f"  - zh-CN - {language_display_name('zh-CN')}\n"
    )


def handle_language_builtin_command(agent: Any, builtin_line: str) -> bool:
    from ..core.localization import (
        apply_display_language,
        get_display_language,
        language_display_name,
        normalize_display_language,
    )

    raw = str(builtin_line or "").strip()
    if not raw.lower().startswith("language"):
        return False
    parts = shlex.split(raw)
    if len(parts) == 1:
        print(language_usage(agent))
        return True
    if len(parts) != 2:
        print(language_usage(agent))
        return True

    arg = str(parts[1] or "").strip()
    current = get_display_language(agent)

    normalized = normalize_display_language(arg)
    if normalized is None:
        print(_t(agent, "language.unsupported_with_usage", language=arg, usage=language_usage(agent)))
        return True

    if normalized == current:
        print(_t(agent, "language.already_set", language=language_display_name(normalized)))
        return True

    result = apply_display_language(agent, normalized)
    if not result.get("success"):
        print(_t(agent, "language.update_failed", error=result.get("error")))
        return True

    reload_fn = getattr(agent, "_reload_chat_history_from_anchor_on_resize", None)
    if callable(reload_fn):
        try:
            reload_fn()
        except Exception as e:
            print(
                _t(
                    agent,
                    "language.updated_with_reload_warning",
                    language=language_display_name(normalized),
                    error=e,
                )
            )
    else:
        print(_t(agent, "language.updated", language=language_display_name(normalized)))
    return True
