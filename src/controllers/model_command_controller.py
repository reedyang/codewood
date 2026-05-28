from __future__ import annotations

import shlex
from typing import Any


def model_usage() -> str:
    return (
        "Usage:\n"
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
            print(f"Current model: {current}")
        configured = list(agent._get_configured_model_selectors() or [])
        if configured:
            print("Available models:")
            for selector in configured:
                print(f"  - {selector}")
        else:
            print("⚠️ model_providers configuration was not found in config.jsonc")
        print(model_usage())
        return True

    selector = " ".join(parts[1:]).strip()
    if not selector:
        print(f"❌ Please provide a model name\n{model_usage()}")
        return True
    if ":" not in selector:
        print(f"❌ Invalid model format. Expected: <model_provider>:<name>\n{model_usage()}")
        return True
    print(agent._switch_model_by_selector(selector))
    return True
