"""Command: mcp_list_disabled_tools."""

from __future__ import annotations

from typing import Any, Dict


def run_mcp_list_disabled_tools(agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    params = params if isinstance(params, dict) else {}
    server = params.get("server")
    try:
        result = agent.mcp_manager.list_disabled_tools(
            str(server).strip() if server else None
        )
        total = sum(len(v) for v in result.values()) if isinstance(result, dict) else 0
        return {
            "success": True,
            "server": server,
            "disabled_tools": result,
            "count": total,
            "message": "MCP disabled tools listed",
        }
    except Exception as e:
        return {"success": False, "error": f"MCP list disabled tools failed: {e}"}
