"""Tool: user_preferences_read."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class UserPreferencesReadTool(BaseTool):
    name = "user_preferences_read"
    description = "Read the full persistent user preferences file (user_preferences.md under config) to inspect current content before editing. This is not the experiential memory store; its content is always injected into system. Optional max_chars truncates the returned content."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "max_chars": {
                "type": "integer",
                "description": "Optional maximum body characters; default 16000.",
            },
        },
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_agent_state

        return delegate_agent_state(agent, "user_preferences_read", params if isinstance(params, dict) else {})
