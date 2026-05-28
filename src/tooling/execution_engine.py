"""Legacy tool execution engine extracted from SmartShellAgent._execute_tool_call_legacy."""
from __future__ import annotations

from typing import Any, Dict

from ..core.security.git_guard import guard_git_clone_precheck
from .handlers.agent_state_handlers import dispatch_agent_state_tool
from .handlers.core_handlers import dispatch_core_tool
from .handlers.file_shell_handlers import dispatch_file_shell_tool
from .handlers.mcp_handlers import dispatch_mcp_tool
from .handlers.memory_handlers import dispatch_memory_tool


def _print_with_auto_hide_tracking(agent: Any, text: str) -> None:
    msg = str(text or "")
    print(msg)


def execute_tool_call_legacy(agent: Any, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """执行工具命令（legacy fallback）。"""
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
    if action == "done":
        return {"success": True, "message": "任务已完成", "finished": True}
    if action == "ask_more_info":
        question = str(params.get("question") or "").strip()
        if not question:
            question = "请提供完成任务所需的补充信息。"
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
            "message": "已请求用户补充信息",
        }
    if action == "task_changed":
        new_task = str(params.get("new_task") or "").strip()
        reason = str(params.get("reason") or "").strip()
        if not new_task:
            return {"success": False, "error": "task_changed 缺少 new_task 参数"}
        return {
            "success": True,
            "task_changed": True,
            "new_task": new_task,
            "reason": reason or "用户输入与原始需求无关，已切换任务",
            "message": "任务已切换",
        }

    elif action == "shell":
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
                            f"检测到重复的 token 提示循环（server={waiting}），已阻止本次 shell 提示。"
                            "请等待用户提供新 token 后，再执行一次 mcp_reconnect。"
                        ),
                    }
            if " mcp start" in lowered_shell or ("helper.exe" in lowered_shell and " mcp " in lowered_shell):
                return {
                    "success": False,
                    "error": (
                        "禁止通过 shell 手工启停 MCP server。"
                        "请使用 mcp_list_tools / mcp_list_resources / mcp_read_resource / mcp_list_prompts / mcp_get_prompt / mcp_sampling_create_message / mcp_completion_complete / mcp_call_tool，并通过 timeout_s/use_cache 重试。"
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
                                "检测到重复 shell 命令，已跳过本次执行。"
                                "如确需重复运行，请在 params 中设置 force=true。"
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
                _print_with_auto_hide_tracking(self, f"\n💻 系统命令执行成功: {result['message']}")
            else:
                _print_with_auto_hide_tracking(self, f"❌ 系统命令执行失败: {result.get('error', '未知错误')}")
            return result
        else:
            print("❌ shell命令缺少command参数")
            return {"success": False, "error": "缺少command参数"}

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
        print("❌ apply_patch命令缺少 path/patch 参数")
        return {"success": False, "error": "缺少 path/patch 参数"}

    elif action == "read_image":
        file_path = params.get("path")
        prompt = params.get("prompt", "")
        if file_path:
            result = self.action_read_image(file_path, prompt)
            if result["success"]:
                print(f"\n🖼️ 图片读取结果 ({result['file']}):")
                print("=" * 60)
                print(result["analysis"])
                print("=" * 60)
            else:
                print(f"❌ {result['error']}")
            return result
        else:
            print("❌ read_image命令缺少path参数")
            return {"success": False, "error": "缺少path参数"}

    elif action == "project_context_search":
        result = self.action_project_context_search(params if isinstance(params, dict) else {})
        if result.get("success"):
            cand = result.get("candidates") if isinstance(result.get("candidates"), list) else []
            print(
                f"🧭 project context: query=`{result.get('query', '')}` "
                f"matches={result.get('total_matches', 0)} top={len(cand)}"
            )
        else:
            print(f"❌ project_context_search 失败: {result.get('error', '')}")
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
                        f"「{sid_guess}」是已加载 Agent Skill 的目录名（skill_id），不是内置 tool。"
                        f' 请先调用：{{"tool":"request_skill_prompt","args":{{"skill_id":"{canon}"}}}}'
                        " 注入 SKILL 全文后，再按正文使用 shell 或其它允许的工具执行。"
                    ),
                    "mistake_skill_as_tool": True,
                    "skill_id": canon,
                }

    return {"success": False, "error": "未知的操作类型"}

