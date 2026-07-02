"""Command: memory_stats."""

from __future__ import annotations

from typing import Any, Dict


def run_memory_stats(agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    if not agent._ensure_memory_service():
        return {"success": False, "error": "memory service unavailable"}
    try:
        st = agent.memory_service.stats()
        return {"success": True, "stats": st}
    except Exception as e:
        return {"success": False, "error": f"memory stats failed: {e}"}
