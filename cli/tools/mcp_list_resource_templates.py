"""Tool: mcp_list_resource_templates."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpListResourceTemplatesTool(BaseTool):
    name = "mcp_list_resource_templates"
    description = "List MCP resource templates."
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

        return delegate_mcp(agent, "mcp_list_resource_templates", params if isinstance(params, dict) else {})
