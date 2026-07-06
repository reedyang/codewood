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
            "prompt": {
                "type": "string",
                "description": "A complete, self-contained task description for the sub-agent, including all context it needs.",
            },
            "image": {
                "type": "string",
                "description": "Optional path to an image file to attach to the sub-agent. Use this to delegate image analysis to a multimodal sub-agent (e.g. when the main model is not multimodal). The image is analyzed by the sub-agent's own model, not the main model.",
            },
        },
        "required": [
            "subagent",
            "prompt",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..core.localization import get_display_language, translate

        def _t(key: str, **kwargs: object) -> str:
            return translate(key, get_display_language(agent), **kwargs)

        args = params if isinstance(params, dict) else {}
        subagent = str(args.get("subagent") or "").strip()
        prompt = str(args.get("prompt") or "").strip()
        image = str(args.get("image") or "").strip() or None
        if not subagent:
            return {"success": False, "error": _t("subagents.error.missing_subagent")}
        if not prompt:
            return {"success": False, "error": _t("subagents.error.empty_prompt")}

        from ..subagents.executor import run_subagent

        return run_subagent(agent, subagent, prompt, image=image)
