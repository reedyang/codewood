from __future__ import annotations

import shlex
from typing import Any


def _t(agent: Any, en: str, zh: str) -> str:
    from ..core.localization import get_display_language, text

    return text(en, zh, get_display_language(agent))


def model_usage(agent: Any) -> str:
    return (
        f"{_t(agent, 'Usage:', '用法：')}\n"
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
            print(_t(agent, f"Current model: {current}", f"当前模型：{current}"))
        configured = list(agent._get_configured_model_selectors() or [])
        if configured:
            print(_t(agent, "Available models:", "可选模型："))
            for selector in configured:
                print(f"  - {selector}")
        else:
            print(_t(agent, "⚠️ model_providers configuration was not found in config.jsonc", "⚠️ 在 config.jsonc 中未找到 model_providers 配置"))
        print(model_usage(agent))
        return True

    selector = " ".join(parts[1:]).strip()
    if not selector:
        print(_t(agent, f"❌ Please provide a model name\n{model_usage(agent)}", f"❌ 请提供模型名称\n{model_usage(agent)}"))
        return True
    if ":" not in selector:
        print(_t(agent, f"❌ Invalid model format. Expected: <model_provider>:<name>\n{model_usage(agent)}", f"❌ 模型格式无效。应为：<model_provider>:<name>\n{model_usage(agent)}"))
        return True
    print(agent._switch_model_by_selector(selector))
    return True
