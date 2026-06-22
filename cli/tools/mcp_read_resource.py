"""Tool: mcp_read_resource."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpReadResourceTool(BaseTool):
    name = "mcp_read_resource"
    description = "Read MCP resource content by URI."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
            "uri": {
                "type": "string",
            },
            "timeout_s": {
                "type": "number",
            },
        },
        "required": [
            "server",
            "uri",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_mcp

        return delegate_mcp(agent, "mcp_read_resource", params if isinstance(params, dict) else {})
