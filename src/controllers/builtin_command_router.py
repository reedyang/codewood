from __future__ import annotations

import os
from typing import Any, Tuple

from ..config.app_info import get_app_name
from .language_command_controller import handle_language_builtin_command


def _t(agent: Any, en: str, zh: str) -> str:
    from ..core.localization import get_display_language, text

    return text(en, zh, get_display_language(agent))


def dispatch_builtin_command(
    agent: Any,
    builtin_line: str,
    *,
    os_name: str,
    wait_for_supplement: bool = False,
    consume_unknown: bool = False,
) -> Tuple[bool, bool]:
    """
    Shared slash-command dispatcher.
    Returns: (handled, should_exit)
    """
    bl = str(builtin_line or "").strip().lower()
    if not bl:
        return False, False

    mcp_tool, mcp_args, mcp_err = agent._parse_mcp_shortcut_command(builtin_line)
    if mcp_tool:
        mcp_res = agent.execute_tool_call(mcp_tool, mcp_args)
        agent._print_mcp_shortcut_result(
            mcp_tool, mcp_args, mcp_res if isinstance(mcp_res, dict) else {}
        )
        return True, False

    if bl == "mcp" or bl.startswith("mcp "):
        print(_t(agent, "Error: {error}", "错误：{error}").format(error=mcp_err))
        return True, False

    if bl in ("exit", "quit"):
        agent._save_current_workspace_position()
        if wait_for_supplement:
            print(f"Exiting {get_app_name()}.")
        return True, True

    if bl == "clear screen":
        os.system("cls" if os_name == "nt" else "clear")
        agent._suppress_next_separator = True
        return True, False

    if bl == "clear":
        print(_t(agent, "Usage: /clear <screen|input history|context>", "用法：/clear <screen|input history|context>"))
        return True, False

    if bl == "clear input history":
        agent.history_manager.clear_history()
        if agent.input_handler is not None and hasattr(
            agent.input_handler, "reset_command_history"
        ):
            agent.input_handler.reset_command_history(
                agent.history_manager.get_all_history()
            )
        print(_t(agent, "History cleared.", "历史记录已清除。"))
        return True, False

    if bl == "clear context":
        agent._clear_active_chat_context_and_tasks()
        print(_t(agent, "AI context and recorded tasks cleared.", "AI 上下文和已记录的任务已清除。"))
        try:
            agent._handle_chat_builtin_command("chat reload")
        except Exception:
            pass
        return True, False

    if bl == "compact":
        svc = getattr(agent, "session_memory_service", None)
        compact_fn = getattr(svc, "compact_context", None)
        if callable(compact_fn):
            compact_fn(mode="manual")
        else:
            print(_t(agent, "Context compaction is unavailable.", "当前无法进行上下文压缩。"))
        return True, False

    if handle_language_builtin_command(agent, builtin_line):
        return True, False

    if agent._handle_model_builtin_command(builtin_line):
        return True, False

    if agent._handle_chat_builtin_command(builtin_line):
        return True, False

    if agent._handle_workspace_builtin_command(builtin_line):
        return True, False

    if bl.startswith("execution-policy "):
        policy = bl.split(" ", 1)[1].strip().lower()
        if policy == "show":
            agent._print_execution_policy_details()
        elif policy:
            agent.execute_tool_call("execution_policy_set", {"policy": policy})
        else:
            print("Usage: /execution-policy <show|unlimited|moderate|confirmation>")
        return True, False

    if bl == "execution-policy":
        print("Usage: /execution-policy <show|unlimited|moderate|confirmation>")
        return True, False

    if bl == "always_confirm-reset":
        agent.execute_tool_call("always_confirm_reset", {})
        return True, False

    if bl == "memory status":
        agent._print_memory_status_details()
        return True, False

    if bl == "memory enable":
        agent.memory_enabled = True
        ok = agent._save_memory_enabled_to_config()
        print(
            "Memory enabled" + (
                "; saved to config.jsonc" if ok else " (failed to persist; session-only)"
            )
        )
        return True, False

    if bl == "memory disable":
        agent.memory_enabled = False
        ok = agent._save_memory_enabled_to_config()
        print(
            "Memory disabled" + (
                "; saved to config.jsonc" if ok else " (failed to persist; session-only)"
            )
        )
        return True, False

    if wait_for_supplement and bl == "help":
        print(_t(agent, "/help is available. Enter normal text to continue the paused task.", "/help 可用。请输入普通文本以继续暂停的任务。"))
        return True, False

    if consume_unknown:
        print(_t(agent, "Unknown built-in command. Use /help.", "无法识别的内置命令。请使用 /help。"))
        return True, False

    return False, False
