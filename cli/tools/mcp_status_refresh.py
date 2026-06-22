"""Tool: mcp_status_refresh."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpStatusRefreshTool(BaseTool):
    name = "mcp_status_refresh"
    description = "Refresh MCP status through live server checks."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "servers": {
                "type": "array",
                "items": {
                    "type": "string",
                },
            },
            "timeout_s": {
                "type": "number",
            },
            "force": {
                "type": "boolean",
            },
            "log_limit": {
                "type": "integer",
            },
        },
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_mcp

        return delegate_mcp(agent, "mcp_status_refresh", params if isinstance(params, dict) else {})
