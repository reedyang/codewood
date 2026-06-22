"""Tool: mcp_reconnect."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpReconnectTool(BaseTool):
    name = "mcp_reconnect"
    description = "Reconnect a single MCP server and refresh its tool list."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
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
        import time
        from ..integrations.mcp import McpError

        params = params if isinstance(params, dict) else {}
        server = params.get("server")
        timeout_s = float(params.get("timeout_s", 15.0))
        if not server:
            return {"success": False, "error": "missing server"}
        try:
            tools = agent.mcp_manager.reconnect_server(str(server), timeout_s=timeout_s)
            agent._mcp_pending_user_input.pop(str(server), None)
            agent.system_prompt = agent._compose_system_prompt_snapshot(include_tools=False)
            status = agent.mcp_manager.get_status().get("servers", {}).get(str(server), {})
            return {
                "success": True,
                "server": server,
                "tools": tools,
                "count": len(tools) if isinstance(tools, list) else 0,
                "source": status.get("source", ""),
                "message": f"MCP server reconnected (server={server})",
            }
        except McpError as e:
            err = str(e)
            err_l = err.lower()
            auth_like = (
                ("401" in err_l)
                or ("unauthorized" in err_l)
                or ("invalid token" in err_l)
                or ("token verification failed" in err_l)
            )
            if auth_like:
                agent._mcp_pending_user_input[str(server)] = {
                    "input_type": "token",
                    "ts": time.time(),
                }
                return {
                    "success": False,
                    "error": f"MCP reconnect failed: {err}",
                    "retryable": False,
                    "needs_user_input": True,
                    "input_type": "token",
                    "suggestion": (
                        "authentication failed; wait for user to provide a new token "
                        "before retrying mcp_reconnect"
                    ),
                }
            return {"success": False, "error": f"MCP reconnect failed: {err}"}
        except Exception as e:
            return {"success": False, "error": f"MCP reconnect exception: {e}"}
