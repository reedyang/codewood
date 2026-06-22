"""Tool: memory_add."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class MemoryAddTool(BaseTool):
    name = "memory_add"
    description = "Write one experiential memory entry. Use only for short factual entries such as agreements, preference conclusions, or corrections. If the user emphasizes permanent/long-term memory and the content is a stable default (forms of address, assistant display name, long-term interaction rules), use user_preferences_patch instead of only this tool. Do not save code snippets, raw script/command output, raw logs, or long summarized text; code/output information should be read live through shell when needed (with summarize if necessary). Do not write passwords, tokens, or private keys. If you believe the user's view may be wrong, record your independent judgment in system_note."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
            },
            "content": {
                "type": "string",
            },
            "tier": {
                "type": "string",
                "description": "working | episodic | durable",
            },
            "memory_type": {
                "type": "string",
                "description": "For example lesson / preference / note.",
            },
            "source": {
                "type": "string",
                "description": "For example assistant / user_request / auto.",
            },
            "user_request": {
                "type": "string",
            },
            "system_note": {
                "type": "string",
            },
        },
        "required": [
            "content",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
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
