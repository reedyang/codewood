"""Tool: mcp_sampling_create_message."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpSamplingCreateMessageTool(BaseTool):
    name = "mcp_sampling_create_message"
    description = "Call the MCP sampling/createMessage capability."
    requires_mcp = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
            "sampling_params": {
                "type": "object",
            },
            "timeout_s": {
                "type": "number",
            },
        },
        "required": [
            "server",
            "sampling_params",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..integrations.mcp import McpError

        params = params if isinstance(params, dict) else {}
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
