from __future__ import annotations

import os
from typing import Any, Tuple

from ..config.app_info import get_app_name
from ..commands import is_command, run_command
from .language_command_controller import handle_language_builtin_command
from .mcp_shortcut_controller import format_mcp_shortcut_error


def _t(agent: Any, key: str, **kwargs: Any) -> str:
    from ..core.localization import get_display_language, translate

    return translate(key, get_display_language(agent), **kwargs)


def _persist_plan_mode(agent: Any, enabled: bool) -> None:
    """Mirror the sticky Plan-mode flag onto the active chat record root.

    Best-effort: the in-memory ``_plan_mode_sticky`` flag has already been set
    by the caller; this only records it so a chat reload resumes the same mode.
    """
    manager = getattr(agent, "_chat_state_manager", None)
    persist = getattr(manager, "persist_active_chat_plan_mode", None)
    if not callable(persist):
        return
    try:
        persist(bool(enabled))
    except Exception:
        pass


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
        if is_command(mcp_tool):
            mcp_res = run_command(agent, mcp_tool, mcp_args)
        else:
            mcp_res = agent.execute_tool_call(mcp_tool, mcp_args)
        agent._print_mcp_shortcut_result(
            mcp_tool, mcp_args, mcp_res if isinstance(mcp_res, dict) else {}
        )
        return True, False

    if bl == "mcp" or bl.startswith("mcp "):
        print(_t(agent, "common.error", error=format_mcp_shortcut_error(agent, mcp_err)))
        return True, False

    if bl in ("exit", "quit"):
        if wait_for_supplement:
            print(_t(agent, "builtin.exiting_app", app_name=get_app_name()))
        return True, True

    if bl == "clear screen":
        os.system("cls" if os_name == "nt" else "clear")
        agent._suppress_next_separator = True
        return True, False

    if bl == "clear":
        print(_t(agent, "builtin.clear_usage"))
        return True, False

    if bl == "clear input history":
        agent.history_manager.clear_history()
        if agent.input_handler is not None and hasattr(
            agent.input_handler, "reset_command_history"
        ):
            agent.input_handler.reset_command_history(
                agent.history_manager.get_all_history()
            )
        print(_t(agent, "builtin.history_cleared"))
        return True, False

    if bl == "clear context":
        agent._clear_active_chat_context_and_tasks()
        print(_t(agent, "builtin.context_cleared"))
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
            print(_t(agent, "builtin.compaction_unavailable"))
        return True, False

    if handle_language_builtin_command(agent, builtin_line):
        return True, False

    if bl == "reasoning" or bl.startswith("reasoning "):
        if bl == "reasoning":
            level = ""
        else:
            level = builtin_line.strip()[len("reasoning "):].strip()
            if level.lower() == "default":
                level = ""
        print(agent._set_reasoning_effort(level))
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
            print(_t(agent, "builtin.execution_policy_usage"))
        return True, False

    if bl == "execution-policy":
        print(_t(agent, "builtin.execution_policy_usage"))
        return True, False

    if bl == "always_confirm-reset":
        result = agent._reset_always_confirm_skip()
        if isinstance(result, dict) and result.get("success"):
            print(result.get("message", "always-confirm skip list reset"))
        return True, False

    if bl == "memory status":
        agent._print_memory_status_details()
        return True, False

    if bl == "memory enable":
        agent.memory_enabled = True
        ok = agent._save_memory_enabled_to_config()
        print(_t(agent, "builtin.memory_enabled_saved" if ok else "builtin.memory_enabled_session_only"))
        return True, False

    if bl == "memory disable":
        agent.memory_enabled = False
        ok = agent._save_memory_enabled_to_config()
        print(_t(agent, "builtin.memory_disabled_saved" if ok else "builtin.memory_disabled_session_only"))
        return True, False

    # Plan-mode toggle. Plan mode is a session-sticky flag that asks the agent
    # to outline a step-by-step plan and hold off on destructive tool calls
    # until the user confirms. The runtime loop reads the flag and appends a
    # planning directive to every message it sends to the model. The flag is
    # also mirrored onto the active chat record root so reloading the chat
    # resumes the same mode.
    if bl in ("plan", "plan on", "plan status"):
        if bl == "plan status":
            on = bool(getattr(agent, "_plan_mode_sticky", False))
            print(
                _t(
                    agent,
                    "builtin.plan_mode_status_on" if on else "builtin.plan_mode_status_off",
                )
            )
            return True, False
        agent._plan_mode_sticky = True
        _persist_plan_mode(agent, True)
        print(_t(agent, "builtin.plan_mode_on"))
        return True, False

    if bl in ("plan off", "agent"):
        agent._plan_mode_sticky = False
        _persist_plan_mode(agent, False)
        print(_t(agent, "builtin.plan_mode_off"))
        return True, False

    if wait_for_supplement and bl == "help":
        print(_t(agent, "builtin.help_available_for_paused_task"))
        return True, False

    if consume_unknown:
        print(_t(agent, "builtin.unknown_command"))
        return True, False

    return False, False
