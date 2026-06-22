"""Tool: mcp_server_info."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpServerInfoTool(BaseTool):
    name = "mcp_server_info"
    description = "Query aggregated information for a specified MCP server (status, tools, resources, prompts)."
    requires_mcp = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
            "refresh": {
                "type": "boolean",
            },
            "timeout_s": {
                "type": "number",
            },
            "include_tools": {
                "type": "boolean",
            },
            "include_resources": {
                "type": "boolean",
            },
            "include_resource_templates": {
                "type": "boolean",
            },
            "include_prompts": {
                "type": "boolean",
            },
        },
        "required": [
            "server",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_mcp

        return delegate_mcp(agent, "mcp_server_info", params if isinstance(params, dict) else {})
