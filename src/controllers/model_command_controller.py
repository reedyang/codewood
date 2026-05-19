from __future__ import annotations

import shlex
from typing import Any


def model_usage() -> str:
    return (
        "用法:\n"
        "  /model\n"
        "  /model <model_provider>:<name>\n"
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
            print(f"当前模型: {current}")
        configured = list(agent._get_configured_model_selectors() or [])
        if configured:
            print("可选模型:")
            for selector in configured:
                print(f"  - {selector}")
        else:
            print("⚠️ 未在 config.json 里找到 model_providers 配置")
        print(model_usage())
        return True

    selector = " ".join(parts[1:]).strip()
    if not selector:
        print(f"❌ 请提供模型名称\n{model_usage()}")
        return True
    if ":" not in selector:
        print(f"❌ 模型格式错误，应为 <model_provider>:<name>\n{model_usage()}")
        return True
    print(agent._switch_model_by_selector(selector))
    return True
