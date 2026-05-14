from __future__ import annotations

from typing import Any, Dict, Optional


def dispatch_memory_tool(agent: Any, action: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if action == "memory_search":
        if not agent._ensure_memory_service():
            return {"success": False, "error": "memory service unavailable"}
        query = str(params.get("query") or "").strip()
        top_k = int(params.get("top_k", params.get("limit", 6)) or 6)
        if not query:
            return {"success": False, "error": "missing query"}
        try:
            sk = agent._memory_scope_key()
            results = agent.memory_service.search_memories(
                query, top_k=top_k, scope_key=sk
            )
            return {"success": True, "results": results, "query": query, "scope": sk}
        except Exception as e:
            return {"success": False, "error": f"memory search failed: {e}"}

    if action == "memory_add":
        if not agent._ensure_memory_service():
            return {"success": False, "error": "memory service unavailable"}
        title = str(params.get("title") or "memory").strip()[:500]
        content = str(params.get("content") or "").strip()
        if not content:
            return {"success": False, "error": "memory_add requires content"}
        tier = str(params.get("tier") or "episodic").strip().lower()
        if tier not in ("working", "episodic", "durable"):
            tier = "episodic"
        mtype = str(params.get("memory_type") or "lesson").strip()[:64] or "lesson"
        source = str(params.get("source") or "assistant").strip()[:64] or "assistant"
        user_request = params.get("user_request")
        ur = str(user_request).strip() if user_request is not None else None
        sys_note = params.get("system_note")
        sn = str(sys_note).strip()[:2000] if sys_note is not None else None
        if sn == "":
            sn = None
        try:
            mid = agent.memory_service.add_memory(
                title=title,
                content=content,
                tier=tier,
                memory_type=mtype,
                scope_key=agent._memory_scope_key(),
                source=source,
                user_request=ur,
                system_note=sn,
            )
            return {"success": True, "memory_id": mid, "title": title}
        except Exception as e:
            return {"success": False, "error": f"memory add failed: {e}"}

    if action == "memory_list":
        if not agent._ensure_memory_service():
            return {"success": False, "error": "memory service unavailable"}
        limit = int(params.get("limit", 20) or 20)
        try:
            rows = agent.memory_service.list_recent(
                limit=limit, scope_key=agent._memory_scope_key()
            )
            return {"success": True, "items": rows}
        except Exception as e:
            return {"success": False, "error": f"memory list failed: {e}"}

    if action == "memory_stats":
        if not agent._ensure_memory_service():
            return {"success": False, "error": "memory service unavailable"}
        try:
            st = agent.memory_service.stats()
            return {"success": True, "stats": st}
        except Exception as e:
            return {"success": False, "error": f"memory stats failed: {e}"}

    if action == "memory_delete":
        if not agent._ensure_memory_service():
            return {"success": False, "error": "memory service unavailable"}
        mid = str(params.get("memory_id") or params.get("id") or "").strip()
        if not mid:
            return {"success": False, "error": "missing memory_id"}
        try:
            ok = agent.memory_service.delete_memory(mid)
            return {"success": ok, "memory_id": mid}
        except Exception as e:
            return {"success": False, "error": f"memory delete failed: {e}"}

    return None
