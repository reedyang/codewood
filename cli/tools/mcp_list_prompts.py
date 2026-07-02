"""Tool: mcp_list_prompts."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpListPromptsTool(BaseTool):
    name = "mcp_list_prompts"
    description = "List prompts for a specified MCP server."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
            "use_cache": {
                "type": "boolean",
            },
            "timeout_s": {
                "type": "number",
            },
        },
        "required": [
            "server",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..integrations.mcp import McpError

        params = params if isinstance(params, dict) else {}
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
