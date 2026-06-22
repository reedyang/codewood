"""Tool: mcp_call_tool."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpCallToolTool(BaseTool):
    name = "mcp_call_tool"
    description = "Call a single MCP tool with JSON arguments."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
            "tool": {
                "type": "string",
            },
            "arguments": {
                "type": "object",
            },
            "timeout_s": {
                "type": "number",
            },
        },
        "required": [
            "server",
            "tool",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_mcp

        return delegate_mcp(agent, "mcp_call_tool", params if isinstance(params, dict) else {})
