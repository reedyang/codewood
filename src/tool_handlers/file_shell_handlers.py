from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from ..git_guard import guard_git_clone_precheck


def dispatch_file_shell_tool(agent: Any, action: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if action == "list":
        path = params.get("path")
        file_filter = params.get("filter")
        smart_filter = params.get("smart_filter")
        result = agent.action_list_directory(path, file_filter)
        if result.get("success"):
            if smart_filter:
                filtered_result = agent.action_intelligent_filter(result, smart_filter)
                if filtered_result.get("success"):
                    result = filtered_result
        return result

    if action == "cd":
        path = params.get("path", "")
        result = agent.action_change_directory(path)
        if result.get("success"):
            agent._bind_project_index_workspace()
        return result

    if action == "rename":
        old_name = params.get("old_name")
        new_name = params.get("new_name")
        if old_name and new_name:
            return agent.action_rename_file(old_name, new_name)
        return {"success": False, "error": "missing old_name/new_name"}

    if action == "move":
        source = params.get("source")
        destination = params.get("destination")
        if source and destination:
            move_cmd = {"tool": "move", "args": {"source": source, "destination": destination}}
            confirmed = agent._freedom_auto_confirm(move_cmd)
            return agent.action_move_file(source, destination, confirmed=confirmed)
        return {"success": False, "error": "missing source/destination"}

    if action == "delete":
        file_name = params.get("file_name") or params.get("path") or params.get("name")
        if not file_name:
            return {"success": False, "error": "missing file_name/path/name"}
        target_path = agent.work_directory / file_name
        base = Path(file_name).name
        if (
            not target_path.exists()
            and agent._last_auto_removed_ephemeral
            and base.lower() == agent._last_auto_removed_ephemeral.lower()
        ):
            agent._last_auto_removed_ephemeral = None
            return {
                "success": True,
                "message": f"file {base} already removed by auto cleanup",
                "skipped_duplicate_delete": True,
            }
        del_cmd = {"tool": "delete", "args": {"path": file_name}}
        confirmed = agent._freedom_auto_confirm(del_cmd)
        return agent.action_delete_file(file_name, confirmed=confirmed)

    if action == "mkdir":
        path = params.get("path")
        if path:
            return agent.action_create_directory(path)
        return {"success": False, "error": "missing path"}

    if action == "info":
        file_name = params.get("file_name") or params.get("path") or params.get("name")
        if file_name:
            return agent.action_get_file_info(file_name)
        return {"success": False, "error": "missing file_name/path/name"}

    if action == "ffmpeg":
        source = params.get("source")
        target = params.get("target")
        options = params.get("options")
        if source and target:
            return agent.action_ffmpeg(source, target, options)
        return {"success": False, "error": "missing source/target"}

    if action == "summarize":
        file_path = params.get("path")
        if file_path:
            return agent.action_summarize_file(file_path)
        return {"success": False, "error": "missing path"}

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

    if action == "text_file":
        filename = params.get("filename")
        content = params.get("content")
        overwrite = bool(params.get("overwrite", False))
        if filename and content is not None:
            file_cmd = {"action": "text_file", "params": {"filename": filename, "content": ""}}
            confirmed = agent._freedom_auto_confirm(file_cmd)
            return agent.action_create_text_file(
                filename, content, confirmed=confirmed, overwrite=overwrite
            )
        return {"success": False, "error": "missing filename/content"}

    if action == "read":
        file_path = params.get("path")
        max_lines = params.get("max_lines") if "max_lines" in params else None
        start_line = params.get("start_line") if "start_line" in params else None
        line_count = params.get("line_count") if "line_count" in params else None
        if file_path:
            return agent.action_read_file(file_path, max_lines, start_line, line_count)
        return {"success": False, "error": "missing path"}

    if action == "edit_text":
        file_path = params.get("path")
        start_line = params.get("start_line")
        line_span = params.get("line_span", 0)
        operation = params.get("operation")
        content = params.get("content")
        if file_path and start_line is not None and operation:
            edit_cmd = {
                "action": "edit_text",
                "params": {
                    "path": file_path,
                    "start_line": start_line,
                    "line_span": line_span,
                    "operation": operation,
                },
            }
            confirmed = agent._freedom_auto_confirm(edit_cmd)
            return agent.action_edit_text_file(
                file_path=file_path,
                start_line=start_line,
                line_span=line_span,
                operation=operation,
                content=content,
                confirmed=confirmed,
            )
        return {"success": False, "error": "missing path/start_line/operation"}

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

    if action == "grep":
        return agent.action_grep(params if isinstance(params, dict) else {})

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
