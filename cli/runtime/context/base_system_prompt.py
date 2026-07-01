"""Base system prompt part: the agent's foundational instructions.

This part owns the construction of the base system prompt: it reads
``prompts/system_prompt.md`` and substitutes app-name placeholders. The result
is cached on the agent (``agent._base_system_prompt``) so it is built once and
so callers/tests can still pre-seed or read the attribute directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...config.app_info import get_app_prompt_name, get_app_prompt_slug_kebab
from .base import ModelContextPart


def _prompts_root() -> Path:
    return Path(__file__).resolve().parents[2] / "prompts"


def build_base_system_prompt(small_model: bool = False) -> str:
    """Read and render the base system prompt template from disk.

    When *small_model* is True, loads ``prompts/small/system_prompt.md``
    instead of the default.
    """
    if small_model:
        prompt_path = _prompts_root() / "small" / "system_prompt.md"
    else:
        prompt_path = _prompts_root() / "system_prompt.md"
    with open(prompt_path, "r", encoding="utf-8") as f:
        return (
            f.read()
            .replace("{{APP_NAME}}", get_app_prompt_name())
            .replace("{{APP_SLUG_KEBAB}}", get_app_prompt_slug_kebab())
        )


class BaseSystemPromptPart(ModelContextPart):
    """The agent's base system prompt, built from the prompt template on demand."""

    name = "base_system_prompt"
    order = 10

    def render(self, agent: Any, include_tools: bool) -> str:
        cached = getattr(agent, "_base_system_prompt", None)
        if cached:
            return str(cached)
        small_model = bool(getattr(agent, "_small_model", False))
        rendered = build_base_system_prompt(small_model=small_model)
        try:
            agent._base_system_prompt = rendered
        except Exception:
            pass
        return rendered
