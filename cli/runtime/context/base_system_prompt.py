"""Base system prompt part: the agent's foundational instructions.

This part owns the construction of the base system prompt: it reads
``prompts/system_prompt.md`` and substitutes app-name placeholders. The result
is cached on the agent (``agent._base_system_prompt``) so it is built once and
so callers/tests can still pre-seed or read the attribute directly.
"""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

from ...config.app_info import get_app_prompt_name, get_app_prompt_slug_kebab
from ..prompt_preprocessor import preprocess_prompt
from .base import ModelContextPart


def _prompts_root() -> Path:
    return Path(__file__).resolve().parents[2] / "prompts"


def build_base_system_prompt(variables: dict = None) -> str:
    """Read and render the base system prompt template from disk.

    *variables* provides additional ``[[if $var="val"]]`` substitution
    variables beyond the built-in ones (``os``).
    """
    prompt_path = _prompts_root() / "system_prompt.md"
    with open(prompt_path, "r", encoding="utf-8") as f:
        raw = f.read()
    merged_vars = {"os": platform.system()}
    if isinstance(variables, dict):
        merged_vars.update(variables)
    raw = preprocess_prompt(raw, merged_vars)
    return (
        raw
        .replace("{{APP_NAME}}", get_app_prompt_name())
        .replace("{{APP_SLUG_KEBAB}}", get_app_prompt_slug_kebab())
    )


class BaseSystemPromptPart(ModelContextPart):
    """The agent's base system prompt, built from the prompt template on demand.

    The base prompt is fully static (no mode-specific placeholders) so it can
    be cached once and reused across mode switches without invalidating the
    model's prefix cache.
    """

    name = "base_system_prompt"
    order = 10

    def render(self, agent: Any, include_tools: bool) -> str:
        pcs_enabled = str(getattr(agent, "project_context_search_enabled", True)).lower()
        return build_base_system_prompt(variables={"project_context_search_enabled": pcs_enabled})
