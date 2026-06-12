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
    "/chat edit ",
    "/chat fork",
    "/chat list",
    "/chat new ",
    "/chat reload",
    "/chat rename ",
    "/chat switch ",
    "/compact",
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
    "/language",
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

# Optional display labels for built-in completions whose menu text should differ
# from the inserted text. The key is the inserted command (as listed above);
# the value is what the completion menu shows. This lets a command advertise its
# argument shape while tab still inserts the bare command.
#
# Convention: required arguments are wrapped in <>, optional arguments in [].
# Only commands that accept arguments need an entry here.
SLASH_BUILTIN_DISPLAY_OVERRIDES = {
    # /chat
    "/chat new ": "/chat new [name]",
    "/chat switch ": "/chat switch <index|id|name>",
    "/chat rename ": "/chat rename <index|id|name> <new name>",
    "/chat edit ": "/chat edit <index>",
    "/chat fork": "/chat fork [index]",
    "/chat delete ": "/chat delete <index|id|name>",
    # /execution-policy
    "/execution-policy ": "/execution-policy <show|unlimited|moderate|confirmation>",
    # /language
    "/language": "/language [language code]",
    # /model
    "/model": "/model [model_provider:name]",
    # /mcp
    "/mcp list-tools ": "/mcp list-tools <server>",
    "/mcp list-resources ": "/mcp list-resources <server>",
    "/mcp list-resource-templates ": "/mcp list-resource-templates <server>",
    "/mcp list-prompts ": "/mcp list-prompts <server>",
    "/mcp list-disabled-tools": "/mcp list-disabled-tools [server]",
    "/mcp reconnect ": "/mcp reconnect <server>",
    "/mcp server-info ": "/mcp server-info <server>",
    "/mcp disable-tools ": "/mcp disable-tools <server> <tool1,tool2>",
    "/mcp enable-tools ": "/mcp enable-tools <server> <tool1,tool2>",
    # /memory
    "/memory search ": "/memory search <query>",
    "/memory remember ": "/memory remember <text>",
    "/memory delete ": "/memory delete <id>",
    # /workspace
    "/workspace create ": "/workspace create <path> [--name <name>]",
    "/workspace switch ": "/workspace switch <name|id|path>",
    "/workspace rename ": "/workspace rename <name|id|path> <new name>",
    "/workspace update ": "/workspace update <name|id|path> [--name <name>] [--path <path>]",
    "/workspace delete ": "/workspace delete <name|id|path> [--remove-files]",
}


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

