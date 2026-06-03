from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def _t(agent: Any, en: str, zh: str) -> str:
    from ..core.localization import get_display_language, text

    return text(en, zh, get_display_language(agent))


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
    agent: Any,
    tool_name: str,
    args: Dict[str, Any],
    result: Dict[str, Any],
) -> None:
    print(_t(agent, "\n=== MCP Command Result ===", "\n=== MCP 命令结果 ==="))
    print(_t(agent, "Command: {tool_name}", "命令：{tool_name}").format(tool_name=tool_name))
    if not result.get("success", False):
        print(_t(agent, "Status : FAILED", "状态：失败"))
        print(
            _t(agent, "Error  : {error}", "错误：{error}").format(
                error=result.get("error", "Unknown error")
            )
        )
        print(_t(agent, "==========================\n", "==========================\n"))
        return

    print(_t(agent, "Status : OK", "状态：成功"))
    if tool_name == "mcp_reload_config":
        print(_t(agent, "Changed: {changed}", "已变更：{changed}").format(changed=bool(result.get("changed", False))))
        summary = result.get("summary", {}) if isinstance(result.get("summary"), dict) else {}
        added = ", ".join(summary.get("added", [])) or "None"
        changed = ", ".join(summary.get("changed", [])) or "None"
        removed = ", ".join(summary.get("removed", [])) or "None"
        print(_t(agent, "Added  : {value}", "新增：{value}").format(value=added))
        print(_t(agent, "Updated: {value}", "更新：{value}").format(value=changed))
        print(_t(agent, "Removed: {value}", "移除：{value}").format(value=removed))
    elif tool_name in ("mcp_status", "mcp_status_refresh"):
        status = result.get("status", {}) if isinstance(result.get("status"), dict) else {}
        print(_t(agent, "Total  : {value}", "总计：{value}").format(value=status.get("total", 0)))
        print(_t(agent, "Success: {value}", "成功：{value}").format(value=status.get("success", 0)))
        print(_t(agent, "Failed : {value}", "失败：{value}").format(value=status.get("failed", 0)))
        print(_t(agent, "Loading: {value}", "加载中：{value}").format(value=status.get("loading_count", 0)))
        print(_t(agent, "Loaded : {value}", "已加载：{value}").format(value=status.get("all_loaded", False)))
        servers = status.get("servers", {}) if isinstance(status.get("servers"), dict) else {}
        if servers:
            print(_t(agent, "Servers:", "服务端："))
            for s, st in servers.items():
                if not isinstance(st, dict):
                    continue
                print(
                    _t(
                        agent,
                        "- {server}: state={state}, tools={tools}, source={source}",
                        "- {server}：状态={state}，工具数={tools}，来源={source}",
                    ).format(
                        server=s,
                        state=st.get("state", ""),
                        tools=st.get("tool_count", 0),
                        source=st.get("source", ""),
                    )
                )
    elif tool_name == "mcp_reconnect":
        print(_t(agent, "Server : {value}", "服务端：{value}").format(value=result.get("server", args.get("server", ""))))
        print(_t(agent, "Source : {value}", "来源：{value}").format(value=result.get("source", "")))
        print(_t(agent, "Tools  : {value}", "工具：{value}").format(value=result.get("count", 0)))
    elif tool_name == "mcp_server_info":
        info = result.get("info", {}) if isinstance(result.get("info"), dict) else {}
        status = info.get("status", {}) if isinstance(info.get("status"), dict) else {}
        print(_t(agent, "Server : {value}", "服务端：{value}").format(value=result.get("server", args.get("server", ""))))
        print(_t(agent, "State  : {value}", "状态：{value}").format(value=status.get("state", "")))
        print(_t(agent, "Source : {value}", "来源：{value}").format(value=status.get("source", "")))
        sections = info.get("sections", {}) if isinstance(info.get("sections"), dict) else {}
        for sec_key, title in (
            ("tools", _t(agent, "Tools", "工具")),
            ("resources", _t(agent, "Resources", "资源")),
            ("resource_templates", _t(agent, "ResourceTemplates", "资源模板")),
            ("prompts", _t(agent, "Prompts", "提示词")),
        ):
            sec = sections.get(sec_key, {}) if isinstance(sections.get(sec_key), dict) else {}
            count = sec.get("count", 0)
            print(_t(agent, "{title:<16}: {count}", "{title:<16}：{count}").format(title=title, count=count))
            items = sec.get("items", []) if isinstance(sec.get("items"), list) else []
            if items and sec_key in ("tools", "resources", "prompts"):
                labels = [mcp_item_label(x) for x in items]
                print(_t(agent, "  - {items}", "  - {items}").format(items=", ".join(labels)))
    elif tool_name in ("mcp_disable_tools", "mcp_enable_tools"):
        print(_t(agent, "Server : {value}", "服务端：{value}").format(value=result.get("server", args.get("server", ""))))
        disabled = result.get("disabled_tools", [])
        if not isinstance(disabled, list):
            disabled = []
        print(
            _t(agent, "Disabled tools ({count}): {items}", "已禁用工具（{count}）：{items}").format(
                count=len(disabled),
                items=", ".join(disabled) if disabled else "None",
            )
        )
    elif tool_name == "mcp_list_disabled_tools":
        data = result.get("disabled_tools", {})
        if isinstance(data, dict):
            for s, arr in data.items():
                tools = arr if isinstance(arr, list) else []
                print(_t(agent, "- {server}: {items}", "- {server}：{items}").format(server=s, items=", ".join(tools) if tools else "None"))
        else:
            print(_t(agent, "Disabled tools: None", "已禁用工具：无"))
    elif tool_name in (
        "mcp_list_tools",
        "mcp_list_resources",
        "mcp_list_resource_templates",
        "mcp_list_prompts",
    ):
        server = result.get("server", args.get("server", ""))
        count = result.get("count", 0)
        print(_t(agent, "Server : {value}", "服务端：{value}").format(value=server))
        print(_t(agent, "Count  : {value}", "数量：{value}").format(value=count))
        key = {
            "mcp_list_tools": "tools",
            "mcp_list_resources": "resources",
            "mcp_list_resource_templates": "templates",
            "mcp_list_prompts": "prompts",
        }.get(tool_name, "")
        items = result.get(key, []) if isinstance(result.get(key), list) else []
        if items:
            labels = [mcp_item_label(x) for x in items]
            print(_t(agent, "Items  : {value}", "条目：{value}").format(value=", ".join(labels)))
    else:
        msg = result.get("message", "")
        if msg:
            print(_t(agent, "Message: {value}", "消息：{value}").format(value=msg))
    print(_t(agent, "==========================\n", "==========================\n"))
