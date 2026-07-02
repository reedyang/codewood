"""Tool: mcp_call_tool."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpCallToolTool(BaseTool):
    name = "mcp_call_tool"
    description = "Call a single MCP tool with JSON arguments."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
            "tool": {
                "type": "string",
            },
            "arguments": {
                "type": "object",
            },
            "timeout_s": {
                "type": "number",
            },
        },
        "required": [
            "server",
            "tool",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..integrations.mcp import McpError

        params = params if isinstance(params, dict) else {}
        server = params.get("server")
        tool_name = params.get("tool")
        arguments = params.get("arguments", {})
        timeout_s = float(params.get("timeout_s", 20.0))
        if not server:
            return {"success": False, "error": "missing server"}
        if not tool_name:
            return {"success": False, "error": "missing tool"}
        if not isinstance(arguments, dict):
            return {"success": False, "error": "arguments must be object"}
        try:
            st = agent.mcp_manager.get_status().get("servers", {}).get(str(server), {})
            state_raw = str(st.get("state", "pending") or "pending").lower()
            if state_raw != "success":
                return {
                    "success": False,
                    "error": (
                        f"server={server} is not ready (state={state_raw}); "
                        "run mcp_call_tool with fresh timeout"
                    ),
                }
        except Exception:
            pass
        try:
            result = agent.mcp_manager.call_tool(
                str(server),
                str(tool_name),
                arguments,
                timeout_s=timeout_s,
            )
            return {
                "success": True,
                "server": server,
                "tool": tool_name,
                "result": result,
                "message": f"MCP tool called ({server}/{tool_name})",
            }
        except McpError as e:
            return {"success": False, "error": f"MCP tool call failed: {e}"}
        except Exception as e:
            return {"success": False, "error": f"MCP tool call exception: {e}"}
