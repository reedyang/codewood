"""Tool: mcp_read_resource."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpReadResourceTool(BaseTool):
    name = "mcp_read_resource"
    description = "Read MCP resource content by URI."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
            "uri": {
                "type": "string",
            },
            "timeout_s": {
                "type": "number",
            },
        },
        "required": [
            "server",
            "uri",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..integrations.mcp import McpError

        params = params if isinstance(params, dict) else {}
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
