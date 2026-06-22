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
        from ._delegation import delegate_memory

        return delegate_memory(agent, "memory_list", params if isinstance(params, dict) else {})
