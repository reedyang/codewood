# REPL: user input starting with "/" is dispatched as built-in or shell (see agent.run).
# English command names only (no Chinese aliases). Keep in sync with built-in branches there.

from __future__ import annotations

from typing import List, Optional, Tuple

SLASH_BUILTIN_COMMANDS: List[str] = [
    "/always_confirm-reset",
    "/chat ",
    "/chat current",
    "/chat delete ",
    "/chat delete all",
    "/chat list",
    "/chat new ",
    "/chat reload",
    "/chat rename ",
    "/chat switch ",
    "/clear context",
    "/clear input history",
    "/clear screen",
    "/execution-policy ",
    "/execution-policy confirmation",
    "/execution-policy moderate",
    "/execution-policy show",
    "/execution-policy unlimited",
    "/exit",
    "/help",
    "/mcp ",
    "/mcp disable-tools ",
    "/mcp enable-tools ",
    "/mcp list-disabled-tools",
    "/mcp list-prompts ",
    "/mcp list-resource-templates ",
    "/mcp list-resources ",
    "/mcp list-tools ",
    "/mcp reload-config",
    "/mcp reconnect ",
    "/mcp server-info ",
    "/mcp status",
    "/mcp status-refresh",
    "/mcp/",
    "/memory ",
    "/memory delete ",
    "/memory disable",
    "/memory enable",
    "/memory list",
    "/memory remember ",
    "/memory search ",
    "/memory stats",
    "/memory status",
    "/model",
    "/quit",
    "/session-summary ",
    "/session-summary off",
    "/session-summary on",
    "/session-summary show",
    "/workspace ",
    "/workspace create ",
    "/workspace current",
    "/workspace delete ",
    "/workspace help",
    "/workspace list",
    "/workspace rename ",
    "/workspace switch ",
    "/workspace update ",
]

# Keep built-in completion list unique while preserving order.
_seen_builtin = set()
_deduped_builtin: List[str] = []
for _cmd in SLASH_BUILTIN_COMMANDS:
    _key = _cmd.lower()
    if _key in _seen_builtin:
        continue
    _seen_builtin.add(_key)
    _deduped_builtin.append(_cmd)
SLASH_BUILTIN_COMMANDS = _deduped_builtin


def slash_builtin_completions(
    prefix_from_slash: str,
    dynamic_commands: Optional[List[str]] = None,
    delayed_dynamic_groups: Optional[List[Tuple[str, List[str]]]] = None,
) -> List[str]:
    """
    prefix_from_slash: text from the first '/' through the cursor (e.g. '/', '/he', '/memory ').
    Returns matching full '/...' commands, sorted, deduplicated.
    """
    if not prefix_from_slash.startswith("/"):
        return []
    pl = prefix_from_slash.lower()
    seen = set()
    out: List[str] = []
    all_commands = list(SLASH_BUILTIN_COMMANDS)
    if dynamic_commands:
        all_commands.extend(dynamic_commands)
    if delayed_dynamic_groups:
        for trigger_prefix, candidates in delayed_dynamic_groups:
            trigger = str(trigger_prefix or "").lower()
            if not trigger:
                continue
            # Delayed dynamic completions are shown only after the trigger prefix
            # is fully typed (e.g. "/workspace switch " with trailing space).
            if not pl.startswith(trigger):
                continue
            if candidates:
                all_commands.extend(candidates)

    for c in all_commands:
        if c.lower().startswith(pl):
            key = c.lower()
            if key not in seen:
                seen.add(key)
                out.append(c)
    # Keep '/model ' candidates in configured order (from config model_providers).
    if not pl.startswith("/model "):
        out.sort(key=str.lower)
    return out

