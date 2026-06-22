"""Tool: request_skill_prompt."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class RequestSkillPromptTool(BaseTool):
    name = "request_skill_prompt"
    description = "Request injection of the prompt for a specified skill. This is a virtual control tool and does not directly perform business actions. Supports chunked long skill bodies: pass section to load a specific section, or full=true to load the complete body."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "string",
            },
            "section": {
                "type": "integer",
                "minimum": 1,
            },
            "full": {
                "type": "boolean",
            },
        },
        "required": [
            "skill_id",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_skill

        return delegate_skill(agent, "request_skill_prompt", params if isinstance(params, dict) else {})
