"""Tool: mcp_call_tool_batch."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class McpCallToolBatchTool(BaseTool):
    name = "mcp_call_tool_batch"
    description = "Call multiple MCP tools in one batch request."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
            },
            "calls": {
                "type": "array",
                "items": {
                    "type": "object",
                },
            },
            "timeout_s": {
                "type": "number",
            },
            "allow_partial_failure": {
                "type": "boolean",
            },
        },
        "required": [
            "server",
            "calls",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..integrations.mcp import McpError

        params = params if isinstance(params, dict) else {}
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
