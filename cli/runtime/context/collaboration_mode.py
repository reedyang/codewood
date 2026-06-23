"""Collaboration-mode part: the active mode's instructions (Agent/Plan).

Renders the ``<collaboration_mode>...</collaboration_mode>`` section that tells
the model which collaboration mode is active and what its rules are. The body is
loaded from ``cli/prompts/collaboration-mode/<mode>.md`` and changes when the
user toggles Plan mode, so this section is the single place mode instructions
enter the model context (replacing the old per-message directive append).
"""

from __future__ import annotations

from typing import Any

from .base import ModelContextPart


class CollaborationModePart(ModelContextPart):
    """Injects the active collaboration mode's instruction block."""

    name = "collaboration_mode"
    order = 15

    def render(self, agent: Any, include_tools: bool) -> str:
        from ..collaboration_mode import render_collaboration_mode_section

        section = render_collaboration_mode_section(agent)
        if not section:
            return ""
        return f"\n\n{section}"
