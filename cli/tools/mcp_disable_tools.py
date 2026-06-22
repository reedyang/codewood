"""Tool: mcp_disable_tools."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpDisableToolsTool(BaseTool):
    name = "mcp_disable_tools"
    description = "Disable tools for a specified MCP server (supports comma-separated string or array)."
    requires_mcp = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
            "tools": {
                "description": "List of tool names; supports \"a,b,c\" or [\"a\",\"b\"].",
                "oneOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "array",
                        "items": {
                            "type": "string",
                        },
                    },
                ],
            },
        },
        "required": [
            "server",
            "tools",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_mcp

        return delegate_mcp(agent, "mcp_disable_tools", params if isinstance(params, dict) else {})
