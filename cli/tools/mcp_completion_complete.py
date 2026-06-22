"""Tool: mcp_completion_complete."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpCompletionCompleteTool(BaseTool):
    name = "mcp_completion_complete"
    description = "Call the MCP completion/complete capability."
    requires_mcp = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
            "completion_params": {
                "type": "object",
            },
            "timeout_s": {
                "type": "number",
            },
        },
        "required": [
            "server",
            "completion_params",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_mcp

        return delegate_mcp(agent, "mcp_completion_complete", params if isinstance(params, dict) else {})
