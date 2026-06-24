"""Browser part: notes the GUI embedded browser and its tools to the model.

Only contributes text when running under the desktop GUI (the serve app sets
``agent._browser_dispatch``); in the TUI / headless modes it renders nothing.
"""

from __future__ import annotations

from typing import Any

from .base import ModelContextPart


class BrowserPart(ModelContextPart):
    """Injects a short description of the embedded browser and its tools."""

    name = "browser"
    order = 55
    requires_tools = True

    def render(self, agent: Any, include_tools: bool) -> str:
        from .. import prompt_composer

        return prompt_composer.build_browser_system_append(agent)
