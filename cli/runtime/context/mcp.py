"""MCP part: configured MCP servers and their cached capabilities."""

from __future__ import annotations

from typing import Any

from .base import ModelContextPart


class McpPart(ModelContextPart):
    """Injects the MCP configuration / cached tool & resource catalog."""

    name = "mcp"
    order = 60

    def render(self, agent: Any, include_tools: bool) -> str:
        from .. import prompt_composer

        return prompt_composer.build_mcp_system_append(agent)
