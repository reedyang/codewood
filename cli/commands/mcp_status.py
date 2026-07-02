"""Command: mcp_status."""

from __future__ import annotations

from typing import Any, Dict


def run_mcp_status(agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    params = params if isinstance(params, dict) else {}
    log_limit = int(params.get("log_limit", 20))
    status = agent.mcp_manager.get_status(log_limit=log_limit)
    return {
        "success": True,
        "cache_only": True,
        "status": status,
        "message": "MCP cached status fetched",
    }
