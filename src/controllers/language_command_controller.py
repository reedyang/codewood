from __future__ import annotations

import shlex
from typing import Any


def _t(agent: Any, en: str, zh: str) -> str:
    from ..core.localization import get_display_language, text

    return text(en, zh, get_display_language(agent))


def language_usage(agent: Any) -> str:
    from ..core.localization import get_display_language, language_display_name

    lang = get_display_language(agent)
    current_name = language_display_name(lang)
    return (
        f"{_t(agent, 'Usage:', '用法：')}\n"
        f"  /language <language code>\n\n"
        f"{_t(agent, 'Current language:', '当前语言：')} {current_name}\n"
        f"{_t(agent, 'Available languages:', '可选语言：')}\n"
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
        print(
            _t(
                agent,
                f"❌ Unsupported language: {arg}\n{language_usage(agent)}",
                f"❌ 不支持的语言：{arg}\n{language_usage(agent)}",
            )
        )
        return True

    if normalized == current:
        print(
            _t(
                agent,
                f"ℹ️ Language is already set to {language_display_name(normalized)}",
                f"ℹ️ 当前语言已经是 {language_display_name(normalized)}",
            )
        )
        return True

    result = apply_display_language(agent, normalized)
    if not result.get("success"):
        print(
            _t(
                agent,
                f"❌ Failed to update language: {result.get('error')}",
                f"❌ 更新语言失败：{result.get('error')}",
            )
        )
        return True

    reload_fn = getattr(agent, "_reload_chat_history_from_anchor_on_resize", None)
    if callable(reload_fn):
        try:
            reload_fn()
        except Exception as e:
            print(
                _t(
                    agent,
                    f"✅ Language updated to {language_display_name(normalized)}\n⚠️ Reload failed: {e}",
                    f"✅ 语言已更新为 {language_display_name(normalized)}\n⚠️ 重载失败：{e}",
                )
            )
    else:
        print(
            _t(
                agent,
                f"✅ Language updated to {language_display_name(normalized)}",
                f"✅ 语言已更新为 {language_display_name(normalized)}",
            )
        )
    return True
