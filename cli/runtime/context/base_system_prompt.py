"""Base system prompt part: the agent's foundational instructions.

This part owns the construction of the base system prompt: it reads
``prompts/system_prompt.md`` and substitutes app-name placeholders. The result
is cached on the agent (``agent._base_system_prompt``) so it is built once and
so callers/tests can still pre-seed or read the attribute directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...config.app_info import get_app_name, get_app_slug_kebab
from .base import ModelContextPart


def build_base_system_prompt() -> str:
    """Read and render the base system prompt template from disk."""
    prompt_path = Path(__file__).resolve().parents[2] / "prompts" / "system_prompt.md"
    with open(prompt_path, "r", encoding="utf-8") as f:
        return (
            f.read()
            .replace("{{APP_NAME}}", get_app_name())
            .replace("{{APP_SLUG_KEBAB}}", get_app_slug_kebab())
        )


class BaseSystemPromptPart(ModelContextPart):
    """The agent's base system prompt, built from the prompt template on demand."""

    name = "base_system_prompt"
    order = 10

    def render(self, agent: Any, include_tools: bool) -> str:
        cached = getattr(agent, "_base_system_prompt", None)
        if cached:
            return str(cached)
        rendered = build_base_system_prompt()
        try:
            agent._base_system_prompt = rendered
        except Exception:
            pass
        return rendered
