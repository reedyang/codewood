from __future__ import annotations

from typing import Any, Dict, Optional

from ...core.localization import get_display_language, translate


def _t(agent: Any, key: str, **kwargs: object) -> str:
    return translate(key, get_display_language(agent), **kwargs)


def dispatch_subagent_tool(agent: Any, action: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if action != "run_subagent":
        return None

    args = params if isinstance(params, dict) else {}
    subagent = str(args.get("subagent") or "").strip()
    prompt = str(args.get("prompt") or "").strip()
    image = str(args.get("image") or "").strip() or None
    if not subagent:
        return {"success": False, "error": _t(agent, "subagents.error.missing_subagent")}
    if not prompt:
        return {"success": False, "error": _t(agent, "subagents.error.empty_prompt")}

    from ...subagents.executor import run_subagent

    return run_subagent(agent, subagent, prompt, image=image)
