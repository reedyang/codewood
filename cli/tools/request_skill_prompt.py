"""Tool: request_skill_prompt."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class RequestSkillPromptTool(BaseTool):
    name = "request_skill_prompt"
    description = "Request the prompt for a specified skill."
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
        # request_skill_prompt is intercepted and fully handled by the runtime
        # loop before tool dispatch; this path is not normally reached.
        return {"success": False, "error": "request_skill_prompt is handled by the runtime loop"}
