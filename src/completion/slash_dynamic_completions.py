import re
from typing import Any, Dict, List, Set, Tuple


def _sorted_unique_ci(values: List[str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for raw in values or []:
        value = str(raw or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return sorted(out, key=str.lower)


def _unique_ci_preserve_order(values: List[str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for raw in values or []:
        value = str(raw or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _get_mcp_servers(mcp_config: Any) -> Dict[str, Any]:
    try:
        servers = (mcp_config or {}).get("mcpServers", {})
    except Exception:
        servers = {}
    return servers if isinstance(servers, dict) else {}


def build_workspace_action_commands(
    workspaces_state: Dict[str, Any], action: str
) -> List[str]:
    commands: List[str] = []
    workspaces = (workspaces_state or {}).get("workspaces", {})
    if not isinstance(workspaces, dict):
        return commands
    for entry in workspaces.values():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        commands.append(f"/workspace {action} {name}")
    return _sorted_unique_ci(commands)


def build_mcp_server_commands(mcp_config: Any) -> List[str]:
    commands = [f"/{name.strip()}/" for name in _get_mcp_servers(mcp_config).keys() if str(name or "").strip()]
    return _sorted_unique_ci(commands)


def build_mcp_server_target_commands(
    mcp_config: Any, subcommand: str, with_trailing_space: bool = False
) -> List[str]:
    commands: List[str] = []
    suffix = " " if with_trailing_space else ""
    for name in _get_mcp_servers(mcp_config).keys():
        server = str(name or "").strip()
        if not server:
            continue
        commands.append(f"/mcp {subcommand} {server}{suffix}")
    return _sorted_unique_ci(commands)


def build_mcp_scoped_commands(mcp_manager: Any) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()

    def _add(cmd: str) -> None:
        value = str(cmd or "").strip()
        if not value.startswith("/"):
            return
        key = value.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(value)

    servers = _get_mcp_servers(getattr(mcp_manager, "mcp_config", {}))
    tools_cache = getattr(mcp_manager, "_tools_cache", {}) or {}
    prompts_cache = getattr(mcp_manager, "_prompts_cache", {}) or {}
    clients = getattr(mcp_manager, "_clients", {}) or {}
    status_map = getattr(mcp_manager, "_status", {}) or {}

    for server in servers.keys():
        srv = str(server or "").strip()
        if not srv:
            continue

        st = {}
        try:
            st = status_map.get(srv, {}) if isinstance(status_map, dict) else {}
        except Exception:
            st = {}
        state = str((st or {}).get("state", "")).strip().lower()
        has_client = bool(isinstance(clients, dict) and srv in clients)
        has_cache = bool(
            (isinstance(tools_cache, dict) and srv in tools_cache)
            or (isinstance(prompts_cache, dict) and srv in prompts_cache)
        )
        is_loaded = has_client or has_cache or state in ("success", "loading")
        if not is_loaded:
            continue

        _add(f"/{srv}/")

        tool_items = []
        try:
            if isinstance(tools_cache, dict):
                tool_items = tools_cache.get(srv, {}).get("tools", [])
        except Exception:
            tool_items = []
        if isinstance(tool_items, list):
            for tool in tool_items:
                name = (
                    str((tool or {}).get("name", "")).strip()
                    if isinstance(tool, dict)
                    else ""
                )
                if name:
                    _add(f"/{srv}/{name}")

        prompt_items = []
        try:
            if isinstance(prompts_cache, dict):
                prompt_items = prompts_cache.get(srv, {}).get("prompts", [])
        except Exception:
            prompt_items = []
        if isinstance(prompt_items, list):
            for prompt in prompt_items:
                name = (
                    str((prompt or {}).get("name", "")).strip()
                    if isinstance(prompt, dict)
                    else ""
                )
                if name:
                    _add(f"/{srv}/{name}")

    return sorted(out, key=str.lower)


def build_mcp_scoped_groups(mcp_manager: Any) -> List[Tuple[str, List[str]]]:
    buckets: Dict[str, List[str]] = {}
    seen_by_trigger: Dict[str, Set[str]] = {}

    for cmd in build_mcp_scoped_commands(mcp_manager):
        if not isinstance(cmd, str):
            continue
        match = re.match(r"^/([^/]+)/([^/].*)$", cmd)
        if not match:
            continue
        server = str(match.group(1) or "").strip()
        name = str(match.group(2) or "").strip()
        if not server or not name:
            continue
        trigger = f"/{server}/"
        if trigger not in buckets:
            buckets[trigger] = []
            seen_by_trigger[trigger] = set()
        key = cmd.lower()
        if key in seen_by_trigger[trigger]:
            continue
        seen_by_trigger[trigger].add(key)
        buckets[trigger].append(cmd)

    groups: List[Tuple[str, List[str]]] = []
    for trigger, candidates in buckets.items():
        if candidates:
            groups.append((trigger, sorted(candidates, key=str.lower)))
    return sorted(groups, key=lambda x: x[0].lower())


def build_model_switch_commands(model_selectors: List[str]) -> List[str]:
    commands: List[str] = []
    for raw in model_selectors or []:
        selector = str(raw or "").strip()
        if not selector:
            continue
        commands.append(f"/model {selector}")
    return _unique_ci_preserve_order(commands)


def build_slash_dynamic_rules(
    workspaces_state: Dict[str, Any],
    mcp_config: Any,
    mcp_scoped_groups_provider: Any,
    model_selectors_provider: Any = None,
) -> List[Dict[str, Any]]:
    model_commands: List[str] = []
    try:
        if callable(model_selectors_provider):
            model_commands = build_model_switch_commands(model_selectors_provider())
    except Exception:
        model_commands = []

    return [
        {
            "trigger": "/mcp server-info ",
            "candidates": build_mcp_server_target_commands(mcp_config, "server-info"),
        },
        {
            "trigger": "/mcp reconnect ",
            "candidates": build_mcp_server_target_commands(mcp_config, "reconnect"),
        },
        {
            "trigger": "/mcp list-tools ",
            "candidates": build_mcp_server_target_commands(mcp_config, "list-tools"),
        },
        {
            "trigger": "/mcp list-resources ",
            "candidates": build_mcp_server_target_commands(mcp_config, "list-resources"),
        },
        {
            "trigger": "/mcp list-resource-templates ",
            "candidates": build_mcp_server_target_commands(
                mcp_config, "list-resource-templates"
            ),
        },
        {
            "trigger": "/mcp list-prompts ",
            "candidates": build_mcp_server_target_commands(mcp_config, "list-prompts"),
        },
        {
            "trigger": "/mcp disable-tools ",
            "candidates": build_mcp_server_target_commands(
                mcp_config, "disable-tools", with_trailing_space=True
            ),
        },
        {
            "trigger": "/mcp enable-tools ",
            "candidates": build_mcp_server_target_commands(
                mcp_config, "enable-tools", with_trailing_space=True
            ),
        },
        {
            "trigger": "/workspace switch ",
            "candidates": build_workspace_action_commands(workspaces_state, "switch"),
        },
        {
            "trigger": "/workspace delete ",
            "candidates": build_workspace_action_commands(workspaces_state, "delete"),
        },
        {
            "trigger": "/model ",
            "candidates": model_commands,
        },
        {
            "groups_provider": mcp_scoped_groups_provider,
        },
    ]
