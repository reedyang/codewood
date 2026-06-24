"""Console part: notes the GUI embedded console and its tools to the model.

Only contributes text when running under the desktop GUI (the serve app sets
``agent._console_dispatch``); in the TUI / headless modes it renders nothing.
"""

from __future__ import annotations

from typing import Any

from .base import ModelContextPart


class ConsolePart(ModelContextPart):
    """Injects a short description of the embedded console and its tools."""

    name = "console"
    order = 56
    requires_tools = True

    def render(self, agent: Any, include_tools: bool) -> str:
        from .. import prompt_composer

        return prompt_composer.build_console_system_append(agent)
