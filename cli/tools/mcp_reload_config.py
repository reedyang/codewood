"""Tool: mcp_reload_config."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpReloadConfigTool(BaseTool):
    name = "mcp_reload_config"
    description = "Manually reload mcp.jsonc and hot-update MCP incrementally (add/change/delete)."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {},
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_mcp

        return delegate_mcp(agent, "mcp_reload_config", params if isinstance(params, dict) else {})
