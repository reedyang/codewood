"""Tool: memory_search."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class MemorySearchTool(BaseTool):
    name = "memory_search"
    description = "Search experiential memory (user-requested remembered facts) within the current workspace scope. Use when the user explicitly asks 'do you remember...', when you need information from past explicit remembers, or when the memory context injected into the user message is insufficient. Prefer descriptive natural-language queries. Limit is optional."
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
            output_lines = []
            for r in results:
                rid = r.get("id", "")
                title = r.get("title", "")
                content = r.get("content", "")[:200]
                sim = r.get("similarity", 0)
                output_lines.append(f"[id={rid[:12]}](sim={sim:.2f}) {title}: {content}")
            output = "\n".join(output_lines) if output_lines else "no results"
            return {"success": True, "results": results, "output": output, "query": query, "scope": sk}
        except Exception as e:
            return {"success": False, "error": f"memory search failed: {e}"}
