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
    """执行工具命令，支持批量命令和 cls 命令。"""
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

    if action == "cls":
        import os
        os.system('cls' if os.name == 'nt' else 'clear')
        return {"success": True, "message": "屏幕已清空"}

    elif action == "batch":
        commands = params.get("commands", [])
        results = []
        all_success = True
        for subcmd in commands:
            sub_action = (subcmd.get("tool") or subcmd.get("action") or "").strip()
            sub_args = subcmd.get("args")
            if not isinstance(sub_args, dict):
                sub_args = subcmd.get("params")
            if not isinstance(sub_args, dict):
                sub_args = {}
            sub_result = self.execute_tool_call(sub_action, sub_args)
            results.append({"action": sub_action, "result": sub_result})
        
            # 检查用户是否取消了子命令
            if not sub_result.get("success", True) and (
                "用户取消了操作" in sub_result.get("error", "") or 
                "用户拒绝" in sub_result.get("error", "") or
                "用户取消" in sub_result.get("error", "")
            ):
                # 用户取消了某个子命令，停止执行剩余命令
                return {"success": False, "error": "用户取消了操作", "results": results}
        
            if not sub_result.get("success", True):
                all_success = False
        return {"success": all_success, "results": results}

    elif action == "ffmpeg":
        source = params.get("source")
        target = params.get("target")
        options = params.get("options")
        if source and target:
            result = self.action_ffmpeg(source, target, options)
            if result["success"]:
                print(f"✅ {result['message']}")
            else:
                print(f"❌ {result['error']}")
            return result
        else:
            print("❌ 命令缺少参数 source 或 target")
            return {"success": False, "error": "缺少 source 或 target 参数"}

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
                                "interactive": True,
                                "output": "",
                                "stderr": "",
                                "return_code": 0,
                            }
                        break
            shell_interactive = True
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
                interactive=True,
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

    elif action == "diff":
        file1 = params.get("file1")
        file2 = params.get("file2")
        options = params.get("options")
        if file1 and file2:
            result = self.action_diff(file1, file2, options)
            if result["success"]:
                command_type = result.get("command_type", "unknown")
                print(f"\n🔍 文件比较完成 (使用 {command_type}): {result['command']}")
                print(f"📊 结果: {result['message']}")
                if result.get("output"):
                    print("📤 差异详情:")
                    print(result["output"])
            else:
                print(f"❌ 文件比较失败: {result['error']}")
                if result.get("output"):
                    print("📤 输出:")
                    print(result["output"])
            return result
        else:
            print("❌ diff命令缺少file1或file2参数")
            return {"success": False, "error": "缺少file1或file2参数"}

    # mcp_* tool branches are dispatched by dispatch_mcp_tool above.

    elif action == "knowledge_sync":
        """同步知识库"""
        if not self._ensure_knowledge_manager():
            return {"success": False, "error": "知识库不可用（依赖未安装或初始化失败）"}
        try:
            self.knowledge_manager.sync_knowledge_base()
            return {"success": True, "message": "知识库同步完成"}
        except Exception as e:
            return {"success": False, "error": f"知识库同步失败: {str(e)}"}

    elif action == "knowledge_stats":
        """获取知识库统计信息"""
        if not self._ensure_knowledge_manager():
            return {"success": False, "error": "知识库不可用（依赖未安装或初始化失败）"}
    
        try:
            stats = self.knowledge_manager.get_knowledge_stats()
            if stats:
                print(f"\n📊 知识库统计信息:")
                print(f"📄 文档总数: {stats.get('total_documents', 0)}")
                print(f"📝 文本片段总数: {stats.get('total_chunks', 0)}")
                print(f"📁 支持的文件类型: {', '.join(stats.get('supported_extensions', []))}")
            
                file_types = stats.get('file_types', {})
                if file_types:
                    print(f"📋 文件类型分布:")
                    for ext, count in file_types.items():
                        print(f"  {ext}: {count} 个文件")
            else:
                print("❌ 获取知识库统计信息失败")
        
            return {"success": True, "stats": stats}
        except Exception as e:
            return {"success": False, "error": f"获取知识库统计信息失败: {str(e)}"}

    elif action == "knowledge_search":
        """搜索知识库"""
        if not self._ensure_knowledge_manager():
            return {"success": False, "error": "知识库不可用（依赖未安装或初始化失败）"}
    
        query = params.get("query", "")
        top_k = params.get("top_k", params.get("limit", 5))
    
        if not query:
            return {"success": False, "error": "缺少搜索查询参数"}
    
        try:
            results = self.knowledge_manager.search_knowledge(query, top_k)
            if results:
                print(f"\n🔍 知识库搜索结果 (查询: '{query}'):")
                print("=" * 80)
                for i, result in enumerate(results, 1):
                    print(f"{i}. 来源: {result['source']}")
                    print(f"   相似度: {result['similarity']:.3f}")
                    print(f"   内容: {result['content'][:200]}...")
                    print("-" * 40)
            else:
                print(f"🔍 未找到相关结果: '{query}'")
        
            return {"success": True, "results": results, "query": query}
        except Exception as e:
            return {"success": False, "error": f"知识库搜索失败: {str(e)}"}

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

