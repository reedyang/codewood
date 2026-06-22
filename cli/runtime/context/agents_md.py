"""AGENTS.md part: user-defined custom prompts from config/project dirs."""

from __future__ import annotations

from typing import Any

from .base import ModelContextPart


class AgentsMdPart(ModelContextPart):
    """Injects ``AGENTS.md`` / ``AGENTS.override.md`` content."""

    name = "agents_md"
    order = 20

    def render(self, agent: Any, include_tools: bool) -> str:
        from .. import prompt_composer

        return prompt_composer.build_agents_md_system_append(agent)
