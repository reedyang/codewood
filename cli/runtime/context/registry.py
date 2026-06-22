"""Ordered registry of model-context parts.

The order here (and each part's ``order`` attribute) defines the section
sequence in the composed system prompt. The historical ordering is preserved:
base prompt, AGENTS.md, user preferences, tools, sub-agents, MCP, runtime
cache hint.
"""

from __future__ import annotations

from typing import List

from .base import ModelContextPart
from .base_system_prompt import BaseSystemPromptPart
from .agents_md import AgentsMdPart
from .user_preferences import UserPreferencesPart
from .tools import ToolsPart
from .subagents import SubagentsPart
from .mcp import McpPart
from .runtime_cache import RuntimeCachePart


def ordered_context_parts() -> List[ModelContextPart]:
    """Return the model-context parts sorted by their ``order`` attribute."""
    parts: List[ModelContextPart] = [
        BaseSystemPromptPart(),
        AgentsMdPart(),
        UserPreferencesPart(),
        ToolsPart(),
        SubagentsPart(),
        McpPart(),
        RuntimeCachePart(),
    ]
    return sorted(parts, key=lambda p: p.order)
