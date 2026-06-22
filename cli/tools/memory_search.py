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
        params = params if isinstance(params, dict) else {}
        if not agent._ensure_memory_service():
            return {"success": False, "error": "memory service unavailable"}
        query = str(params.get("query") or "").strip()
        top_k = int(params.get("top_k", params.get("limit", 6)) or 6)
        if not query:
            return {"success": False, "error": "missing query"}
        try:
            sk = agent._memory_scope_key()
            results = agent.memory_service.search_memories(query, top_k=top_k, scope_key=sk)
            return {"success": True, "results": results, "query": query, "scope": sk}
        except Exception as e:
            return {"success": False, "error": f"memory search failed: {e}"}
