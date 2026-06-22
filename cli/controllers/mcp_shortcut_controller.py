from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def _t(agent: Any, key: str, **kwargs: Any) -> str:
    from ..core.localization import get_display_language, translate

    return translate(key, get_display_language(agent), **kwargs)


def _err(key: str, **kwargs: Any) -> Dict[str, Any]:
    return {"key": key, "kwargs": kwargs}


def format_mcp_shortcut_error(agent: Any, error: Any) -> str:
    if isinstance(error, dict):
        return _t(agent, str(error.get("key") or ""), **dict(error.get("kwargs") or {}))
    return str(error or "")


def parse_mcp_shortcut_command(
    builtin_line: str,
) -> Tuple[Optional[str], Dict[str, Any], Optional[Any]]:
    """
    Parse '/mcp ...' shortcuts into tool calls.
    Rules:
    - Only required parameters are accepted.
    - Optional parameters are not supported in shortcuts.
    - For 'mcp_list_disabled_tools', server is optional.
    """
    raw = (builtin_line or "").strip()
    if not raw:
        return None, {}, _err("mcp.shortcut.error.empty_command")
    parts = raw.split()
    low = [p.lower() for p in parts]
    if not low:
        return None, {}, None
    if low[0] != "mcp":
        return None, {}, None
    if len(parts) < 2:
        return None, {}, _err("mcp.shortcut.usage.root")
    cmd = low[1]

    if cmd == "reload-config" and len(parts) == 2:
        return "mcp_reload_config", {}, None
    if cmd == "reload-config" and len(parts) != 2:
        return None, {}, _err("mcp.shortcut.usage.reload_config")
    if cmd == "status" and len(parts) == 2:
        return "mcp_status", {}, None
    if cmd == "status" and len(parts) != 2:
        return None, {}, _err("mcp.shortcut.usage.status")
    if cmd == "status-refresh" and len(parts) == 2:
        return "mcp_status_refresh", {}, None
    if cmd == "status-refresh" and len(parts) != 2:
        return None, {}, _err("mcp.shortcut.usage.status_refresh")
    if cmd == "reconnect" and len(parts) == 3:
        return "mcp_reconnect", {"server": parts[2]}, None
    if cmd == "reconnect":
        return None, {}, _err("mcp.shortcut.usage.reconnect")
    if cmd == "server-info" and len(parts) == 3:
        return "mcp_server_info", {"server": parts[2]}, None
    if cmd == "server-info":
        return None, {}, _err("mcp.shortcut.usage.server_info")
    if cmd == "list-tools" and len(parts) == 3:
        return "mcp_list_tools", {"server": parts[2]}, None
    if cmd == "list-tools":
        return None, {}, _err("mcp.shortcut.usage.list_tools")
    if cmd == "list-resources" and len(parts) == 3:
        return "mcp_list_resources", {"server": parts[2]}, None
    if cmd == "list-resources":
        return None, {}, _err("mcp.shortcut.usage.list_resources")
    if cmd == "list-resource-templates" and len(parts) == 3:
        return "mcp_list_resource_templates", {"server": parts[2]}, None
    if cmd == "list-resource-templates":
        return None, {}, _err("mcp.shortcut.usage.list_resource_templates")
    if cmd == "list-prompts" and len(parts) == 3:
        return "mcp_list_prompts", {"server": parts[2]}, None
    if cmd == "list-prompts":
        return None, {}, _err("mcp.shortcut.usage.list_prompts")
    if cmd == "list-disabled-tools":
        if len(parts) == 2:
            return "mcp_list_disabled_tools", {}, None
        if len(parts) == 3:
            return "mcp_list_disabled_tools", {"server": parts[2]}, None
        return None, {}, _err("mcp.shortcut.usage.list_disabled_tools")

    if cmd == "disable-tools" and len(parts) >= 4:
        server = parts[2]
        tools_csv = " ".join(parts[3:]).strip()
        tools = [x.strip() for x in tools_csv.split(",") if x.strip()]
        if not tools:
            return (
                None,
                {},
                _err("mcp.shortcut.error.missing_tools_parameter_disable"),
            )
        return "mcp_disable_tools", {"server": server, "tools": tools}, None
    if cmd == "disable-tools":
        return None, {}, _err("mcp.shortcut.usage.disable_tools")

    if cmd == "enable-tools" and len(parts) >= 4:
        server = parts[2]
        tools_csv = " ".join(parts[3:]).strip()
        tools = [x.strip() for x in tools_csv.split(",") if x.strip()]
        if not tools:
            return (
                None,
                {},
                _err("mcp.shortcut.error.missing_tools_parameter_enable"),
            )
        return "mcp_enable_tools", {"server": server, "tools": tools}, None
    if cmd == "enable-tools":
        return None, {}, _err("mcp.shortcut.usage.enable_tools")

    return None, {}, _err("mcp.shortcut.error.invalid_command")


def mcp_item_label(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    for k in ("display_name", "name", "uri", "id", "title"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return str(item)


def print_mcp_shortcut_result(
    agent: Any,
    tool_name: str,
    args: Dict[str, Any],
    result: Dict[str, Any],
) -> None:
    print(_t(agent, "mcp.shortcut.result_header"))
    print(_t(agent, "mcp.shortcut.command", tool_name=tool_name))
    if not result.get("success", False):
        print(_t(agent, "mcp.shortcut.status_failed"))
        print(_t(agent, "mcp.shortcut.error", error=result.get("error", "Unknown error")))
        print(_t(agent, "mcp.shortcut.result_footer"))
        return

    print(_t(agent, "mcp.shortcut.status_ok"))
    if tool_name == "mcp_reload_config":
        print(_t(agent, "mcp.shortcut.changed", changed=bool(result.get("changed", False))))
        summary = result.get("summary", {}) if isinstance(result.get("summary"), dict) else {}
        added = ", ".join(summary.get("added", [])) or "None"
        changed = ", ".join(summary.get("changed", [])) or "None"
        removed = ", ".join(summary.get("removed", [])) or "None"
        print(_t(agent, "mcp.shortcut.added", value=added))
        print(_t(agent, "mcp.shortcut.updated", value=changed))
        print(_t(agent, "mcp.shortcut.removed", value=removed))
    elif tool_name in ("mcp_status", "mcp_status_refresh"):
        status = result.get("status", {}) if isinstance(result.get("status"), dict) else {}
        print(_t(agent, "mcp.shortcut.total", value=status.get("total", 0)))
        print(_t(agent, "mcp.shortcut.success", value=status.get("success", 0)))
        print(_t(agent, "mcp.shortcut.failed", value=status.get("failed", 0)))
        print(_t(agent, "mcp.shortcut.loading", value=status.get("loading_count", 0)))
        print(_t(agent, "mcp.shortcut.loaded", value=status.get("all_loaded", False)))
        servers = status.get("servers", {}) if isinstance(status.get("servers"), dict) else {}
        if servers:
            print(_t(agent, "mcp.shortcut.servers"))
            for s, st in servers.items():
                if not isinstance(st, dict):
                    continue
                print(
                    _t(
                        agent,
                        "mcp.shortcut.server_state",
                        server=s,
                        state=st.get("state", ""),
                        tools=st.get("tool_count", 0),
                        source=st.get("source", ""),
                    )
                )
    elif tool_name == "mcp_reconnect":
        print(_t(agent, "mcp.shortcut.server", value=result.get("server", args.get("server", ""))))
        print(_t(agent, "mcp.shortcut.source", value=result.get("source", "")))
        print(_t(agent, "mcp.shortcut.tools_count", value=result.get("count", 0)))
    elif tool_name == "mcp_server_info":
        info = result.get("info", {}) if isinstance(result.get("info"), dict) else {}
        status = info.get("status", {}) if isinstance(info.get("status"), dict) else {}
        print(_t(agent, "mcp.shortcut.server", value=result.get("server", args.get("server", ""))))
        print(_t(agent, "mcp.shortcut.state", value=status.get("state", "")))
        print(_t(agent, "mcp.shortcut.source", value=status.get("source", "")))
        sections = info.get("sections", {}) if isinstance(info.get("sections"), dict) else {}
        for sec_key, title in (
            ("tools", _t(agent, "mcp.shortcut.section.tools")),
            ("resources", _t(agent, "mcp.shortcut.section.resources")),
            ("resource_templates", _t(agent, "mcp.shortcut.section.resource_templates")),
            ("prompts", _t(agent, "mcp.shortcut.section.prompts")),
        ):
            sec = sections.get(sec_key, {}) if isinstance(sections.get(sec_key), dict) else {}
            count = sec.get("count", 0)
            print(_t(agent, "mcp.shortcut.section_count", title=title, count=count))
            items = sec.get("items", []) if isinstance(sec.get("items"), list) else []
            if items and sec_key in ("tools", "resources", "prompts"):
                labels = [mcp_item_label(x) for x in items]
                print(_t(agent, "mcp.shortcut.items", items=", ".join(labels)))
    elif tool_name in ("mcp_disable_tools", "mcp_enable_tools"):
        print(_t(agent, "mcp.shortcut.server", value=result.get("server", args.get("server", ""))))
        disabled = result.get("disabled_tools", [])
        if not isinstance(disabled, list):
            disabled = []
        print(
            _t(agent, "mcp.shortcut.disabled_tools", count=len(disabled), items=", ".join(disabled) if disabled else "None")
        )
    elif tool_name == "mcp_list_disabled_tools":
        data = result.get("disabled_tools", {})
        if isinstance(data, dict):
            for s, arr in data.items():
                tools = arr if isinstance(arr, list) else []
                print(
                    _t(
                        agent,
                        "mcp.shortcut.server_items",
                        server=s,
                        items=", ".join(tools) if tools else "None",
                    )
                )
        else:
            print(_t(agent, "mcp.shortcut.disabled_tools_none"))
    elif tool_name in (
        "mcp_list_tools",
        "mcp_list_resources",
        "mcp_list_resource_templates",
        "mcp_list_prompts",
    ):
        server = result.get("server", args.get("server", ""))
        count = result.get("count", 0)
        print(_t(agent, "mcp.shortcut.server", value=server))
        print(_t(agent, "mcp.shortcut.count", value=count))
        key = {
            "mcp_list_tools": "tools",
            "mcp_list_resources": "resources",
            "mcp_list_resource_templates": "templates",
            "mcp_list_prompts": "prompts",
        }.get(tool_name, "")
        items = result.get(key, []) if isinstance(result.get(key), list) else []
        if items:
            labels = [mcp_item_label(x) for x in items]
            print(_t(agent, "mcp.shortcut.items_label", value=", ".join(labels)))
    else:
        msg = result.get("message", "")
        if msg:
            print(_t(agent, "mcp.shortcut.message", value=msg))
    print(_t(agent, "mcp.shortcut.result_footer"))
