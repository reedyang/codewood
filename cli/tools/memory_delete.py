"""Tool: memory_delete."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class MemoryDeleteTool(BaseTool):
    name = "memory_delete"
    description = "Delete one experiential memory entry by id."
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
        from ._delegation import delegate_memory

        return delegate_memory(agent, "memory_delete", params if isinstance(params, dict) else {})
