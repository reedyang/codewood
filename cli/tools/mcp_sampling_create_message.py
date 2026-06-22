"""Tool: mcp_sampling_create_message."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpSamplingCreateMessageTool(BaseTool):
    name = "mcp_sampling_create_message"
    description = "Call the MCP sampling/createMessage capability."
    requires_mcp = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
            "sampling_params": {
                "type": "object",
            },
            "timeout_s": {
                "type": "number",
            },
        },
        "required": [
            "server",
            "sampling_params",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_mcp

        return delegate_mcp(agent, "mcp_sampling_create_message", params if isinstance(params, dict) else {})
