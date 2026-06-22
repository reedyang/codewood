"""Tool: run_subagent."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class RunSubagentTool(BaseTool):
    name = "run_subagent"
    description = "Delegate a self-contained subtask to a configured sub-agent. The sub-agent runs an isolated agentic loop with its own model, instructions, and tools, and returns a final text result that you should use to continue the main task. Choose the sub-agent whose description best matches the subtask. Sub-agents cannot call run_subagent themselves."
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
        from ._delegation import delegate_subagent

        return delegate_subagent(agent, "run_subagent", params if isinstance(params, dict) else {})
