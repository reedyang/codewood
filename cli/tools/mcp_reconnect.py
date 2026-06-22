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
        from ._delegation import delegate_mcp

        return delegate_mcp(agent, "mcp_reconnect", params if isinstance(params, dict) else {})
