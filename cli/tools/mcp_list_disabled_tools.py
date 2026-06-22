"""Tool: mcp_list_disabled_tools."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpListDisabledToolsTool(BaseTool):
    name = "mcp_list_disabled_tools"
    description = "View the MCP tool disabled list (optionally filtered by server)."
    requires_mcp = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
        },
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_mcp

        return delegate_mcp(agent, "mcp_list_disabled_tools", params if isinstance(params, dict) else {})
