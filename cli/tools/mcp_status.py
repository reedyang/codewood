"""Tool: mcp_status."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpStatusTool(BaseTool):
    name = "mcp_status"
    description = "Read MCP status only from the in-memory cache."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "log_limit": {
                "type": "integer",
            },
        },
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_mcp

        return delegate_mcp(agent, "mcp_status", params if isinstance(params, dict) else {})
