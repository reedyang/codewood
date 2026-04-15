# Windows REPL: user input starting with "/" is dispatched as built-in or shell (see smart_shell_agent.run).
# English command names only (no Chinese aliases). Keep in sync with built-in branches there.

from __future__ import annotations

from typing import List, Optional

WINDOWS_SLASH_BUILTIN_COMMANDS: List[str] = [
    "/exit",
    "/quit",
    "/cls",
    "/clear ",
    "/clear screen",
    "/clear history",
    "/clear context",
    "/knowledge ",
    "/knowledge status",
    "/knowledge sync",
    "/knowledge stats",
    "/knowledge search ",
    "/memory ",
    "/memory status",
    "/memory stats",
    "/memory list",
    "/memory search ",
    "/memory remember ",
    "/memory delete ",
    "/execution-policy",
    "/execution-policy show",
    "/execution-policy unlimited",
    "/execution-policy moderate",
    "/execution-policy confirmation",
    "/always_confirm-reset",
    "/mcp ",
    "/mcp status",
    "/mcp status-refresh",
    "/mcp reload-config",
    "/mcp reconnect ",
    "/mcp server-info ",
    "/mcp list-tools ",
    "/mcp list-resources ",
    "/mcp list-resource-templates ",
    "/mcp list-prompts ",
    "/mcp list-disabled-tools",
    "/mcp disable-tools ",
    "/mcp enable-tools ",
    "/help",
]

# Keep built-in completion list unique while preserving order.
_seen_builtin = set()
_deduped_builtin: List[str] = []
for _cmd in WINDOWS_SLASH_BUILTIN_COMMANDS:
    _key = _cmd.lower()
    if _key in _seen_builtin:
        continue
    _seen_builtin.add(_key)
    _deduped_builtin.append(_cmd)
WINDOWS_SLASH_BUILTIN_COMMANDS = _deduped_builtin


def windows_slash_builtin_completions(
    prefix_from_slash: str, dynamic_commands: Optional[List[str]] = None
) -> List[str]:
    """
    prefix_from_slash: text from the first '/' through the cursor (e.g. '/', '/he', '/knowledge ').
    Returns matching full '/...' commands, sorted, deduplicated.
    """
    if not prefix_from_slash.startswith("/"):
        return []
    pl = prefix_from_slash.lower()
    seen = set()
    out: List[str] = []
    all_commands = list(WINDOWS_SLASH_BUILTIN_COMMANDS)
    if dynamic_commands:
        all_commands.extend(dynamic_commands)

    for c in all_commands:
        if c.lower().startswith(pl):
            key = c.lower()
            if key not in seen:
                seen.add(key)
                out.append(c)
    out.sort(key=str.lower)
    return out
