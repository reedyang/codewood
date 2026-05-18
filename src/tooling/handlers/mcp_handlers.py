from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ...integrations.mcp import McpError


def dispatch_mcp_tool(agent: Any, action: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if action == "mcp_list_disabled_tools":
        server = params.get("server")
        try:
            result = agent.mcp_manager.list_disabled_tools(
                str(server).strip() if server else None
            )
            total = sum(len(v) for v in result.values()) if isinstance(result, dict) else 0
            return {
                "success": True,
                "server": server,
                "disabled_tools": result,
                "count": total,
                "message": "MCP disabled tools listed",
            }
        except Exception as e:
            return {"success": False, "error": f"MCP list disabled tools failed: {e}"}

    if action == "mcp_reload_config":
        result = agent._reload_mcp_config_now()
        if result.get("success"):
            return {
                "success": True,
                "changed": bool(result.get("changed", False)),
                "summary": result.get("summary", {}),
                "message": str(result.get("message", "MCP config reloaded")),
            }
        return {"success": False, "error": str(result.get("error", "MCP config reload failed"))}

    if action == "mcp_disable_tools":
        server = params.get("server")
        tools_param = params.get("tools")
        if not server:
            return {"success": False, "error": "missing server"}
        names: List[str] = []
        if isinstance(tools_param, str):
            names = [x.strip() for x in tools_param.split(",") if x.strip()]
        elif isinstance(tools_param, list):
            names = [str(x).strip() for x in tools_param if str(x).strip()]
        else:
            return {"success": False, "error": "tools must be csv or list"}
        if not names:
            return {"success": False, "error": "tools is empty"}
        try:
            disabled = agent.mcp_manager.disable_tools(str(server), names)
            agent.system_prompt = agent._compose_system_prompt_snapshot(include_tools=False)
            return {
                "success": True,
                "server": server,
                "disabled_tools": disabled,
                "count": len(disabled),
                "message": f"MCP tools disabled (server={server})",
            }
        except McpError as e:
            return {"success": False, "error": f"MCP disable tools failed: {e}"}
        except Exception as e:
            return {"success": False, "error": f"MCP disable tools exception: {e}"}

    if action == "mcp_enable_tools":
        server = params.get("server")
        tools_param = params.get("tools")
        if not server:
            return {"success": False, "error": "missing server"}
        names: List[str] = []
        if isinstance(tools_param, str):
            names = [x.strip() for x in tools_param.split(",") if x.strip()]
        elif isinstance(tools_param, list):
            names = [str(x).strip() for x in tools_param if str(x).strip()]
        else:
            return {"success": False, "error": "tools must be csv or list"}
        if not names:
            return {"success": False, "error": "tools is empty"}
        try:
            disabled = agent.mcp_manager.enable_tools(str(server), names)
            agent.system_prompt = agent._compose_system_prompt_snapshot(include_tools=False)
            return {
                "success": True,
                "server": server,
                "disabled_tools": disabled,
                "count": len(disabled),
                "message": f"MCP tools enabled (server={server})",
            }
        except McpError as e:
            return {"success": False, "error": f"MCP enable tools failed: {e}"}
        except Exception as e:
            return {"success": False, "error": f"MCP enable tools exception: {e}"}

    if action == "mcp_server_info":
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

    if action == "mcp_list_tools":
        server = params.get("server")
        use_cache = bool(params.get("use_cache", True))
        timeout_s = float(params.get("timeout_s", 8.0))
        if not server:
            return {"success": False, "error": "missing server"}
        try:
            tools, from_cache = agent.mcp_manager.list_tools(
                str(server),
                timeout_s=timeout_s,
                use_cache=use_cache,
            )
            agent.system_prompt = agent._compose_system_prompt_snapshot(include_tools=False)
            status = agent.mcp_manager.get_status().get("servers", {}).get(str(server), {})
            return {
                "success": True,
                "server": server,
                "tools": tools,
                "from_cache": from_cache,
                "source": status.get("source", ""),
                "count": len(tools) if isinstance(tools, list) else 0,
                "message": f"MCP tools fetched (server={server})",
            }
        except McpError as e:
            return {"success": False, "error": f"MCP list tools failed: {e}"}
        except Exception as e:
            return {"success": False, "error": f"MCP list tools exception: {e}"}

    if action == "mcp_status":
        log_limit = int(params.get("log_limit", 20))
        status = agent.mcp_manager.get_status(log_limit=log_limit)
        return {
            "success": True,
            "cache_only": True,
            "status": status,
            "message": "MCP cached status fetched",
        }

    if action == "mcp_status_refresh":
        timeout_s = float(params.get("timeout_s", 12.0))
        force = bool(params.get("force", True))
        log_limit = int(params.get("log_limit", 20))
        servers = params.get("servers")
        if servers is not None and not isinstance(servers, list):
            return {"success": False, "error": "servers must be a list"}
        try:
            status = agent.mcp_manager.refresh_status_sync(
                servers=[str(s) for s in servers] if isinstance(servers, list) else None,
                timeout_s=timeout_s,
                force=force,
            )
            agent.system_prompt = agent._compose_system_prompt_snapshot(include_tools=False)
            status["recent_logs"] = agent.mcp_manager.get_recent_logs(log_limit)
            return {
                "success": True,
                "cache_only": False,
                "status": status,
                "message": "MCP status refreshed",
            }
        except Exception as e:
            return {"success": False, "error": f"MCP status refresh failed: {e}"}

    if action == "mcp_reconnect":
        server = params.get("server")
        timeout_s = float(params.get("timeout_s", 15.0))
        if not server:
            return {"success": False, "error": "missing server"}
        try:
            tools = agent.mcp_manager.reconnect_server(str(server), timeout_s=timeout_s)
            agent._mcp_pending_user_input.pop(str(server), None)
            agent.system_prompt = agent._compose_system_prompt_snapshot(include_tools=False)
            status = agent.mcp_manager.get_status().get("servers", {}).get(str(server), {})
            return {
                "success": True,
                "server": server,
                "tools": tools,
                "count": len(tools) if isinstance(tools, list) else 0,
                "source": status.get("source", ""),
                "message": f"MCP server reconnected (server={server})",
            }
        except McpError as e:
            err = str(e)
            err_l = err.lower()
            auth_like = (
                ("401" in err_l)
                or ("unauthorized" in err_l)
                or ("invalid token" in err_l)
                or ("token 无效" in err_l)
                or ("token 验证失败" in err_l)
            )
            if auth_like:
                agent._mcp_pending_user_input[str(server)] = {
                    "input_type": "token",
                    "ts": time.time(),
                }
                return {
                    "success": False,
                    "error": f"MCP reconnect failed: {err}",
                    "retryable": False,
                    "needs_user_input": True,
                    "input_type": "token",
                    "suggestion": (
                        "authentication failed; wait for user to provide a new token "
                        "before retrying mcp_reconnect"
                    ),
                }
            return {"success": False, "error": f"MCP reconnect failed: {err}"}
        except Exception as e:
            return {"success": False, "error": f"MCP reconnect exception: {e}"}

    if action == "mcp_call_tool":
        server = params.get("server")
        tool_name = params.get("tool")
        arguments = params.get("arguments", {})
        timeout_s = float(params.get("timeout_s", 20.0))
        if not server:
            return {"success": False, "error": "missing server"}
        if not tool_name:
            return {"success": False, "error": "missing tool"}
        if not isinstance(arguments, dict):
            return {"success": False, "error": "arguments must be object"}
        try:
            st = agent.mcp_manager.get_status().get("servers", {}).get(str(server), {})
            state_raw = str(st.get("state", "pending") or "pending").lower()
            if state_raw != "success":
                return {
                    "success": False,
                    "error": (
                        f"server={server} is not ready (state={state_raw}); "
                        "run mcp_list_tools(use_cache=false) first"
                    ),
                }
        except Exception:
            pass
        try:
            result = agent.mcp_manager.call_tool(
                str(server),
                str(tool_name),
                arguments,
                timeout_s=timeout_s,
            )
            return {
                "success": True,
                "server": server,
                "tool": tool_name,
                "result": result,
                "message": f"MCP tool called ({server}/{tool_name})",
            }
        except McpError as e:
            return {"success": False, "error": f"MCP tool call failed: {e}"}
        except Exception as e:
            return {"success": False, "error": f"MCP tool call exception: {e}"}

    if action == "mcp_call_tool_batch":
        server = params.get("server")
        calls = params.get("calls", [])
        timeout_s = float(params.get("timeout_s", 30.0))
        allow_partial_failure = bool(params.get("allow_partial_failure", False))
        if not server:
            return {"success": False, "error": "missing server"}
        if not isinstance(calls, list):
            return {"success": False, "error": "calls must be list"}
        try:
            st = agent.mcp_manager.get_status().get("servers", {}).get(str(server), {})
            state_raw = str(st.get("state", "pending") or "pending").lower()
            if state_raw != "success":
                return {
                    "success": False,
                    "error": (
                        f"server={server} is not ready (state={state_raw}); "
                        "run mcp_list_tools(use_cache=false) first"
                    ),
                }
        except Exception:
            pass
        try:
            results = agent.mcp_manager.call_tools_batch(
                str(server),
                calls,
                timeout_s=timeout_s,
                allow_partial_failure=allow_partial_failure,
            )
            total_count = len(results) if isinstance(results, list) else 0
            if allow_partial_failure and isinstance(results, list):
                ok_count = 0
                error_count = 0
                for item in results:
                    if isinstance(item, dict) and item.get("ok") is True:
                        ok_count += 1
                    else:
                        error_count += 1
            else:
                ok_count = total_count
                error_count = 0
            return {
                "success": True,
                "server": server,
                "results": results,
                "count": total_count,
                "total_count": total_count,
                "ok_count": ok_count,
                "error_count": error_count,
                "has_error": error_count > 0,
                "message": f"MCP tool batch called (server={server})",
            }
        except McpError as e:
            return {"success": False, "error": f"MCP tool batch failed: {e}"}
        except Exception as e:
            return {"success": False, "error": f"MCP tool batch exception: {e}"}

    if action == "mcp_list_resources":
        server = params.get("server")
        use_cache = bool(params.get("use_cache", True))
        timeout_s = float(params.get("timeout_s", 8.0))
        if not server:
            return {"success": False, "error": "missing server"}
        try:
            resources, from_cache = agent.mcp_manager.list_resources(
                str(server),
                timeout_s=timeout_s,
                use_cache=use_cache,
            )
            agent.system_prompt = agent._compose_system_prompt_snapshot(include_tools=False)
            return {
                "success": True,
                "server": server,
                "resources": resources,
                "from_cache": from_cache,
                "count": len(resources) if isinstance(resources, list) else 0,
                "message": f"MCP resources fetched (server={server})",
            }
        except McpError as e:
            return {"success": False, "error": f"MCP list resources failed: {e}"}
        except Exception as e:
            return {"success": False, "error": f"MCP list resources exception: {e}"}

    if action == "mcp_read_resource":
        server = params.get("server")
        uri = params.get("uri")
        timeout_s = float(params.get("timeout_s", 20.0))
        if not server:
            return {"success": False, "error": "missing server"}
        if not uri:
            return {"success": False, "error": "missing uri"}
        try:
            result = agent.mcp_manager.read_resource(
                str(server),
                str(uri),
                timeout_s=timeout_s,
            )
            return {
                "success": True,
                "server": server,
                "uri": uri,
                "result": result,
                "message": f"MCP resource read ({server}::{uri})",
            }
        except McpError as e:
            return {"success": False, "error": f"MCP read resource failed: {e}"}
        except Exception as e:
            return {"success": False, "error": f"MCP read resource exception: {e}"}

    if action == "mcp_list_resource_templates":
        server = params.get("server")
        use_cache = bool(params.get("use_cache", True))
        timeout_s = float(params.get("timeout_s", 8.0))
        if not server:
            return {"success": False, "error": "missing server"}
        try:
            templates, from_cache = agent.mcp_manager.list_resource_templates(
                str(server),
                timeout_s=timeout_s,
                use_cache=use_cache,
            )
            return {
                "success": True,
                "server": server,
                "templates": templates,
                "from_cache": from_cache,
                "count": len(templates) if isinstance(templates, list) else 0,
                "message": f"MCP resource templates fetched (server={server})",
            }
        except McpError as e:
            return {"success": False, "error": f"MCP list resource templates failed: {e}"}
        except Exception as e:
            return {"success": False, "error": f"MCP list resource templates exception: {e}"}

    if action == "mcp_list_prompts":
        server = params.get("server")
        use_cache = bool(params.get("use_cache", True))
        timeout_s = float(params.get("timeout_s", 8.0))
        if not server:
            return {"success": False, "error": "missing server"}
        try:
            prompts, from_cache = agent.mcp_manager.list_prompts(
                str(server),
                timeout_s=timeout_s,
                use_cache=use_cache,
            )
            agent.system_prompt = agent._compose_system_prompt_snapshot(include_tools=False)
            return {
                "success": True,
                "server": server,
                "prompts": prompts,
                "from_cache": from_cache,
                "count": len(prompts) if isinstance(prompts, list) else 0,
                "message": f"MCP prompts fetched (server={server})",
            }
        except McpError as e:
            return {"success": False, "error": f"MCP list prompts failed: {e}"}
        except Exception as e:
            return {"success": False, "error": f"MCP list prompts exception: {e}"}

    if action == "mcp_get_prompt":
        server = params.get("server")
        prompt_name = params.get("prompt")
        arguments = params.get("arguments", {})
        timeout_s = float(params.get("timeout_s", 20.0))
        if not server:
            return {"success": False, "error": "missing server"}
        if not prompt_name:
            return {"success": False, "error": "missing prompt"}
        if not isinstance(arguments, dict):
            return {"success": False, "error": "arguments must be object"}
        try:
            result = agent.mcp_manager.get_prompt(
                str(server),
                str(prompt_name),
                arguments,
                timeout_s=timeout_s,
            )
            return {
                "success": True,
                "server": server,
                "prompt": prompt_name,
                "result": result,
                "message": f"MCP prompt fetched ({server}/{prompt_name})",
            }
        except McpError as e:
            return {"success": False, "error": f"MCP get prompt failed: {e}"}
        except Exception as e:
            return {"success": False, "error": f"MCP get prompt exception: {e}"}

    if action == "mcp_sampling_create_message":
        server = params.get("server")
        sampling_params = params.get("sampling_params", {})
        timeout_s = float(params.get("timeout_s", 30.0))
        if not server:
            return {"success": False, "error": "missing server"}
        if not isinstance(sampling_params, dict):
            return {"success": False, "error": "sampling_params must be object"}
        try:
            result = agent.mcp_manager.sampling_create_message(
                str(server),
                sampling_params,
                timeout_s=timeout_s,
            )
            return {
                "success": True,
                "server": server,
                "result": result,
                "message": f"MCP sampling/createMessage called (server={server})",
            }
        except McpError as e:
            return {"success": False, "error": f"MCP sampling/createMessage failed: {e}"}
        except Exception as e:
            return {"success": False, "error": f"MCP sampling/createMessage exception: {e}"}

    if action == "mcp_completion_complete":
        server = params.get("server")
        completion_params = params.get("completion_params", {})
        timeout_s = float(params.get("timeout_s", 20.0))
        if not server:
            return {"success": False, "error": "missing server"}
        if not isinstance(completion_params, dict):
            return {"success": False, "error": "completion_params must be object"}
        try:
            result = agent.mcp_manager.completion_complete(
                str(server),
                completion_params,
                timeout_s=timeout_s,
            )
            return {
                "success": True,
                "server": server,
                "result": result,
                "message": f"MCP completion/complete called (server={server})",
            }
        except McpError as e:
            return {"success": False, "error": f"MCP completion/complete failed: {e}"}
        except Exception as e:
            return {"success": False, "error": f"MCP completion/complete exception: {e}"}

    return None
