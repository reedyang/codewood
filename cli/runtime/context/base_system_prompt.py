"""Base system prompt part: the agent's foundational instructions."""

from __future__ import annotations

from typing import Any

from .base import ModelContextPart


class BaseSystemPromptPart(ModelContextPart):
    """The agent's pre-composed base system prompt (``agent._base_system_prompt``)."""

    name = "base_system_prompt"
    order = 10

    def render(self, agent: Any, include_tools: bool) -> str:
        return str(getattr(agent, "_base_system_prompt", "") or "")
