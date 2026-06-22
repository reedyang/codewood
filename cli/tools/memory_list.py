"""Tool: memory_list."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class MemoryListTool(BaseTool):
    name = "memory_list"
    description = "List recent experiential memory summaries in the current workspace scope, sorted by recent access."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
            },
        },
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        if not agent._ensure_memory_service():
            return {"success": False, "error": "memory service unavailable"}
        limit = int(params.get("limit", 20) or 20)
        try:
            rows = agent.memory_service.list_recent(limit=limit, scope_key=agent._memory_scope_key())
            return {"success": True, "items": rows}
        except Exception as e:
            return {"success": False, "error": f"memory list failed: {e}"}
