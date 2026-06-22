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
        params = params if isinstance(params, dict) else {}
        log_limit = int(params.get("log_limit", 20))
        status = agent.mcp_manager.get_status(log_limit=log_limit)
        return {
            "success": True,
            "cache_only": True,
            "status": status,
            "message": "MCP cached status fetched",
        }
