"""Tool: mcp_call_tool_batch."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpCallToolBatchTool(BaseTool):
    name = "mcp_call_tool_batch"
    description = "Call multiple MCP tools in one batch request."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
            "calls": {
                "type": "array",
                "items": {
                    "type": "object",
                },
            },
            "timeout_s": {
                "type": "number",
            },
            "allow_partial_failure": {
                "type": "boolean",
            },
        },
        "required": [
            "server",
            "calls",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_mcp

        return delegate_mcp(agent, "mcp_call_tool_batch", params if isinstance(params, dict) else {})
