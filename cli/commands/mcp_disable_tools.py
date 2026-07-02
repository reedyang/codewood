"""Command: mcp_disable_tools."""

from __future__ import annotations

from typing import Any, Dict, List

from ..integrations.mcp import McpError


def run_mcp_disable_tools(agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    params = params if isinstance(params, dict) else {}
    server = params.get("server")
    tools_param = params.get("tools")
    if not server:
        return {"success": False, "error": "missing server"}
    names: List[str] = []
    if isinstance(tools_param, str):
        names = [x.strip() for x in tools_param.split(",") if x.strip()]
    elif isinstance(tools_param, list):
        names = [str(x).strip() for x in tools_param if str(x).strip()]
    else:
        return {"success": False, "error": "tools must be csv or list"}
    if not names:
        return {"success": False, "error": "tools is empty"}
    try:
        disabled = agent.mcp_manager.disable_tools(str(server), names)
        agent.system_prompt = agent._compose_system_prompt_snapshot(include_tools=False)
        return {
            "success": True,
            "server": server,
            "disabled_tools": disabled,
            "count": len(disabled),
            "message": f"MCP tools disabled (server={server})",
        }
    except McpError as e:
        return {"success": False, "error": f"MCP disable tools failed: {e}"}
    except Exception as e:
        return {"success": False, "error": f"MCP disable tools exception: {e}"}
