"""Tool: memory_search."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class MemorySearchTool(BaseTool):
    name = "memory_search"
    description = "Semantically search experiential memory within the current workspace scope. When information is missing to complete the task, call this before other built-in tools, skills, or MCP tools, except for one-off inputs clearly unrelated to memory or pure external facts. If there are no hits or information remains insufficient, use other capabilities to fill the gap. If this turn's system prompt experiential memory already fully covers the needed information, do not search again. When a natural-language entity reference lacks a stable identifier, query with the entity name/alias and likely keywords; do not invent IDs before searching. Put retrieval essentials in query; limit is optional."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
            },
            "limit": {
                "type": "integer",
            },
        },
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_memory

        return delegate_memory(agent, "memory_search", params if isinstance(params, dict) else {})
