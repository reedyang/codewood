"""User preferences part: persistent preferences injected before tools/MCP."""

from __future__ import annotations

from typing import Any

from .base import ModelContextPart


class UserPreferencesPart(ModelContextPart):
    """Injects persisted user preferences."""

    name = "user_preferences"
    order = 30

    def render(self, agent: Any, include_tools: bool) -> str:
        from .. import prompt_composer

        return prompt_composer.build_user_preferences_system_append(agent)
