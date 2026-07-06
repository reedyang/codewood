"""Tool: memory_delete."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class MemoryDeleteTool(BaseTool):
    name = "memory_delete"
    description = "Delete a memory entry by id."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "string",
            },
        },
        "required": [
            "memory_id",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        if not agent._ensure_memory_service():
            return {"success": False, "error": "memory service unavailable"}
        mid = str(params.get("memory_id") or params.get("id") or "").strip()
        if not mid:
            return {"success": False, "error": "missing memory_id"}
        try:
            ok = agent.memory_service.delete_memory(mid)
            return {"success": ok, "memory_id": mid, "output": f"deleted memory_id={mid}" if ok else f"memory_id={mid} not found"}
        except Exception as e:
            return {"success": False, "error": f"memory delete failed: {e}"}
