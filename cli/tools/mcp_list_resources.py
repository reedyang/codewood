"""Tool: mcp_list_resources."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpListResourcesTool(BaseTool):
    name = "mcp_list_resources"
    description = "List resources for a specified MCP server."
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
        from ._delegation import delegate_mcp

        return delegate_mcp(agent, "mcp_list_resources", params if isinstance(params, dict) else {})
