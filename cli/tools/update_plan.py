"""Tool: update_plan."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class UpdatePlanTool(BaseTool):
    name = "update_plan"
    description = "Maintain an up-to-date, step-by-step plan for the current task. Provide an ordered list of short steps, each with a status (pending, in_progress, or completed). Keep at most one step in_progress at a time. The plan is stored on the active chat record as model context only and is not surfaced verbatim to the user."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "explanation": {
                "type": "string",
                "description": "Optional short note about why the plan is being updated (e.g. a step finished or the plan was revised).",
            },
            "plan": {
                "type": "array",
                "description": "Ordered list of plan steps. Each item must include `step` (1-sentence summary, no more than 5-7 words) and `status` (pending, in_progress, or completed).",
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {
                            "type": "string",
                        },
                        "status": {
                            "type": "string",
                            "enum": [
                                "pending",
                                "in_progress",
                                "completed",
                            ],
                        },
                    },
                    "required": [
                        "step",
                        "status",
                    ],
                },
            },
        },
        "required": [
            "plan",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_core

        return delegate_core(agent, "update_plan", params if isinstance(params, dict) else {})
