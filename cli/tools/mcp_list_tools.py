"""Tool: mcp_list_tools."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpListToolsTool(BaseTool):
    name = "mcp_list_tools"
    description = "List available tools for a specified MCP server."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
            "use_cache": {
                "type": "boolean",
            },
            "timeout_s": {
                "type": "number",
            },
        },
        "required": [
            "server",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..integrations.mcp import McpError

        params = params if isinstance(params, dict) else {}
        server = params.get("server")
        use_cache = bool(params.get("use_cache", True))
        timeout_s = float(params.get("timeout_s", 8.0))
        if not server:
            return {"success": False, "error": "missing server"}
        try:
            tools, from_cache = agent.mcp_manager.list_tools(
                str(server),
                timeout_s=timeout_s,
                use_cache=use_cache,
            )
            agent.system_prompt = agent._compose_system_prompt_snapshot(include_tools=False)
            status = agent.mcp_manager.get_status().get("servers", {}).get(str(server), {})
            return {
                "success": True,
                "server": server,
                "tools": tools,
                "from_cache": from_cache,
                "source": status.get("source", ""),
                "count": len(tools) if isinstance(tools, list) else 0,
                "message": f"MCP tools fetched (server={server})",
            }
        except McpError as e:
            return {"success": False, "error": f"MCP list tools failed: {e}"}
        except Exception as e:
            return {"success": False, "error": f"MCP list tools exception: {e}"}
