"""Sub-agents part: lists available sub-agents for ``run_subagent``."""

from __future__ import annotations

from typing import Any

from .base import ModelContextPart


class SubagentsPart(ModelContextPart):
    """Injects the available sub-agents catalog."""

    name = "subagents"
    order = 50

    def render(self, agent: Any, include_tools: bool) -> str:
        from .. import prompt_composer

        return prompt_composer.build_subagents_system_append(agent)
