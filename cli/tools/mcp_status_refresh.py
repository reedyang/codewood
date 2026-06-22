"""Tool: mcp_status_refresh."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpStatusRefreshTool(BaseTool):
    name = "mcp_status_refresh"
    description = "Refresh MCP status through live server checks."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "servers": {
                "type": "array",
                "items": {
                    "type": "string",
                },
            },
            "timeout_s": {
                "type": "number",
            },
            "force": {
                "type": "boolean",
            },
            "log_limit": {
                "type": "integer",
            },
        },
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        timeout_s = float(params.get("timeout_s", 12.0))
        force = bool(params.get("force", True))
        log_limit = int(params.get("log_limit", 20))
        servers = params.get("servers")
        if servers is not None and not isinstance(servers, list):
            return {"success": False, "error": "servers must be a list"}
        try:
            status = agent.mcp_manager.refresh_status_sync(
                servers=[str(s) for s in servers] if isinstance(servers, list) else None,
                timeout_s=timeout_s,
                force=force,
            )
            agent.system_prompt = agent._compose_system_prompt_snapshot(include_tools=False)
            status["recent_logs"] = agent.mcp_manager.get_recent_logs(log_limit)
            return {
                "success": True,
                "cache_only": False,
                "status": status,
                "message": "MCP status refreshed",
            }
        except Exception as e:
            return {"success": False, "error": f"MCP status refresh failed: {e}"}
