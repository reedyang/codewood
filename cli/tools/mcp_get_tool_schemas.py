"""Tool: mcp_get_tool_schemas."""

from __future__ import annotations

from typing import Any, Dict, List

from .base import BaseTool


class McpGetToolSchemasTool(BaseTool):
    name = "mcp_get_tool_schemas"
    description = "Get the full input schemas for one or more MCP tools by server and tool names."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
            "tools": {
                "description": "List of tool names; supports \"a,b,c\" or [\"a\",\"b\"].",
                "oneOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "array",
                        "items": {
                            "type": "string",
                        },
                    },
                ],
            },
        },
        "required": [
            "server",
            "tools",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..integrations.mcp import McpError

        params = params if isinstance(params, dict) else {}
        server = str(params.get("server", "")).strip()
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
            manager = agent.mcp_manager
            all_tools, _ = manager.list_tools_with_disabled(server, use_cache=True)
            if not isinstance(all_tools, list):
                return {"success": False, "error": f"no tools found for server: {server}"}

            tool_map: Dict[str, Dict[str, Any]] = {}
            for t in all_tools:
                if not isinstance(t, dict):
                    continue
                name = str(t.get("name", "")).strip()
                tool_map[name] = t

            schemas: Dict[str, Dict[str, Any]] = {}
            not_found: List[str] = []
            for name in names:
                t = tool_map.get(name)
                if t is None:
                    not_found.append(name)
                    continue
                schema = t.get("inputSchema")
                if not isinstance(schema, dict):
                    schema = t.get("parameters")
                schemas[name] = {
                    "schema": schema if isinstance(schema, dict) else {},
                    "description": str(t.get("description", "")),
                }

            return {
                "success": True,
                "server": server,
                "schemas": schemas,
                "count": len(schemas),
                "not_found": not_found if not_found else [],
                "message": f"MCP tool schemas fetched (server={server})",
            }
        except McpError as e:
            return {"success": False, "error": f"mcp_get_tool_schemas failed: {e}"}
        except Exception as e:
            return {"success": False, "error": f"mcp_get_tool_schemas exception: {e}"}
