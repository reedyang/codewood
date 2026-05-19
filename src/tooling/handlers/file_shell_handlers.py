from __future__ import annotations

from typing import Any, Dict, Optional

from ...core.security.git_guard import guard_git_clone_precheck


def dispatch_file_shell_tool(agent: Any, action: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if action == "ffmpeg":
        source = params.get("source")
        target = params.get("target")
        options = params.get("options")
        if source and target:
            return agent.action_ffmpeg(source, target, options)
        return {"success": False, "error": "missing source/target"}

    if action == "shell":
        shell_cmd = params.get("command")
        if not shell_cmd:
            return {"success": False, "error": "missing command"}

        lowered_shell = str(shell_cmd).lower()
        if agent._mcp_pending_user_input:
            promptish = (
                ("token" in lowered_shell)
                or ("auth" in lowered_shell)
                or ("credential" in lowered_shell)
                or ("set /p" in lowered_shell)
            )
            mcpish = ("mcp" in lowered_shell) or ("figma" in lowered_shell)
            echoish = ("echo " in lowered_shell) or ("set /p" in lowered_shell)
            if promptish and mcpish and echoish:
                waiting = ", ".join(sorted(agent._mcp_pending_user_input.keys()))
                return {
                    "success": False,
                    "retryable": False,
                    "blocked_by_guard": True,
                    "needs_user_input": True,
                    "input_type": "token",
                    "error": (
                        f"detected repeated token prompt loop for server={waiting}; "
                        "wait for fresh user token then retry mcp_reconnect"
                    ),
                }

        if " mcp start" in lowered_shell or ("helper.exe" in lowered_shell and " mcp " in lowered_shell):
            return {
                "success": False,
                "error": (
                    "manual MCP server start via shell is blocked; "
                    "use MCP tools (mcp_list_tools/mcp_call_tool/etc.)"
                ),
            }

        shell_force = bool(params.get("force", False))
        clone_guard = guard_git_clone_precheck(agent.work_directory, str(shell_cmd), shell_force)
        if isinstance(clone_guard, dict):
            return clone_guard

        if not shell_force:
            for item in reversed(agent.operation_results[-6:]):
                prev_cmd = item.get("command") or {}
                prev_res = item.get("result") or {}
                if prev_cmd.get("action") != "shell":
                    continue
                prev_params = prev_cmd.get("params") or {}
                if str(prev_params.get("command", "")).strip() == str(shell_cmd).strip():
                    if prev_res.get("success", False):
                        msg = "duplicate shell command skipped; set force=true to rerun"
                        return {
                            "success": True,
                            "message": msg,
                            "skipped_duplicate": True,
                            "interactive": True,
                            "output": "",
                            "stderr": "",
                            "return_code": 0,
                        }
                    break

        shell_cmd_dict = {
            "action": "shell",
            "params": {
                "command": shell_cmd,
                "interactive": True,
                "force": shell_force,
                "input": params.get("input") if isinstance(params.get("input"), str) else None,
            },
        }
        confirmed = agent._freedom_auto_confirm(shell_cmd_dict)
        return agent.action_shell_command(
            shell_cmd,
            confirmed=confirmed,
            interactive=True,
            input_data=None,
        )

    if action == "apply_patch":
        file_path = params.get("path")
        patch = params.get("patch")
        if file_path and patch is not None:
            patch_cmd = {"action": "apply_patch", "params": {"path": file_path}}
            confirmed = agent._freedom_auto_confirm(patch_cmd)
            return agent.action_apply_unified_patch(
                file_path=file_path, patch=str(patch), confirmed=confirmed
            )
        return {"success": False, "error": "missing path/patch"}

    if action == "read_image":
        file_path = params.get("path")
        prompt = params.get("prompt", "")
        if file_path:
            return agent.action_read_image(file_path, prompt)
        return {"success": False, "error": "missing path"}

    if action == "project_context_search":
        return agent.action_project_context_search(params if isinstance(params, dict) else {})

    if action == "diff":
        file1 = params.get("file1")
        file2 = params.get("file2")
        options = params.get("options")
        if file1 and file2:
            return agent.action_diff(file1, file2, options)
        return {"success": False, "error": "missing file1/file2"}

    return None
