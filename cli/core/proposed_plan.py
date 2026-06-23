"""Helpers for the Plan-mode ``<proposed_plan>`` block.

In Plan mode the model presents its finished plan wrapped in a
``<proposed_plan>...</proposed_plan>`` block (see
``cli/prompts/collaboration-mode/plan.md``). The block is:

* extracted so the host can cache the plan markdown (for the execute chooser and
  the optional clear-context implementation path), and
* stripped from the user-visible assistant text so the plan renders once as a
  dedicated card rather than twice (inline + card).

The raw assistant text (tags included) stays in chat history untouched, so a
"keep context" execution can rely on the plan already being in the transcript.
These helpers are pure/stateless for easy reuse and testing.
"""

from __future__ import annotations

import re
from typing import List, Optional

PROPOSED_PLAN_OPEN_TAG = "<proposed_plan>"
PROPOSED_PLAN_CLOSE_TAG = "</proposed_plan>"

# Match a full block; DOTALL so the body can span lines. Non-greedy so multiple
# blocks (should not happen, but be safe) are matched individually.
_PROPOSED_PLAN_RE = re.compile(
    r"<proposed_plan>\s*(.*?)\s*</proposed_plan>",
    re.DOTALL | re.IGNORECASE,
)


def has_proposed_plan(text: str) -> bool:
    """True when ``text`` contains a complete ``<proposed_plan>`` block."""
    if not isinstance(text, str) or PROPOSED_PLAN_OPEN_TAG not in text:
        return False
    return _PROPOSED_PLAN_RE.search(text) is not None


def extract_proposed_plans(text: str) -> List[str]:
    """Return the markdown body of each ``<proposed_plan>`` block in order."""
    if not isinstance(text, str) or PROPOSED_PLAN_OPEN_TAG not in text:
        return []
    return [m.group(1).strip() for m in _PROPOSED_PLAN_RE.finditer(text)]


def latest_proposed_plan(text: str) -> Optional[str]:
    """Return the last ``<proposed_plan>`` body, or ``None`` when absent.

    Plan mode replaces the whole plan each turn, so the last block is the
    authoritative one.
    """
    plans = extract_proposed_plans(text)
    return plans[-1] if plans else None


def strip_proposed_plan_blocks(text: str) -> str:
    """Remove ``<proposed_plan>`` blocks from user-visible assistant text.

    Collapses the surrounding blank lines the removal would otherwise leave so
    the visible bubble reads cleanly. Returns the input unchanged when no block
    is present.
    """
    if not isinstance(text, str) or PROPOSED_PLAN_OPEN_TAG not in text:
        return text
    stripped = _PROPOSED_PLAN_RE.sub("", text)
    # Collapse 3+ newlines left by the removal into a single blank line.
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()
