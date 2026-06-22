"""Tool: memory_stats."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class MemoryStatsTool(BaseTool):
    name = "memory_stats"
    description = "Read experiential memory storage stats (entry count, embedding model, directory)."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {},
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_memory

        return delegate_memory(agent, "memory_stats", params if isinstance(params, dict) else {})
