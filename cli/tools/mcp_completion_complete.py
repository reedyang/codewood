"""Tool: mcp_completion_complete."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpCompletionCompleteTool(BaseTool):
    name = "mcp_completion_complete"
    description = "Call the MCP completion/complete capability."
    requires_mcp = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
            "completion_params": {
                "type": "object",
            },
            "timeout_s": {
                "type": "number",
            },
        },
        "required": [
            "server",
            "completion_params",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..integrations.mcp import McpError

        params = params if isinstance(params, dict) else {}
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
