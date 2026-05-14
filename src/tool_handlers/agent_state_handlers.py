from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional


def dispatch_agent_state_tool(agent: Any, action: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if action == "user_preferences_read":
        try:
            from .. import user_preferences_manager as _upm

            meta, body = _upm.read_body(Path(agent.config_dir))
            lim = int(params.get("max_chars") or 16000)
            truncated = len(body) > lim
            text = body if not truncated else body[:lim] + "..."
            return {
                "success": True,
                "meta": meta,
                "body": text,
                "truncated": truncated,
                "path": str(Path(agent.config_dir) / _upm.DEFAULT_FILENAME),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    if action == "user_preferences_patch":
        try:
            from .. import user_preferences_manager as _upm

            op = str(params.get("operation") or "upsert_section").strip().lower()
            if op == "replace_body":
                return _upm.replace_body(
                    Path(agent.config_dir),
                    str(params.get("markdown_body") or ""),
                )
            if op == "upsert_section":
                sh = str(params.get("section_heading") or "").strip()
                if not sh:
                    return {
                        "success": False,
                        "error": "user_preferences_patch upsert_section requires section_heading",
                    }
                sb = str(params.get("section_body") or "")
                return _upm.upsert_section(Path(agent.config_dir), sh, sb)
            return {"success": False, "error": f"unknown operation: {op}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    if action == "execution_policy_set":
        result = agent._set_execution_policy(params.get("policy", ""))
        if result.get("success"):
            print(result.get("message", "execution_policy updated"))
        else:
            print(result.get("error", "execution_policy update failed"))
        return result

    if action in ("freedom_enable", "freedom_on"):
        result = agent._enable_freedom()
        if result.get("success"):
            print(result.get("message", "freedom mode enabled"))
        else:
            print(result.get("error", "failed to enable freedom mode"))
        return result

    if action in ("freedom_disable", "freedom_off"):
        result = agent._disable_freedom()
        if result.get("success"):
            print(result.get("message", "freedom mode disabled"))
        else:
            print(result.get("error", "failed to disable freedom mode"))
        return result

    if action == "always_confirm_reset":
        result = agent._reset_always_confirm_skip()
        if result.get("success"):
            print(result.get("message", "always-confirm skip list reset"))
        return result

    return None
