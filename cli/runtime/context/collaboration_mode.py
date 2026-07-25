"""Collaboration-mode part: no-op — mode rules now live in the static system prompt."""

from __future__ import annotations

from typing import Any

from .base import ModelContextPart


class CollaborationModePart(ModelContextPart):
    name = "collaboration_mode"
    order = 15

    def render(self, agent: Any, include_tools: bool) -> str:
        return ""
