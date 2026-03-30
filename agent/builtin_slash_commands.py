# Windows REPL: user input starting with "/" is dispatched as built-in or shell (see smart_shell_agent.run).
# English command names only (no Chinese aliases). Keep in sync with built-in branches there.

from __future__ import annotations

from typing import List, Optional

WINDOWS_SLASH_BUILTIN_COMMANDS: List[str] = [
    "/exit",
    "/quit",
    "/cls",
    "/clear-screen",
    "/clear-history",
    "/clear-context",
    "/knowledge-on",
    "/knowledge-off",
    "/knowledge-sync",
    "/knowledge-stats",
    "/knowledge-search ",
    "/freedom-on",
    "/freedom-off",
    "/always_confirm-reset",
    "/mcp-reload-config",
    "/mcp-status",
    "/mcp-status-refresh",
    "/mcp-reconnect ",
    "/mcp-server-info ",
    "/mcp-list-tools ",
    "/mcp-list-resources ",
    "/mcp-list-resource-templates ",
    "/mcp-list-prompts ",
    "/mcp-list-disabled-tools",
    "/mcp-disable-tools ",
    "/mcp-enable-tools ",
    "/help",
]


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
            if c not in seen:
                seen.add(c)
                out.append(c)
    out.sort(key=str.lower)
    return out
