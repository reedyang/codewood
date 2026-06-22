"""Runtime cache hint part: workspace-level cache directory guidance."""

from __future__ import annotations

from typing import Any

from .base import ModelContextPart


class RuntimeCachePart(ModelContextPart):
    """Injects the generic runtime cache-dir hint for skills/scripts."""

    name = "runtime_cache"
    order = 70

    def render(self, agent: Any, include_tools: bool) -> str:
        from .. import prompt_composer

        return prompt_composer.build_runtime_cache_prompt_append(
            agent, default_workspace_id="default"
        )
