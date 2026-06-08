"""Legacy tool execution engine extracted from Agent._execute_tool_call_legacy."""
from __future__ import annotations

from typing import Any, Dict

from ..core.localization import get_display_language, translate
from ..core.security.git_guard import guard_git_clone_precheck
from .handlers.agent_state_handlers import dispatch_agent_state_tool
from .handlers.core_handlers import dispatch_core_tool
from .handlers.file_shell_handlers import dispatch_file_shell_tool
from .handlers.mcp_handlers import dispatch_mcp_tool
from .handlers.memory_handlers import dispatch_memory_tool


def _print_with_auto_hide_tracking(agent: Any, text: str) -> None:
    msg = str(text or "")
    print(msg)


def _t(agent: Any, key: str, **kwargs: Any) -> str:
    return translate(key, get_display_language(agent), **kwargs)


def execute_tool_call_legacy(agent: Any, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool command (legacy fallback)."""
    self = agent
    action = (tool_name or "").strip()
    params = arguments if isinstance(arguments, dict) else {}
    for handler in (
        dispatch_core_tool,
        dispatch_file_shell_tool,
        dispatch_mcp_tool,
        dispatch_memory_tool,
        dispatch_agent_state_tool,
    ):
        delegated = handler(self, action, params)
        if delegated is not None:
            return delegated
    if action == "ask_more_info":
        question = str(params.get("question") or "").strip()
        if not question:
            question = "Please provide the additional information needed to finish the task."
        expected = params.get("expected_fields")
        if not isinstance(expected, list):
            expected = []
        expected_fields = [str(x).strip() for x in expected if str(x).strip()]
        return {
            "success": True,
            "needs_user_input": True,
            "input_type": "supplement",
            "question": question,
            "expected_fields": expected_fields,
            "retryable": False,
            "message": "Requested additional information from the user",
        }
    if action == "shell":
        shell_cmd = params.get("command")
        if shell_cmd:
            lowered_shell = str(shell_cmd).lower()
            if self._mcp_pending_user_input:
                promptish = (
                    ("token" in lowered_shell)
                    or ("auth" in lowered_shell)
                    or ("credential" in lowered_shell)
                    or ("set /p" in lowered_shell)
                )
                mcpish = ("mcp" in lowered_shell) or ("figma" in lowered_shell)
                echoish = ("echo " in lowered_shell) or ("set /p" in lowered_shell)
                if promptish and mcpish and echoish:
                    waiting = ", ".join(sorted(self._mcp_pending_user_input.keys()))
                    return {
                        "success": False,
                        "retryable": False,
                        "blocked_by_guard": True,
                        "needs_user_input": True,
                        "input_type": "token",
                        "error": (
                            f"Detected a repeated token prompt loop (server={waiting}); this shell prompt was blocked."
                            " Wait for the user to provide a new token, then run mcp_reconnect again."
                        ),
                    }
            if " mcp start" in lowered_shell or ("helper.exe" in lowered_shell and " mcp " in lowered_shell):
                return {
                    "success": False,
                    "error": (
                        "Do not manually start or stop MCP servers via shell."
                        " Use mcp_list_tools / mcp_list_resources / mcp_read_resource / mcp_list_prompts / mcp_get_prompt / mcp_sampling_create_message / mcp_completion_complete / mcp_call_tool, and retry with timeout_s/use_cache."
                    ),
                }
            shell_force = bool(params.get("force", False))
            clone_guard = guard_git_clone_precheck(self.work_directory, str(shell_cmd), shell_force)
            if isinstance(clone_guard, dict):
                return clone_guard
            shell_interactive = bool(params.get("interactive", False))
            if not shell_force:
                # Guardrail: avoid accidental duplicate execution loops in multi-step tasks.
                for item in reversed(self.operation_results[-6:]):
                    prev_cmd = item.get("command") or {}
                    prev_res = item.get("result") or {}
                    if prev_cmd.get("action") != "shell":
                        continue
                    prev_params = prev_cmd.get("params") or {}
                    if str(prev_params.get("command", "")).strip() == str(shell_cmd).strip():
                        if prev_res.get("success", False):
                            msg = (
                                "Detected a duplicate shell command; skipped this execution."
                                " If you really need to rerun it, set force=true in params."
                            )
                            _print_with_auto_hide_tracking(self, f"ℹ️ {msg}")
                            return {
                                "success": True,
                                "message": msg,
                                "skipped_duplicate": True,
                                "interactive": shell_interactive,
                                "output": "",
                                "stderr": "",
                                "return_code": 0,
                            }
                        break
            shell_input = params.get("input")
            shell_cmd_dict = {
                "action": "shell",
                "params": {
                    "command": shell_cmd,
                    "interactive": shell_interactive,
                    "force": shell_force,
                    "input": shell_input if isinstance(shell_input, str) else None,
                },
            }
            confirmed = self._freedom_auto_confirm(shell_cmd_dict)
            result = self.action_shell_command(
                shell_cmd,
                confirmed=confirmed,
                interactive=shell_interactive,
                input_data=None,
            )
            if result["success"]:
                _print_with_auto_hide_tracking(self, f"\n💻 System command succeeded: {result['message']}")
            else:
                _print_with_auto_hide_tracking(self, f"❌ System command failed: {result.get('error', 'Unknown error')}")
            return result
        else:
            print(_t(self, "tool.shell.missing_command"))
            return {"success": False, "error": "Missing command parameter"}

    elif action == "apply_patch":
        file_path = params.get("path")
        patch = params.get("patch")
        if file_path and patch is not None:
            patch_cmd = {
                "action": "apply_patch",
                "params": {"path": file_path},
            }
            confirmed = self._freedom_auto_confirm(patch_cmd)
            result = self.action_apply_unified_patch(
                file_path=file_path, patch=str(patch), confirmed=confirmed
            )
            if result["success"]:
                print(f"✅ {result['message']}")
            else:
                print(f"❌ {result['error']}")
            return result
        print(_t(self, "tool.apply_patch.missing_path_patch"))
        return {"success": False, "error": "Missing path/patch parameters"}

    elif action == "read_image":
        file_path = params.get("path")
        prompt = params.get("prompt", "")
        if file_path:
            result = self.action_read_image(file_path, prompt)
            if result["success"]:
                print(_t(self, "tool.read_image.result_header", file=result["file"]))
                print("=" * 60)
                print(result["analysis"])
                print("=" * 60)
            else:
                print(f"❌ {result['error']}")
            return result
        else:
            print(_t(self, "tool.read_image.missing_path"))
            return {"success": False, "error": "Missing path parameter"}

    elif action == "project_context_search":
        result = self.action_project_context_search(params if isinstance(params, dict) else {})
        if result.get("success"):
            cand = result.get("candidates") if isinstance(result.get("candidates"), list) else []
            print(
                _t(
                    self,
                    "tool.project_context.summary",
                    query=result.get("query", ""),
                    matches=result.get("total_matches", 0),
                    top=len(cand),
                )
            )
        else:
            print(_t(self, "tool.project_context.failed", error=result.get("error", "")))
        return result

    # mcp_* tool branches are dispatched by dispatch_mcp_tool above.

    # memory_* and agent state branches are dispatched by handlers above.

    # Model often emits {"tool":"<skill_id>"} (e.g. weather) — skill folders are not tool names.
    sid_guess = (action or "").strip()
    if sid_guess and self.skills:
        for s in self.skills:
            if str(getattr(s, "skill_id", "")).strip().lower() == sid_guess.lower():
                canon = str(getattr(s, "skill_id", "") or sid_guess)
                return {
                    "success": False,
                    "error": (
                        f"\"{sid_guess}\" is the directory name (skill_id) of a loaded Agent Skill, not a built-in tool."
                        f' Call {{"tool":"request_skill_prompt","args":{{"skill_id":"{canon}"}}}} first'
                        " to inject the full SKILL text, then follow its instructions to execute shell or other allowed tools."
                    ),
                    "mistake_skill_as_tool": True,
                    "skill_id": canon,
                }

    return {"success": False, "error": "Unknown operation type"}

