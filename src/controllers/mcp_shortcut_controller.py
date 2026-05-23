from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def parse_mcp_shortcut_command(
    builtin_line: str,
) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    """
    Parse '/mcp ...' shortcuts into tool calls.
    Rules:
    - Only required parameters are accepted.
    - Optional parameters are not supported in shortcuts.
    - For 'mcp_list_disabled_tools', server is optional.
    """
    raw = (builtin_line or "").strip()
    if not raw:
        return None, {}, "Command is empty"
    parts = raw.split()
    low = [p.lower() for p in parts]
    if not low:
        return None, {}, None
    if low[0] != "mcp":
        return None, {}, None
    if len(parts) < 2:
        return None, {}, "Usage: /mcp <subcommand> [args]"
    cmd = low[1]

    if cmd == "reload-config" and len(parts) == 2:
        return "mcp_reload_config", {}, None
    if cmd == "reload-config" and len(parts) != 2:
        return None, {}, "Usage: /mcp reload-config"
    if cmd == "status" and len(parts) == 2:
        return "mcp_status", {}, None
    if cmd == "status" and len(parts) != 2:
        return None, {}, "Usage: /mcp status"
    if cmd == "status-refresh" and len(parts) == 2:
        return "mcp_status_refresh", {}, None
    if cmd == "status-refresh" and len(parts) != 2:
        return None, {}, "Usage: /mcp status-refresh"
    if cmd == "reconnect" and len(parts) == 3:
        return "mcp_reconnect", {"server": parts[2]}, None
    if cmd == "reconnect":
        return None, {}, "Usage: /mcp reconnect <server>"
    if cmd == "server-info" and len(parts) == 3:
        return "mcp_server_info", {"server": parts[2]}, None
    if cmd == "server-info":
        return None, {}, "Usage: /mcp server-info <server>"
    if cmd == "list-tools" and len(parts) == 3:
        return "mcp_list_tools", {"server": parts[2]}, None
    if cmd == "list-tools":
        return None, {}, "Usage: /mcp list-tools <server>"
    if cmd == "list-resources" and len(parts) == 3:
        return "mcp_list_resources", {"server": parts[2]}, None
    if cmd == "list-resources":
        return None, {}, "Usage: /mcp list-resources <server>"
    if cmd == "list-resource-templates" and len(parts) == 3:
        return "mcp_list_resource_templates", {"server": parts[2]}, None
    if cmd == "list-resource-templates":
        return None, {}, "Usage: /mcp list-resource-templates <server>"
    if cmd == "list-prompts" and len(parts) == 3:
        return "mcp_list_prompts", {"server": parts[2]}, None
    if cmd == "list-prompts":
        return None, {}, "Usage: /mcp list-prompts <server>"
    if cmd == "list-disabled-tools":
        if len(parts) == 2:
            return "mcp_list_disabled_tools", {}, None
        if len(parts) == 3:
            return "mcp_list_disabled_tools", {"server": parts[2]}, None
        return None, {}, "Usage: /mcp list-disabled-tools [server]"

    if cmd == "disable-tools" and len(parts) >= 4:
        server = parts[2]
        tools_csv = " ".join(parts[3:]).strip()
        tools = [x.strip() for x in tools_csv.split(",") if x.strip()]
        if not tools:
            return (
                None,
                {},
                "Missing 'tools' parameter. Use comma-separated values, for example: /mcp disable-tools playwright browser_click,browser_type",
            )
        return "mcp_disable_tools", {"server": server, "tools": tools}, None
    if cmd == "disable-tools":
        return None, {}, "Usage: /mcp disable-tools <server> <tool1,tool2>"

    if cmd == "enable-tools" and len(parts) >= 4:
        server = parts[2]
        tools_csv = " ".join(parts[3:]).strip()
        tools = [x.strip() for x in tools_csv.split(",") if x.strip()]
        if not tools:
            return (
                None,
                {},
                "Missing 'tools' parameter. Use comma-separated values, for example: /mcp enable-tools playwright browser_click,browser_type",
            )
        return "mcp_enable_tools", {"server": server, "tools": tools}, None
    if cmd == "enable-tools":
        return None, {}, "Usage: /mcp enable-tools <server> <tool1,tool2>"

    return None, {}, (
        "Invalid MCP shortcut command. Available examples: "
        "/mcp status, /mcp status-refresh, /mcp reload-config, "
        "/mcp reconnect <server>, /mcp server-info <server>, "
        "/mcp list-tools <server>, /mcp list-resources <server>, "
        "/mcp list-resource-templates <server>, /mcp list-prompts <server>, "
        "/mcp list-disabled-tools [server], "
        "/mcp disable-tools <server> <tool1,tool2>, /mcp enable-tools <server> <tool1,tool2>"
    )


def mcp_item_label(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    for k in ("display_name", "name", "uri", "id", "title"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return str(item)


def print_mcp_shortcut_result(
    tool_name: str,
    args: Dict[str, Any],
    result: Dict[str, Any],
) -> None:
    print("\n=== MCP Command Result ===")
    print(f"Command: {tool_name}")
    if not result.get("success", False):
        print("Status : FAILED")
        print(f"Error  : {result.get('error', 'Unknown error')}")
        print("==========================\n")
        return

    print("Status : OK")
    if tool_name == "mcp_reload_config":
        print(f"Changed: {bool(result.get('changed', False))}")
        summary = result.get("summary", {}) if isinstance(result.get("summary"), dict) else {}
        added = ", ".join(summary.get("added", [])) or "None"
        changed = ", ".join(summary.get("changed", [])) or "None"
        removed = ", ".join(summary.get("removed", [])) or "None"
        print(f"Added  : {added}")
        print(f"Updated: {changed}")
        print(f"Removed: {removed}")
    elif tool_name in ("mcp_status", "mcp_status_refresh"):
        status = result.get("status", {}) if isinstance(result.get("status"), dict) else {}
        print(f"Total  : {status.get('total', 0)}")
        print(f"Success: {status.get('success', 0)}")
        print(f"Failed : {status.get('failed', 0)}")
        print(f"Loading: {status.get('loading_count', 0)}")
        print(f"Loaded : {status.get('all_loaded', False)}")
        servers = status.get("servers", {}) if isinstance(status.get("servers"), dict) else {}
        if servers:
            print("Servers:")
            for s, st in servers.items():
                if not isinstance(st, dict):
                    continue
                print(
                    f"- {s}: state={st.get('state','')}, tools={st.get('tool_count',0)}, source={st.get('source','')}"
                )
    elif tool_name == "mcp_reconnect":
        print(f"Server : {result.get('server', args.get('server', ''))}")
        print(f"Source : {result.get('source', '')}")
        print(f"Tools  : {result.get('count', 0)}")
    elif tool_name == "mcp_server_info":
        info = result.get("info", {}) if isinstance(result.get("info"), dict) else {}
        status = info.get("status", {}) if isinstance(info.get("status"), dict) else {}
        print(f"Server : {result.get('server', args.get('server', ''))}")
        print(f"State  : {status.get('state', '')}")
        print(f"Source : {status.get('source', '')}")
        sections = info.get("sections", {}) if isinstance(info.get("sections"), dict) else {}
        for sec_key, title in (
            ("tools", "Tools"),
            ("resources", "Resources"),
            ("resource_templates", "ResourceTemplates"),
            ("prompts", "Prompts"),
        ):
            sec = sections.get(sec_key, {}) if isinstance(sections.get(sec_key), dict) else {}
            count = sec.get("count", 0)
            print(f"{title:<16}: {count}")
            items = sec.get("items", []) if isinstance(sec.get("items"), list) else []
            if items and sec_key in ("tools", "resources", "prompts"):
                labels = [mcp_item_label(x) for x in items]
                print(f"  - {', '.join(labels)}")
    elif tool_name in ("mcp_disable_tools", "mcp_enable_tools"):
        print(f"Server : {result.get('server', args.get('server', ''))}")
        disabled = result.get("disabled_tools", [])
        if not isinstance(disabled, list):
            disabled = []
        print(f"Disabled tools ({len(disabled)}): {', '.join(disabled) if disabled else 'None'}")
    elif tool_name == "mcp_list_disabled_tools":
        data = result.get("disabled_tools", {})
        if isinstance(data, dict):
            for s, arr in data.items():
                tools = arr if isinstance(arr, list) else []
                print(f"- {s}: {', '.join(tools) if tools else 'None'}")
        else:
            print("Disabled tools: None")
    elif tool_name in (
        "mcp_list_tools",
        "mcp_list_resources",
        "mcp_list_resource_templates",
        "mcp_list_prompts",
    ):
        server = result.get("server", args.get("server", ""))
        count = result.get("count", 0)
        print(f"Server : {server}")
        print(f"Count  : {count}")
        key = {
            "mcp_list_tools": "tools",
            "mcp_list_resources": "resources",
            "mcp_list_resource_templates": "templates",
            "mcp_list_prompts": "prompts",
        }.get(tool_name, "")
        items = result.get(key, []) if isinstance(result.get(key), list) else []
        if items:
            labels = [mcp_item_label(x) for x in items]
            print(f"Items  : {', '.join(labels)}")
    else:
        msg = result.get("message", "")
        if msg:
            print(f"Message: {msg}")
    print("==========================\n")
