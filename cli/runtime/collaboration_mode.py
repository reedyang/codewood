"""Collaboration-mode prompt loading and rendering.

Plan mode is modeled after Codex's "collaboration mode" design: instead of
appending a directive to every outgoing message, the active mode contributes a
dedicated ``<collaboration_mode>...</collaboration_mode>`` section to the system
prompt. The section body comes from a Markdown template under
``cli/prompts/collaboration-mode/<mode>.md`` (``agent.md`` / ``plan.md``), with a
small set of ``{{VAR}}`` placeholders substituted at runtime.

Keeping the templates on disk (rather than as localized strings) lets us track
the canonical wording in source control and edit it without touching code; the
runtime variables are the only dynamic part.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from ..config.app_info import get_app_prompt_name

MODE_AGENT = "agent"
MODE_PLAN = "plan"

#: Modes the user can switch between (drives ``{{KNOWN_MODE_NAMES}}``).
KNOWN_MODES = (MODE_AGENT, MODE_PLAN)

#: Human-facing display names per mode (used in prompts/UI).
MODE_DISPLAY_NAMES: Dict[str, str] = {
    MODE_AGENT: "Agent",
    MODE_PLAN: "Plan",
}

_COLLAB_OPEN_TAG = "<collaboration_mode>"
_COLLAB_CLOSE_TAG = "</collaboration_mode>"


def _prompts_dir() -> Path:
    """Absolute path to ``cli/prompts/collaboration-mode``."""
    return Path(__file__).resolve().parent.parent / "prompts" / "collaboration-mode"


def normalize_mode(mode: Any) -> str:
    """Return a known mode string, defaulting to Agent for anything unknown."""
    value = str(mode or "").strip().lower()
    return value if value in KNOWN_MODES else MODE_AGENT


def known_mode_names() -> str:
    """Comma-separated display names for the ``{{KNOWN_MODE_NAMES}}`` variable."""
    return ", ".join(MODE_DISPLAY_NAMES[m] for m in KNOWN_MODES)


def _runtime_variables() -> Dict[str, str]:
    """Map of ``{{VAR}}`` placeholder -> substitution value.

    Centralized so new variables only need an entry here; templates and this
    map are the single source of truth for what is interpolated.
    """
    return {
        "{{KNOWN_MODE_NAMES}}": known_mode_names(),
        "{{APP_NAME}}": get_app_prompt_name(),
    }


def load_mode_prompt(mode: str) -> str:
    """Read the raw Markdown template for ``mode`` (no substitution)."""
    path = _prompts_dir() / f"{normalize_mode(mode)}.md"
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def render_mode_prompt(mode: str) -> str:
    """Return the mode template with runtime ``{{VAR}}`` placeholders applied."""
    text = load_mode_prompt(mode)
    if not text:
        return ""
    for placeholder, value in _runtime_variables().items():
        text = text.replace(placeholder, value)
    return text


def active_mode_for_agent(agent: Any) -> str:
    """Resolve the agent's current collaboration mode from its sticky flag."""
    return MODE_PLAN if bool(getattr(agent, "_plan_mode_sticky", False)) else MODE_AGENT


def render_collaboration_mode_section(agent: Any) -> str:
    """Render the active mode's prompt wrapped in collaboration-mode tags.

    Returns an empty string when the template is missing/empty so the section
    is simply omitted rather than emitting bare tags.
    """
    body = render_mode_prompt(active_mode_for_agent(agent)).strip()
    if not body:
        return ""
    return f"{_COLLAB_OPEN_TAG}\n{body}\n{_COLLAB_CLOSE_TAG}"
