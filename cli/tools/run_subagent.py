"""Tool: run_subagent."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class RunSubagentTool(BaseTool):
    name = "run_subagent"
    description = "Delegate a subtask to a configured sub-agent. Returns a text result to continue the main task."
    requires_subagents = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "subagent": {
                "type": "string",
                "description": "The name of the sub-agent to invoke (must match one of the available sub-agents).",
            },
            "topic": {
                "type": "string",
                "description": "A short, concise topic describing what this sub-agent call is about. Displayed in the GUI chat title bar while viewing the sub-session.",
            },
            "prompt": {
                "type": "string",
                "description": "A complete, self-contained task description for the sub-agent, including all context it needs. Write it in the SAME language the user is using in their message (do not translate or switch languages when delegating).",
            },
            "image": {
                "type": "string",
                "description": "Optional path to an image file to attach to the sub-agent. Use this to delegate image analysis to a multimodal sub-agent (e.g. when the main model is not multimodal). The image is analyzed by the sub-agent's own model, not the main model.",
            },
        },
        "required": [
            "subagent",
            "topic",
            "prompt",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..core.localization import get_display_language, translate

        def _t(key: str, **kwargs: object) -> str:
            return translate(key, get_display_language(agent), **kwargs)

        args = params if isinstance(params, dict) else {}
        subagent = str(args.get("subagent") or "").strip()
        topic = str(args.get("topic") or "").strip()
        prompt = str(args.get("prompt") or "").strip()
        image = str(args.get("image") or "").strip() or None
        if not subagent:
            return {"success": False, "error": _t("subagents.error.missing_subagent")}
        if not topic:
            return {"success": False, "error": _t("subagents.error.missing_topic")}
        if not prompt:
            return {"success": False, "error": _t("subagents.error.empty_prompt")}

        from ..subagents.executor import run_subagent

        return run_subagent(agent, subagent, prompt, image=image, topic=topic)
