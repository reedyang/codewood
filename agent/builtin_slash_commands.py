# Windows REPL: user input starting with "/" is dispatched as built-in or shell (see smart_shell_agent.run).
# English command names only (no Chinese aliases). Keep in sync with built-in branches there.

from __future__ import annotations

from typing import List

WINDOWS_SLASH_BUILTIN_COMMANDS: List[str] = [
    "/exit",
    "/quit",
    "/cls",
    "/clear screen",
    "/clear history",
    "/clear context",
    "/knowledge on",
    "/knowledge off",
    "/knowledge sync",
    "/knowledge stats",
    "/knowledge search ",
    "/freedom on",
    "/freedom off",
    "/help",
]


def windows_slash_builtin_completions(prefix_from_slash: str) -> List[str]:
    """
    prefix_from_slash: text from the first '/' through the cursor (e.g. '/', '/he', '/knowledge ').
    Returns matching full '/...' commands, sorted, deduplicated.
    """
    if not prefix_from_slash.startswith("/"):
        return []
    pl = prefix_from_slash.lower()
    seen = set()
    out: List[str] = []
    for c in WINDOWS_SLASH_BUILTIN_COMMANDS:
        if c.lower().startswith(pl):
            if c not in seen:
                seen.add(c)
                out.append(c)
    out.sort(key=str.lower)
    return out
