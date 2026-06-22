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
        from ._delegation import delegate_memory

        return delegate_memory(agent, "memory_add", params if isinstance(params, dict) else {})
