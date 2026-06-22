"""Tool: mcp_get_prompt."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpGetPromptTool(BaseTool):
    name = "mcp_get_prompt"
    description = "Resolve a single MCP prompt with optional arguments."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
            "prompt": {
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
            "prompt",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_mcp

        return delegate_mcp(agent, "mcp_get_prompt", params if isinstance(params, dict) else {})
