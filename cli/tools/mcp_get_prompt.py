"""Tool: mcp_get_prompt."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpGetPromptTool(BaseTool):
    name = "mcp_get_prompt"
    description = "Resolve an MCP prompt with optional arguments."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
            "prompt": {
                "type": "string",
            },
            "arguments": {
                "type": "object",
            },
            "timeout_s": {
                "type": "number",
            },
        },
        "required": [
            "server",
            "prompt",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..integrations.mcp import McpError

        params = params if isinstance(params, dict) else {}
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
