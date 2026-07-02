"""Command: mcp_reload_config."""

from __future__ import annotations

from typing import Any, Dict


def run_mcp_reload_config(agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    params = params if isinstance(params, dict) else {}
    result = agent._reload_mcp_config_now()
    if result.get("success"):
        return {
            "success": True,
            "changed": bool(result.get("changed", False)),
            "summary": result.get("summary", {}),
            "message": str(result.get("message", "MCP config reloaded")),
        }
    return {"success": False, "error": str(result.get("error", "MCP config reload failed"))}
