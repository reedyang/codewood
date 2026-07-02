"""Command: mcp_list_tools - TUI /mcp list-tools implementation."""

from __future__ import annotations

from typing import Any, Dict, List

from ..integrations.mcp import McpError


def run_mcp_list_tools(agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    params = params if isinstance(params, dict) else {}
    server = params.get("server")
    use_cache = bool(params.get("use_cache", True))
    timeout_s = float(params.get("timeout_s", 8.0))
    if not server:
        return {"success": False, "error": "missing server"}
    try:
        tools, from_cache = agent.mcp_manager.list_tools_with_disabled(
            str(server),
            timeout_s=timeout_s,
            use_cache=use_cache,
        )
        agent.system_prompt = agent._compose_system_prompt_snapshot(include_tools=False)
        status = agent.mcp_manager.get_status().get("servers", {}).get(str(server), {})

        enabled: List[Dict[str, Any]] = []
        disabled: List[Dict[str, Any]] = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            if bool(t.get("disabled", False)):
                disabled.append(t)
            else:
                enabled.append(t)

        return {
            "success": True,
            "server": server,
            "tools": tools,
            "enabled_tools": enabled,
            "disabled_tools": disabled,
            "from_cache": from_cache,
            "source": status.get("source", ""),
            "count": len(tools) if isinstance(tools, list) else 0,
            "enabled_count": len(enabled),
            "disabled_count": len(disabled),
            "message": f"MCP tools fetched (server={server})",
        }
    except McpError as e:
        return {"success": False, "error": f"MCP list tools failed: {e}"}
    except Exception as e:
        return {"success": False, "error": f"MCP list tools exception: {e}"}
