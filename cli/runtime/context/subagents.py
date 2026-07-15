"""Sub-agents part: lists available sub-agents for ``run_subagent``."""

from __future__ import annotations

from typing import Any, List

from .base import ModelContextPart


class SubagentsPart(ModelContextPart):
    """Injects the available sub-agents catalog."""

    name = "subagents"
    order = 50

    def render(self, agent: Any, include_tools: bool) -> str:
        """List available sub-agents so the main model knows when to call run_subagent."""
        subagents = list(getattr(agent, "subagents", []) or [])
        if not subagents:
            return ""
        lines: List[str] = [
            "",
            "",
            "## Sub-agents",
            "The following sub-agents are available. Each runs an isolated agentic loop with its own "
            "model, instructions, and tools, and returns a final text result.",
            "When a subtask matches a sub-agent's description, call the `run_subagent` tool with that "
            "sub-agent's `name` and a complete, self-contained `prompt`. Then use the returned output to "
            "continue the main task. Sub-agents cannot invoke `run_subagent` themselves.",
            # Language requirement for the delegated prompt: the model must match
            # the language the user is actually writing in (auto-detected from the
            # user's message), so the sub-agent receives and answers the subtask in
            # the same tongue as the conversation.
            "Language: write the `prompt` (and `topic`) arguments in the SAME language the user is using "
            "in their message. Do not translate or switch languages when delegating a subtask.",
            "Available sub-agents:",
        ]
        for rec in subagents:
            model_label = (str(getattr(rec, "model_selector", "") or "").strip()) or "main model"
            lines.append(f"- {rec.name} (model: {model_label}): {rec.description}")
        return "\n".join(lines)
