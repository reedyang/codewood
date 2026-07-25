"""Collaboration-mode constants and helpers.

Mode rules are now part of the static system prompt (``system_prompt.md``).
This module keeps the mode constants and helpers used by the UI layer.
"""

from __future__ import annotations

from typing import Any, Dict

MODE_AGENT = "agent"
MODE_PLAN = "plan"

KNOWN_MODES = (MODE_AGENT, MODE_PLAN)

MODE_DISPLAY_NAMES: Dict[str, str] = {
    MODE_AGENT: "Agent",
    MODE_PLAN: "Plan",
}


def normalize_mode(mode: Any) -> str:
    """Return a known mode string, defaulting to Agent for anything unknown."""
    value = str(mode or "").strip().lower()
    return value if value in KNOWN_MODES else MODE_AGENT


def known_mode_names() -> str:
    """Comma-separated display names."""
    return ", ".join(MODE_DISPLAY_NAMES[m] for m in KNOWN_MODES)


def active_mode_for_agent(agent: Any) -> str:
    """Resolve the agent's current collaboration mode from its sticky flag."""
    return MODE_PLAN if bool(getattr(agent, "_plan_mode_sticky", False)) else MODE_AGENT
