"""Tools part: the tool catalog and tool-call-mode guidance."""

from __future__ import annotations

from typing import Any

from .base import ModelContextPart


class ToolsPart(ModelContextPart):
    """Injects the tool catalog. Only present when ``include_tools`` is set."""

    name = "tools"
    order = 40
    requires_tools = True

    def render(self, agent: Any, include_tools: bool) -> str:
        from .. import prompt_composer

        # Preserve the historical leading newline before the tools block.
        return "\n" + prompt_composer.build_tools_prompt_append(agent)
