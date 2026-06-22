"""Tool: mcp_server_info."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpServerInfoTool(BaseTool):
    name = "mcp_server_info"
    description = "Query aggregated information for a specified MCP server (status, tools, resources, prompts)."
    requires_mcp = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
            "refresh": {
                "type": "boolean",
            },
            "timeout_s": {
                "type": "number",
            },
            "include_tools": {
                "type": "boolean",
            },
            "include_resources": {
                "type": "boolean",
            },
            "include_resource_templates": {
                "type": "boolean",
            },
            "include_prompts": {
                "type": "boolean",
            },
        },
        "required": [
            "server",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        server = params.get("server")
        if not server:
            return {"success": False, "error": "missing server"}
        server = str(server).strip()
        all_servers = agent.mcp_manager.mcp_config.get("mcpServers", {})
        if not isinstance(all_servers, dict) or server not in all_servers:
            return {"success": False, "error": f"unconfigured MCP server: {server}"}
        server_conf = all_servers.get(server, {})
        skip_preload = bool(
            isinstance(server_conf, dict) and server_conf.get("skip_preload", False)
        )
        clients_map = getattr(agent.mcp_manager, "_clients", {}) or {}
        is_connected = bool(isinstance(clients_map, dict) and server in clients_map)

        # Respect explicit skip_preload policy: when server is configured as skipped
        # and currently disconnected, do not auto-connect on server-info.
        if skip_preload and not is_connected:
            status_all = agent.mcp_manager.get_status()
            status_entry = (
                status_all.get("servers", {}).get(server, {})
                if isinstance(status_all.get("servers", {}), dict)
                else {}
            )
            status_out: Dict[str, Any] = (
                dict(status_entry) if isinstance(status_entry, dict) else {}
            )
            if not status_out:
                status_out = {
                    "state": "skipped",
                    "last_error": "skip_preload=true",
                    "suggestion": "server configured with skip_preload=true; connection is intentionally skipped.",
                }
            else:
                status_out["state"] = status_out.get("state") or "skipped"
                status_out["last_error"] = status_out.get("last_error") or "skip_preload=true"
                status_out["suggestion"] = (
                    status_out.get("suggestion")
                    or "server configured with skip_preload=true; connection is intentionally skipped."
                )
            info = {
                "server": server,
                "refresh": bool(params.get("refresh", False)),
                "use_cache": not bool(params.get("refresh", False)),
                "skipped_by_config": True,
                "sections": {},
                "errors": {},
                "status": status_out,
                "disabled_tools": agent.mcp_manager.list_disabled_tools(server).get(
                    server, []
                ),
                "status_summary": {
                    "all_loaded": bool(status_all.get("all_loaded", False)),
                    "loading_count": int(status_all.get("loading_count", 0) or 0),
                },
            }
            return {
                "success": True,
                "server": server,
                "info": info,
                "message": (
                    f"MCP server info skipped (server={server}): "
                    "configured skip_preload=true and not connected"
                ),
            }

        refresh = bool(params.get("refresh", False))
        timeout_s = float(params.get("timeout_s", 8.0))
        include_tools = bool(params.get("include_tools", True))
        include_resources = bool(params.get("include_resources", True))
        include_resource_templates = bool(params.get("include_resource_templates", True))
        include_prompts = bool(params.get("include_prompts", True))
        use_cache = not refresh

        info: Dict[str, Any] = {
            "server": server,
            "refresh": refresh,
            "use_cache": use_cache,
            "sections": {},
            "errors": {},
        }

        def _pack_items(payload: Any) -> List[Dict[str, Any]]:
            if not isinstance(payload, list):
                return []
            return [item for item in payload if isinstance(item, dict)]

        try:
            if include_tools:
                try:
                    tools, tools_from_cache = agent.mcp_manager.list_tools_with_disabled(
                        server, timeout_s=timeout_s, use_cache=use_cache
                    )
                    tools_items = _pack_items(tools)
                    tool_display_names: List[str] = []
                    disabled_tool_count = 0
                    for t in tools_items:
                        dn = str(t.get("display_name", "")).strip()
                        nm = str(t.get("name", "")).strip()
                        if bool(t.get("disabled", False)):
                            disabled_tool_count += 1
                        if dn:
                            tool_display_names.append(dn)
                        elif nm:
                            tool_display_names.append(nm)
                    info["sections"]["tools"] = {
                        "count": len(tools_items),
                        "from_cache": bool(tools_from_cache),
                        "items": tools_items,
                        "display_names": tool_display_names,
                        "disabled_count": disabled_tool_count,
                    }
                except Exception as e:
                    info["errors"]["tools"] = str(e)

            if include_resources:
                try:
                    resources, resources_from_cache = agent.mcp_manager.list_resources(
                        server, timeout_s=timeout_s, use_cache=use_cache
                    )
                    resources_items = _pack_items(resources)
                    info["sections"]["resources"] = {
                        "count": len(resources_items),
                        "from_cache": bool(resources_from_cache),
                        "items": resources_items,
                    }
                except Exception as e:
                    info["errors"]["resources"] = str(e)

            if include_resource_templates:
                try:
                    templates, templates_from_cache = agent.mcp_manager.list_resource_templates(
                        server, timeout_s=timeout_s, use_cache=use_cache
                    )
                    template_items = _pack_items(templates)
                    info["sections"]["resource_templates"] = {
                        "count": len(template_items),
                        "from_cache": bool(templates_from_cache),
                        "items": template_items,
                    }
                except Exception as e:
                    info["errors"]["resource_templates"] = str(e)

            if include_prompts:
                try:
                    prompts, prompts_from_cache = agent.mcp_manager.list_prompts(
                        server, timeout_s=timeout_s, use_cache=use_cache
                    )
                    prompt_items = _pack_items(prompts)
                    info["sections"]["prompts"] = {
                        "count": len(prompt_items),
                        "from_cache": bool(prompts_from_cache),
                        "items": prompt_items,
                    }
                except Exception as e:
                    info["errors"]["prompts"] = str(e)

            agent.system_prompt = agent._compose_system_prompt_snapshot(include_tools=False)
            status_all = agent.mcp_manager.get_status()
            info["status"] = status_all.get("servers", {}).get(server, {})
            info["disabled_tools"] = agent.mcp_manager.list_disabled_tools(server).get(server, [])
            info["status_summary"] = {
                "all_loaded": bool(status_all.get("all_loaded", False)),
                "loading_count": int(status_all.get("loading_count", 0) or 0),
            }
            return {
                "success": True,
                "server": server,
                "info": info,
                "message": f"MCP server info fetched (server={server}, refresh={refresh})",
            }
        except Exception as e:
            return {"success": False, "error": f"MCP server info failed: {e}"}
