"""Runtime main loop extracted from SmartShellAgent.run."""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ..config.app_info import get_app_display_version, get_app_name
from ..config.startup_tips import (
    format_tip_with_highlights,
    get_random_startup_tip_entry,
)
from ..core.assistant_output_highlighter import format_assistant_display_response
from ..core.logging.app_logging import get_log_file_path, get_logger
from ..controllers.builtin_command_router import dispatch_builtin_command
from ..core.console_utils import (
    _ansi_bold,
    _ansi_gray,
    _ansi_cyan,
    _WorkingStatusTicker,
    _ansi_yellow,
)

_CODE_MUTATION_TOOLS = {
    "apply_patch",
}
_WORKING_STATUS_MARQUEE_FPS = 10.0


def _is_software_development_domain(domains: List[str]) -> bool:
    return "software_development" in {str(x or "").strip() for x in (domains or [])}


def _estimate_visible_lines(agent: Any, text: str) -> int:
    s = str(text or "")
    if not s:
        return 0
    fn = getattr(agent, "_estimate_rendered_line_count", None)
    if callable(fn):
        try:
            return max(0, int(fn(s)))
        except Exception:
            pass
    normalized = s.replace("\r\n", "\n").replace("\r", "\n")
    parts = normalized.split("\n")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return max(0, len(parts))


def _shell_command_indicates_verification(command: str) -> bool:
    c = str(command or "").strip().lower()
    if not c:
        return False
    needles = (
        "pytest",
        "unittest",
        "py_compile",
        "mypy",
        "ruff",
        "flake8",
        "eslint",
        "npm test",
        "pnpm test",
        "yarn test",
        "go test",
        "cargo test",
        "gradle test",
        "mvn test",
        "ctest",
    )
    return any(n in c for n in needles)


def _tool_change_and_verification_hints(
    tool_name: str,
    args: Dict[str, Any],
    result: Dict[str, Any],
) -> Dict[str, Any]:
    t = str(tool_name or "").strip()
    out: Dict[str, Any] = {
        "code_changed": False,
        "changed_files": [],
        "verified": False,
        "verification_summary": "",
    }

    if t in _CODE_MUTATION_TOOLS:
        out["code_changed"] = True
        p = str(args.get("path") or "").strip()
        if p:
            out["changed_files"] = [p]

    if t == "shell":
        cmd = str(args.get("command") or "").strip()
        if _shell_command_indicates_verification(cmd):
            ok = bool(result.get("success", False))
            out["verified"] = ok
            status = "passed" if ok else "failed"
            out["verification_summary"] = f"shell verification `{cmd}` => {status}"
    return out


def _build_minimal_verification_command(changed_files: List[str]) -> str:
    files = [str(x or "").strip() for x in (changed_files or []) if str(x or "").strip()]
    py_files = [f for f in files if f.lower().endswith(".py")]
    if py_files:
        joined = " ".join(f'"{f}"' for f in py_files)
        return f"python -m py_compile {joined}"
    return "请先执行最小验证（相关测试、编译或静态检查）"


def _emit_flow_log(message: str) -> None:
    msg = f"[Flow] {message}"
    try:
        get_logger("smartshell.runtime.flow").info(msg)
    except Exception:
        pass


def _sanitize_prompt_pollution(text: str, work_directory: Any) -> str:
    s = str(text or "")
    if not s:
        return s
    cleaned = s.replace("\r", "").strip()
    if not cleaned:
        return ""

    wd = str(work_directory or "").strip()
    if wd:
        prompt_prefix = f"{wd}>"
        while cleaned.startswith(prompt_prefix):
            cleaned = cleaned[len(prompt_prefix):].lstrip()
    while cleaned.startswith("›"):
        cleaned = cleaned[1:].lstrip()

    if re.match(r"^>{2,}\s*\S", cleaned):
        cleaned = re.sub(r"^>+\s*", "", cleaned, count=1)

    return cleaned


def _format_startup_directory(workspace_dir: Any) -> str:
    raw = str(workspace_dir or "")
    if not raw:
        return raw
    try:
        current = Path(raw).expanduser().resolve(strict=False)
        home = Path.home().resolve(strict=False)
        relative = current.relative_to(home)
    except Exception:
        return raw

    rel_text = str(relative)
    if not rel_text or rel_text == ".":
        return "~"
    return f"~{os.sep}{rel_text}"


def _print_startup_overview(agent: Any) -> None:
    model_name = str(getattr(agent, "model_name", "") or "")
    workspace_name = str(getattr(agent, "workspace_name", "") or "")
    workspace_dir = _format_startup_directory(getattr(agent, "workspace_root", "") or "")
    app_name = get_app_name()
    version = get_app_display_version()

    line1 = f">_ {app_name} ({version})"
    line2 = f"model:     {model_name}  /model to change"
    line3 = f"workspace: {workspace_name}"
    line4 = f"directory: {workspace_dir}"

    width = max(len(line1), len(line2), len(line3), len(line4)) + 2
    top = "╭" + ("─" * width) + "╮"
    mid1 = "│ " + line1.ljust(width - 1) + "│"
    mid2 = "│" + (" " * width) + "│"
    mid3 = "│ " + line2.ljust(width - 1) + "│"
    mid4 = "│ " + line3.ljust(width - 1) + "│"
    mid5 = "│ " + line4.ljust(width - 1) + "│"
    bottom = "╰" + ("─" * width) + "╯"

    print(_ansi_gray(top))
    # Header line: use terminal default foreground for main text, prefix gray.
    print(
        _ansi_gray("│ ")
        + _ansi_gray(">_ ")
        + app_name
        + _ansi_gray(f" ({version})")
        + _ansi_gray(" " * max(0, width - 1 - len(line1)))
        + _ansi_gray("│")
    )
    print(_ansi_gray(mid2))
    # model line
    prefix_model = "model:     "
    print(
        _ansi_gray("│ ")
        + _ansi_gray(prefix_model)
        + model_name
        + _ansi_gray("  ")
        + _ansi_cyan("/model")
        + _ansi_gray(" to change")
        + _ansi_gray(" " * max(0, width - 1 - len(line2)))
        + _ansi_gray("│")
    )
    # workspace line
    prefix_workspace = "workspace: "
    print(
        _ansi_gray("│ ")
        + _ansi_gray(prefix_workspace)
        + workspace_name
        + _ansi_gray(" " * max(0, width - 1 - len(line3)))
        + _ansi_gray("│")
    )
    # directory line
    prefix_directory = "directory: "
    print(
        _ansi_gray("│ ")
        + _ansi_gray(prefix_directory)
        + workspace_dir
        + _ansi_gray(" " * max(0, width - 1 - len(line4)))
        + _ansi_gray("│")
    )
    print(_ansi_gray(bottom))
    startup_chat_warning = str(getattr(agent, "_startup_chat_state_warning", "") or "").strip()
    if startup_chat_warning:
        print(_ansi_yellow(startup_chat_warning))
    print("")
    tip_entry = get_random_startup_tip_entry()
    tip_text = str(tip_entry.get("text") or "")
    highlights_raw = tip_entry.get("highlights", [])
    highlights = highlights_raw if isinstance(highlights_raw, list) else []
    rendered_tip = format_tip_with_highlights(
        text=tip_text,
        highlights=[str(h or "") for h in highlights],
        highlight_formatter=_ansi_cyan,
    )
    print("  " + _ansi_bold("Tip: ") + rendered_tip)
    print("")


def run_agent_loop(agent: Any):
    """Run the AI Agent main loop with multi-step tool execution until done."""
    from .. import smart_shell_agent as _ssa
    KNOWLEDGE_AVAILABLE = getattr(_ssa, "KNOWLEDGE_AVAILABLE", False)
    self = agent
    import sys
    import os
    os_name = os.name

    # 启动时提示知识库状态（功能始终开启；仅依赖或初始化失败时提示）
    if not KNOWLEDGE_AVAILABLE:
        if sys.version_info >= (3, 14):
            print(
                "Knowledge base dependencies are unavailable on the current Python version; the main program can continue running. "
                "Use Python 3.12 or 3.13 and install knowledge base dependencies."
            )
        else:
            print(
                "Knowledge base dependencies are not ready; the main program can continue running. "
                "Install the knowledge-related packages from requirements when needed."
            )
    elif KNOWLEDGE_AVAILABLE and self.knowledge_manager is not None:
        svc = self.knowledge_manager
        if svc.is_ready() and not svc.is_available():
            lp = get_log_file_path()
            print(
                "Knowledge base initialization failed; please check logs"
                + (f" ({lp})" if lp else "")
                + ", and verify sentence-transformers, network access (first run needs model download), "
                + "and the config directory workspace/knowledge/."
            )

    if self.skills:
        _sk_path = self.config_dir / "skills"

    _print_startup_overview(self)
    try:
        sys.stdout.flush()
    except Exception:
        pass

    import subprocess
    import re
    system_cmd_patterns = [
        r'^cd(\s+.+)?$',
        r'^(dir|ls|list)(\s+.+)?$',
        r'^(del|delete|rm)(\s+.+)?$',
        r'^(ping)(\s+.+)?$',
        r'^(ipconfig|ifconfig)(\s+.+)?$',
        r'^(type|cat)(\s+.+)?$',
        r'^(echo)(\s+.+)?$',
        r'^(whoami|hostname|date|time)(\s+.+)?$',
        r'^(wmic|net)(\s+.+)?$',
    ]
    system_cmd_re = re.compile('|'.join(system_cmd_patterns), re.IGNORECASE)

    while True:
        in_task_execution = False
        self._in_task_execution = False
        current_task_id = ""
        pre_task_status_ticker: Optional[_WorkingStatusTicker] = None
        try:
            self._refresh_input_handler_skill_completions()
            # 获取用户输入（含等待态回注输入），统一走主循环处理路径
            if getattr(self, "_queued_user_input", None) is not None:
                user_input = str(self._queued_user_input or "")
                self._queued_user_input = None
            else:
                user_input = self._get_user_input_with_history()
            user_input = _sanitize_prompt_pollution(user_input, self.work_directory)
            raw_user_input = str(user_input or "")
        
            # 保存到历史记录（非空输入）
            if user_input.strip():
                self.history_manager.add_entry(user_input)
                # 同步输入处理器内存历史（如 prompt_toolkit），确保上下键与持久化去重结果一致。
                if self.input_handler is not None and hasattr(
                    self.input_handler, "reset_command_history"
                ):
                    self.input_handler.reset_command_history(
                        self.history_manager.get_all_history()
                    )

            stripped_in = user_input.strip()
            if not stripped_in:
                continue

            forced_mcp = self._extract_forced_mcp_reference(stripped_in)
            forced_mcp_entries: List[Dict[str, str]] = (
                list(forced_mcp.get("entries", [])) if forced_mcp else []
            )

            forced_skill: Optional[Dict[str, Any]] = self._extract_forced_skill_reference(stripped_in)
            forced_skills: List[Dict[str, str]] = (
                list(forced_skill.get("skills", [])) if forced_skill else []
            )
            # When slash references are present, route the remaining natural-language
            # part as the task text to avoid the model treating "/skills/<skill-name>" itself as work.
            task_user_input = stripped_in
            try:
                if forced_mcp and str(forced_mcp.get("rest") or "").strip():
                    task_user_input = str(forced_mcp.get("rest") or "").strip()
                if forced_skill and str(forced_skill.get("rest") or "").strip():
                    task_user_input = str(forced_skill.get("rest") or "").strip()
            except Exception:
                task_user_input = stripped_in

            # Built-in slash commands use "/" prefix; direct shell uses "!" prefix.
            builtin_line: Optional[str] = None
            if stripped_in.startswith("/") and not forced_skills and not forced_mcp_entries:
                builtin_line = stripped_in[1:].lstrip()
                if not builtin_line:
                    print(
                        "ℹ️ Built-in commands must start with /. "
                        "For example: /exit, /help, /clear screen, /knowledge status, /memory status; "
                        "a standalone / is invalid. "
                        "For local commands/scripts executed without AI, use ! prefix, e.g. !ls, !git status."
                    )
                    continue

            if builtin_line is not None:
                handled, should_exit = dispatch_builtin_command(
                    self,
                    builtin_line,
                    os_name=os_name,
                    wait_for_supplement=False,
                    consume_unknown=False,
                )
                if handled:
                    if should_exit:
                        break
                    continue

                bl = builtin_line.lower()
                mcp_tool, mcp_args, mcp_err = self._parse_mcp_shortcut_command(builtin_line)
                if mcp_tool:
                    mcp_res = self.execute_tool_call(mcp_tool, mcp_args)
                    self._print_mcp_shortcut_result(mcp_tool, mcp_args, mcp_res if isinstance(mcp_res, dict) else {})
                    continue
                if bl == "mcp" or bl.startswith("mcp "):
                    print(f"❌ {mcp_err}")
                    continue
                if bl in ('exit', 'quit'):
                    self._save_current_workspace_position()
                    break
                # clear screen
                if bl == 'cls' or bl == 'clear screen':
                    os.system('cls' if os_name == 'nt' else 'clear')
                    self._suppress_next_separator = True
                    continue
                if bl == "clear":
                    print("Usage: /clear <screen|history|context>")
                    continue
                if bl == 'clear history':
                    self.history_manager.clear_history()
                    if self.input_handler is not None and hasattr(
                        self.input_handler, "reset_command_history"
                    ):
                        self.input_handler.reset_command_history(
                            self.history_manager.get_all_history()
                        )
                    print("✅ History has been cleared")
                    continue
                if bl == "clear context":
                    self.conversation_history.clear()
                    self._sync_active_chat_messages()
                    self.operation_results.clear()
                    self._last_auto_removed_ephemeral = None
                    self._session_summary_llm = ""
                    self._session_summary_rolling = ""
                    self._last_llm_summary_pair_count = 0
                    print(
                        "✅ AI context has been cleared "
                        "(conversation history and recent operation-result cache; command-line input history is unchanged)"
                    )
                    continue
                if bl == "knowledge":
                    print("Usage: /knowledge <status|sync|stats|search <query>>")
                    continue
                if bl == "knowledge status":
                    self._print_knowledge_status_details()
                    continue

                if bl == "memory":
                    print("Usage: /memory <enable|disable|status|stats|list|search <query>|remember <text>|delete <id>>")
                    continue
                if bl == "memory enable":
                    self.memory_enabled = True
                    ok = self._save_memory_enabled_to_config()
                    print(
                        "✅ Experiential memory is enabled"
                        + ("; saved to config.json" if ok else " (failed to save config; only effective for this process)")
                    )
                    continue
                if bl == "memory disable":
                    self.memory_enabled = False
                    ok = self._save_memory_enabled_to_config()
                    print(
                        "✅ Experiential memory is disabled"
                        + ("; saved to config.json" if ok else " (failed to save config; only effective for this process)")
                    )
                    continue
                if bl == "memory status":
                    self._print_memory_status_details()
                    continue
                if bl == "memory stats":
                    self.execute_tool_call("memory_stats", {"verbose_print": True})
                    continue
                if bl == "memory list":
                    self.execute_tool_call(
                        "memory_list", {"limit": 20, "verbose_print": True}
                    )
                    continue
                if bl.startswith("memory search "):
                    q = builtin_line[len("memory search ") :].strip()
                    if q:
                        self.execute_tool_call(
                            "memory_search", {"query": q, "verbose_print": True}
                        )
                    else:
                        print("❌ Please provide search content")
                    continue
                if bl.startswith("memory remember "):
                    text = builtin_line[len("memory remember ") :].strip()
                    if not text:
                        print("❌ Please provide content to remember")
                        continue
                    title = text[:80] + ("…" if len(text) > 80 else "")
                    self.execute_tool_call(
                        "memory_add",
                        {
                            "title": title,
                            "content": text,
                            "tier": "episodic",
                            "memory_type": "preference",
                            "source": "user_request",
                            "user_request": text,
                            "verbose_print": True,
                        },
                    )
                    continue
                if bl.startswith("memory delete "):
                    mid = builtin_line[len("memory delete ") :].strip()
                    if mid:
                        self.execute_tool_call(
                            "memory_delete",
                            {"memory_id": mid, "verbose_print": True},
                        )
                    else:
                        print("❌ Please provide memory id")
                    continue

                if self._handle_chat_builtin_command(builtin_line):
                    continue

                if self._handle_workspace_builtin_command(builtin_line):
                    continue

                if bl.startswith("execution-policy "):
                    policy = ""
                    policy = bl.split(" ", 1)[1].strip().lower()
                    if policy == "show":
                        self._print_execution_policy_details()
                        continue
                    if not policy:
                        print("Usage: /execution-policy <show|unlimited|moderate|confirmation>")
                    else:
                        self.execute_tool_call("execution_policy_set", {"policy": policy})
                    continue
                if bl == "execution-policy":
                    print("Usage: /execution-policy <show|unlimited|moderate|confirmation>")
                    continue

                if bl.startswith("session-summary "):
                    sub = bl[len("session-summary ") :].strip().lower()
                    if sub in ("on", "enable", "true", "1"):
                        self.session_summary_llm_enabled = True
                        ok = self._save_session_summary_llm_to_config()
                        print(
                            f"✅ Session LLM summary enabled (periodic compression for experiential-memory retrieval query)"
                            f"{'; saved to config.json' if ok else ' (failed to save config; only effective for this process)'}"
                        )
                        continue
                    if sub in ("off", "disable", "false", "0"):
                        self.session_summary_llm_enabled = False
                        ok = self._save_session_summary_llm_to_config()
                        print(
                            f"✅ Session LLM summary disabled (rolling excerpts are still kept)"
                            f"{'; saved to config.json' if ok else ' (failed to save config; only effective for this process)'}"
                        )
                        continue
                    if sub == "show":
                        on = bool(getattr(self, "session_summary_llm_enabled", True))
                        cfg_path = self.config_dir / "config.json"
                        print(
                            f"Session LLM summary (session_summary_llm): {'on' if on else 'off'}\n"
                            f"  Config key: \"session_summary_llm\" in config.json (boolean)\n"
                            f"  Config file: {cfg_path}"
                        )
                        continue
                    print(
                        "Usage: /session-summary <on|off|show>\n"
                        "  on/off   - toggle periodic LLM session summary (rolling excerpt remains when off)\n"
                        "  show     - show current switch and config file path"
                    )
                    continue
                if bl == "session-summary":
                    print(
                        "Usage: /session-summary <on|off|show>\n"
                        "  /session-summary on     - enable LLM session summary\n"
                        "  /session-summary off    - disable (rolling excerpt only)\n"
                        "  /session-summary show   - show status"
                    )
                    continue

                if bl == "always_confirm-reset":
                    self.execute_tool_call("always_confirm_reset", {})
                    continue

                if bl == 'knowledge sync':
                    self.execute_tool_call("knowledge_sync", {})
                    continue

                if bl == 'knowledge stats':
                    self.execute_tool_call("knowledge_stats", {})
                    continue

                if bl.startswith('knowledge search '):
                    query = builtin_line[len('knowledge search ') :]
                    if query.strip():
                        self.execute_tool_call("knowledge_search", {"query": query.strip()})
                    else:
                        print("❌ Please provide search query content")
                    continue
                if bl == 'help':

                    self._print_main_help()

                    continue

                print(
                    "❌ Unrecognized built-in command. Use /help to view the list. "
                    "For direct local shell/script execution, use ! prefix, e.g. !git status, !dir."
                )
                continue

            # Direct local execution without AI: requires leading "!" on all platforms.
            run_direct_shell: Optional[str] = None
            if stripped_in.startswith("!"):
                run_direct_shell = stripped_in[1:].lstrip()
                if not run_direct_shell:
                    print(
                        "ℹ️ System commands/executables executed directly (without AI) must start with !, "
                        "for example !ls, !dir, !ping 127.0.0.1, !git status; a standalone ! is invalid."
                    )
                    continue

            if run_direct_shell is not None:
                ui = run_direct_shell
                self._print_direct_shell_command_feedback(ui, failed=False)
                execution_cwd = self._shell_execution_cwd()
                raw_user_direct_cmd = f"!{ui}"
                try:
                    self._last_direct_shell_execution = None
                except Exception:
                    pass
                if self._is_executable_file(ui):
                    exec_ok = bool(self._execute_file_directly(ui))
                    last_direct = getattr(self, "_last_direct_shell_execution", None)
                    if not exec_ok:
                        rendered_lines = 0
                        cursor_at_line_start = True
                        if isinstance(last_direct, dict):
                            try:
                                rendered_lines = int(last_direct.get("rendered_output_lines") or 0)
                            except Exception:
                                rendered_lines = 0
                            cursor_at_line_start = bool(last_direct.get("cursor_at_line_start", True))
                        else:
                            rendered_lines = _estimate_visible_lines(
                                self, "❌ 执行文件失败\n"
                            )
                        self._repaint_direct_shell_command_feedback_if_failed(
                            ui,
                            rendered_output_lines=rendered_lines,
                            cursor_at_line_start=cursor_at_line_start,
                            failed=True,
                        )
                    if isinstance(last_direct, dict):
                        self._record_direct_shell_execution_history(
                            raw_user_command=raw_user_direct_cmd,
                            executed_command=str(last_direct.get("executed_command") or ui),
                            cwd=str(last_direct.get("cwd") or execution_cwd),
                            return_code=int(last_direct.get("return_code") if last_direct.get("return_code") is not None else (0 if exec_ok else 1)),
                            stdout_text=str(last_direct.get("stdout") or ""),
                            stderr_text=str(last_direct.get("stderr") or ""),
                            aborted_by_user=bool(last_direct.get("aborted_by_user", False)),
                        )
                    else:
                        self._record_direct_shell_execution_history(
                            raw_user_command=raw_user_direct_cmd,
                            executed_command=ui,
                            cwd=str(execution_cwd),
                            return_code=0 if exec_ok else 1,
                            stdout_text="",
                            stderr_text="" if exec_ok else "Command execution failed (no detailed output captured)\n",
                        )
                    self._show_separator_next_prompt = not self._is_direct_shell_result_aborted(last_direct)
                    continue

                user_input_cmd = ui
                if system_cmd_re.match(ui):
                    current_direct_result = None
                    if user_input_cmd.lower().startswith('ls') and os_name == 'nt':
                        user_input_cmd = 'dir ' + user_input_cmd[2:].strip()
                    elif user_input_cmd.lower().startswith('list') and os_name == 'nt':
                        user_input_cmd = 'dir ' + user_input_cmd[4:].strip()
                    elif user_input_cmd.lower().startswith('dir') and os_name != 'nt':
                        user_input_cmd = 'ls ' + user_input_cmd[3:].strip()

                    try:
                        if user_input_cmd.lower().startswith('cd '):
                            cd_stdout = ""
                            cd_stderr = ""
                            cd_return_code = 0
                            path = user_input_cmd[3:].strip()
                            try:
                                if path == "..":
                                    new_path = execution_cwd.parent
                                elif path == ".":
                                    new_path = execution_cwd
                                else:
                                    raw_path = Path(path)
                                    if raw_path.is_absolute():
                                        new_path = raw_path
                                    else:
                                        new_path = execution_cwd / raw_path
                                new_path = new_path.resolve()
                                if not new_path.exists():
                                    msg = f"❌ Directory '{path}' does not exist"
                                    print(msg)
                                    cd_stderr = f"{msg}\n"
                                    cd_return_code = 1
                                elif not new_path.is_dir():
                                    msg = f"❌ '{path}' is not a directory"
                                    print(msg)
                                    cd_stderr = f"{msg}\n"
                                    cd_return_code = 1
                                else:
                                    self.work_directory = new_path
                                    if self.input_handler:
                                        self.input_handler.update_work_directory(new_path)
                                    self._save_current_workspace_position()
                                    self._reset_work_directory_to_startup_initial()
                            except Exception as e:
                                msg = f"❌ Failed to change directory: {e}"
                                print(msg)
                                cd_stderr = f"{msg}\n"
                                cd_return_code = 1
                            if cd_return_code != 0:
                                self._repaint_direct_shell_command_feedback_if_failed(
                                    ui,
                                    rendered_output_lines=_estimate_visible_lines(
                                        self, cd_stderr
                                    ),
                                    cursor_at_line_start=True,
                                    failed=True,
                                )
                            self._record_direct_shell_execution_history(
                                raw_user_command=raw_user_direct_cmd,
                                executed_command=user_input_cmd,
                                cwd=str(execution_cwd),
                                return_code=cd_return_code,
                                stdout_text=cd_stdout,
                                stderr_text=cd_stderr,
                            )
                        else:
                            return_code: Optional[int] = None
                            try:
                                return_code = self._run_direct_shell_with_prefixed_output(
                                    user_input_cmd,
                                    execution_cwd,
                                )
                            except Exception as e:
                                msg = f"❌ Command execution error: {e}"
                                print(msg)
                                self._repaint_direct_shell_command_feedback_if_failed(
                                    ui,
                                    rendered_output_lines=_estimate_visible_lines(
                                        self, f"{msg}\n"
                                    ),
                                    cursor_at_line_start=True,
                                    failed=True,
                                )
                                self._record_direct_shell_execution_history(
                                    raw_user_command=raw_user_direct_cmd,
                                    executed_command=user_input_cmd,
                                    cwd=str(execution_cwd),
                                    return_code=1,
                                    stdout_text="",
                                    stderr_text=f"{msg}\n",
                                )
                            finally:
                                self._reset_work_directory_to_startup_initial()
                            if return_code is not None:
                                last_direct = getattr(self, "_last_direct_shell_execution", None)
                                current_direct_result = last_direct
                                if int(return_code) != 0:
                                    rendered_lines = 0
                                    cursor_at_line_start = True
                                    if isinstance(last_direct, dict):
                                        try:
                                            rendered_lines = int(last_direct.get("rendered_output_lines") or 0)
                                        except Exception:
                                            rendered_lines = 0
                                        cursor_at_line_start = bool(
                                            last_direct.get("cursor_at_line_start", True)
                                        )
                                    self._repaint_direct_shell_command_feedback_if_failed(
                                        ui,
                                        rendered_output_lines=rendered_lines,
                                        cursor_at_line_start=cursor_at_line_start,
                                        failed=True,
                                    )
                                if isinstance(last_direct, dict):
                                    self._record_direct_shell_execution_history(
                                        raw_user_command=raw_user_direct_cmd,
                                        executed_command=str(last_direct.get("executed_command") or user_input_cmd),
                                        cwd=str(last_direct.get("cwd") or execution_cwd),
                                        return_code=int(last_direct.get("return_code") if last_direct.get("return_code") is not None else return_code),
                                        stdout_text=str(last_direct.get("stdout") or ""),
                                        stderr_text=str(last_direct.get("stderr") or ""),
                                        aborted_by_user=bool(last_direct.get("aborted_by_user", False)),
                                    )
                                else:
                                    self._record_direct_shell_execution_history(
                                        raw_user_command=raw_user_direct_cmd,
                                        executed_command=user_input_cmd,
                                        cwd=str(execution_cwd),
                                        return_code=int(return_code),
                                        stdout_text="",
                                        stderr_text="",
                                    )
                    except Exception as e:
                        msg = f"❌ System command execution error: {e}"
                        print(msg)
                        self._repaint_direct_shell_command_feedback_if_failed(
                            ui,
                            rendered_output_lines=_estimate_visible_lines(
                                self, f"{msg}\n"
                            ),
                            cursor_at_line_start=True,
                            failed=True,
                        )
                        self._record_direct_shell_execution_history(
                            raw_user_command=raw_user_direct_cmd,
                            executed_command=user_input_cmd,
                            cwd=str(execution_cwd),
                            return_code=1,
                            stdout_text="",
                            stderr_text=f"{msg}\n",
                        )
                    self._show_separator_next_prompt = not self._is_direct_shell_result_aborted(
                        current_direct_result
                    )
                    continue

                # e.g. !git status — not in the small whitelist but still direct shell
                return_code: Optional[int] = None
                last_direct = None
                try:
                    return_code = self._run_direct_shell_with_prefixed_output(
                        ui,
                        execution_cwd,
                    )
                except Exception as e:
                    msg = f"❌ Command execution error: {e}"
                    print(msg)
                    self._repaint_direct_shell_command_feedback_if_failed(
                        ui,
                        rendered_output_lines=_estimate_visible_lines(
                            self, f"{msg}\n"
                        ),
                        cursor_at_line_start=True,
                        failed=True,
                    )
                    self._record_direct_shell_execution_history(
                        raw_user_command=raw_user_direct_cmd,
                        executed_command=ui,
                        cwd=str(execution_cwd),
                        return_code=1,
                        stdout_text="",
                        stderr_text=f"{msg}\n",
                    )
                finally:
                    self._reset_work_directory_to_startup_initial()
                if return_code is not None:
                    last_direct = getattr(self, "_last_direct_shell_execution", None)
                    if int(return_code) != 0:
                        rendered_lines = 0
                        cursor_at_line_start = True
                        if isinstance(last_direct, dict):
                            try:
                                rendered_lines = int(last_direct.get("rendered_output_lines") or 0)
                            except Exception:
                                rendered_lines = 0
                            cursor_at_line_start = bool(last_direct.get("cursor_at_line_start", True))
                        self._repaint_direct_shell_command_feedback_if_failed(
                            ui,
                            rendered_output_lines=rendered_lines,
                            cursor_at_line_start=cursor_at_line_start,
                            failed=True,
                        )
                    if isinstance(last_direct, dict):
                        self._record_direct_shell_execution_history(
                            raw_user_command=raw_user_direct_cmd,
                            executed_command=str(last_direct.get("executed_command") or ui),
                            cwd=str(last_direct.get("cwd") or execution_cwd),
                            return_code=int(last_direct.get("return_code") if last_direct.get("return_code") is not None else return_code),
                            stdout_text=str(last_direct.get("stdout") or ""),
                            stderr_text=str(last_direct.get("stderr") or ""),
                            aborted_by_user=bool(last_direct.get("aborted_by_user", False)),
                        )
                    else:
                        self._record_direct_shell_execution_history(
                            raw_user_command=raw_user_direct_cmd,
                            executed_command=ui,
                            cwd=str(execution_cwd),
                            return_code=int(return_code),
                            stdout_text="",
                            stderr_text="",
                        )
                self._show_separator_next_prompt = not self._is_direct_shell_result_aborted(last_direct)
                continue

            # Natural-language turn: rewrite prompt line as chat-style user line.
            in_task_execution = True
            self._in_task_execution = True
            turn_send_started_at = time.perf_counter()
            _emit_flow_log(
                f"用户回车发送: chars={len(task_user_input)}, active_chat={getattr(self, 'active_chat_id', '')}"
            )
            self._start_interrupt_monitor(cancel_task_on_interrupt=True)
            self._consume_task_interrupt_requested()
            self._rewrite_previous_prompt_as_user(raw_user_input.strip())
            pre_task_status_ticker = _WorkingStatusTicker(
                sys.stdout,
                fps=_WORKING_STATUS_MARQUEE_FPS,
            )
            pre_task_status_ticker.start()

            last_result = None
            self._last_auto_removed_ephemeral = None
            original_user_task = task_user_input
            domain_classify_started_at = time.perf_counter()
            _emit_flow_log("任务领域分类开始")
            domain_info = self._classify_task_domains(original_user_task)
            domain_elapsed_ms = int((time.perf_counter() - domain_classify_started_at) * 1000)
            _emit_flow_log(
                "任务领域分类结束: "
                f"primary={domain_info.get('primary_domain', 'general_other')}, "
                f"domains={list(domain_info.get('domains') or ['general_other'])}, "
                f"elapsed_ms={domain_elapsed_ms}"
            )
            current_task_id = self._start_chat_task(
                root_user_input=original_user_task,
                domains=list(domain_info.get("domains") or []),
                classifier=domain_info,
            )
            code_changed_in_task = False
            verification_done_in_task = False
            changed_files_in_task: Set[str] = set()
            verification_evidence_in_task: List[str] = []
            in_task_execution = True
            self._active_skill_full_prompt = ""
            self._active_skill_id = None
            self._active_skill_source = None
            self._active_skill_section = 0
            self._active_skill_total_sections = 0
            self._active_skill_chunked = False
            forced_skill_prefix = ""
            forced_mcp_prefix = ""
            if forced_mcp_entries:
                for e in forced_mcp_entries:
                    srv = str(e.get("server", "")).strip()
                    name = str(e.get("name", "")).strip()
                    kind = str(e.get("kind", "")).strip() or "unknown"
                    print(f"🧩 Enabled MCP reference: /mcp/{srv}/{name} ({kind})")
                forced_mcp_prefix = self._build_forced_mcp_prefix(forced_mcp_entries)
            preloaded_skill_ids: Set[str] = set()
            if forced_skills:
                skill_items = []
                full_prompts: List[str] = []
                for s in forced_skills:
                    sid = str(s.get("skill_id") or "").strip()
                    sname = str(s.get("name") or sid).strip()
                    if not sid:
                        continue
                    skill_items.append(f"`{sname}`(skill_id=`{sid}`)")
                    full_prompt, meta = self._build_single_skill_prompt(sid)
                    if full_prompt:
                        print(f"🧩 Enabled skill: {sname}")
                        full_prompts.append(full_prompt)
                        preloaded_skill_ids.add(self._canonical_skill_id(sid))
                        if not self._active_skill_id:
                            self._active_skill_id = sid
                            self._active_skill_source = "local" if self._is_local_skill_id(sid) else "mcp"
                            self._active_skill_section = int(meta.get("section") or 0)
                            self._active_skill_total_sections = int(meta.get("total") or 0)
                            self._active_skill_chunked = bool(meta.get("chunked", False))
                if skill_items:
                    forced_skill_prefix = (
                        f"【强制技能】本轮必须优先使用以下 skills（按用户输入顺序）：{', '.join(skill_items)}，"
                        "并按各自 SKILL.md 执行。若与 AGENTS.md 或通用系统说明冲突，"
                        "以这些 skills 正文为准（安全/越权/破坏性硬限制除外）。\n\n"
                    )
                if full_prompts:
                    self._active_skill_full_prompt = "\n".join(full_prompts)
            first_round_contract = (
                "\n\n【首轮回复硬性要求（必须遵守）】\n"
                "1) 对于需要两步及以上完成的任务，先简要说明“将要完成哪些事情”，紧随其后再输出任务编排：Step 1..N，并为每步标注状态（pending/in_progress/completed/failed）。\n"
                "2) 在同一条回复结尾输出且仅输出一个工具调用 JSON。\n"
                "3) 仅当当前会话尚未注入目标 skill 正文时，优先请求 skill 提示；"
                "若该 skill 已注入（例如用户通过 `/skills/<skill-name>` 显式启用），通常不应重复调用 request_skill_prompt。"
                "但若系统提示明确为「分段注入」且你确需后续段，可调用带 section/full 参数的 request_skill_prompt。"
                "如确需请求，也必须先给出上述步骤编排，再在结尾输出 "
                "{\"tool\":\"request_skill_prompt\",\"args\":{\"skill_id\":\"...\"}}。\n"
                "4) 对于需要两步及以上完成的任务，禁止首轮直接只给工具调用 JSON 而不做“事项简述 + 步骤编排”。\n"
                "5) 若用户问题可被上一条 system 开头的【经验记忆】单独完整回答，"
                "首轮应直接给出简短自然语言并以 {\"tool\":\"done\",\"args\":{}} 结束，不要输出 Step 编排或 memory_search。\n"
                "6) 若任务需要把自然语言指称解析为稳定标识符/映射：先阅读【经验记忆】，仍不足则首轮或次轮使用 memory_search，再执行检索、shell 或 request_skill_prompt；禁止在未核对记忆时先猜标识符再搜网。\n"
                "7) 若你已输出 Step 1..N 且含「检索/搜索」与后续「分析、再跑脚本、再请求其它 skill」等，禁止在仅完成靠前步骤且仍有 pending 时 {\"tool\":\"done\"}；须继续直至各步完成或显式说明改计划原因。\n\n"
                "8) 只要调用了网络检索相关的工具、命令、脚本或 skill（网页搜索、联网抓取、在线查询等），调用 done 前必须先输出一次检索结果总结（关键信息、来源要点、与用户问题的对应关系）；禁止检索后直接 done。\n\n"
                "9) 若上一任务已被用户取消，而本轮用户输入是新任务，禁止主动恢复或重做被取消任务；仅当用户明确要求“继续/重做”时才可恢复。\n\n"
                "【MCP 工具选择补充约束】\n"
                "- 用户若请求“指定 MCP server 的信息/详情”，首个查询工具必须是 mcp_server_info。\n"
                "- mcp_status/mcp_status_refresh 仅用于全局 MCP 状态总览，不可替代指定 server 的详情查询。\n\n"
                "【知识库 knowledge_search 约束】\n"
                "- 禁止：用户未明确要求检索知识库或参考知识库（本地文档库）信息时，不得调用 knowledge_search。\n"
                "- 必须：用户明确要求「检索知识库」「在知识库里查」「参考知识库中的资料/内容」或清晰等价表述时，"
                "必须先调用 knowledge_search 取得相关片段，再作答或继续其他工具；禁止未检索却声称已依据知识库。\n"
                "- 判定依据为用户原话语义，不使用固定关键词表做机械匹配。\n\n"
                "【经验记忆 memory_* 与 knowledge_search 区分】\n"
                "- knowledge_search：用户明确要求检索/参考「知识库、本地文档库」中的资料时使用。\n"
                "- memory_search：若 system 开头【经验记忆】已含作答所需信息，不要为走流程而调用；"
                "但若任务依赖「仅可能存在于记忆中的实体标识/别名映射」而当前看不出可靠值，必须先 memory_search 再调用下游取数或脚本，禁止臆造标识符。\n"
                "- memory_add：用户明确要求「记住某事」「以后按某偏好」且属于个人经验而非文档时；"
                "若你认为用户观点明显有误，仍可按工具说明在内容中记录你的判断（system_note）。\n\n"
                "首轮输出模板示例：\n"
                "我将帮你获取并显示 playwright MCP 最新状态。\n"
                "Step 1 [in_progress]: <当前要执行的步骤>\n"
                "Step 2 [pending]: <后续步骤>\n\n"
                "```json\n"
                "{\"tool\":\"<tool_name>\",\"args\":{...}}\n"
                "```"
            )
            first_round_evidence = ""
            if self._project_context_feature_enabled():
                project_context_ready = False
                project_context_files_total = 0
                project_context_inflight = False
                project_context_skip_reason = "unknown"
                try:
                    project_context_inflight = bool(
                        getattr(self, "_project_context_refresh_inflight", False)
                    )
                    if project_context_inflight:
                        project_context_ready = False
                        project_context_skip_reason = "refresh_inflight"
                    else:
                        idx = getattr(self, "_project_context_index", None)
                        files_map = getattr(idx, "files", None) if idx is not None else None
                        project_context_files_total = (
                            len(files_map) if isinstance(files_map, dict) else 0
                        )
                        project_context_ready = project_context_files_total > 0
                        project_context_skip_reason = (
                            "ready" if project_context_ready else "index_empty"
                        )
                except Exception as e:
                    project_context_ready = False
                    project_context_skip_reason = f"status_check_failed:{type(e).__name__}"

                if project_context_ready:
                    project_context_started_at = time.perf_counter()
                    _emit_flow_log(
                        "首轮项目上下文检索准备开始: "
                        f"files_total={project_context_files_total}"
                    )
                    ev_args = {
                        "query": original_user_task,
                        "max_files": 8,
                        "refresh": False,
                        "refresh_async": True,
                    }
                    ev_res = self.execute_tool_call("project_context_search", ev_args)
                    self.operation_results.append(
                        {
                            "command": {"action": "project_context_search", "params": ev_args},
                            "result": ev_res,
                            "timestamp": datetime.now().isoformat(),
                        }
                    )
                    first_round_evidence = self._render_evidence_block_from_project_context_result(ev_res)
                    project_context_elapsed_ms = int((time.perf_counter() - project_context_started_at) * 1000)
                    _emit_flow_log(
                        "首轮项目上下文检索准备结束: "
                        f"success={bool(ev_res.get('success', False))}, "
                        f"matches={int(ev_res.get('total_matches', 0) or 0)}, "
                        f"elapsed_ms={project_context_elapsed_ms}"
                    )
                else:
                    try:
                        self._schedule_project_context_refresh_background(
                            force=False,
                            reason="first-round-evidence-not-ready",
                        )
                    except Exception:
                        pass
                    _emit_flow_log(
                        "首轮项目上下文检索准备结束: "
                        f"skipped(not_ready:{project_context_skip_reason}), "
                        f"files_total={project_context_files_total}, "
                        f"refresh_inflight={project_context_inflight}"
                    )
            else:
                _emit_flow_log("首轮项目上下文检索准备结束: skipped(feature_disabled)")
            next_input = (
                f"{forced_mcp_prefix}{forced_skill_prefix}{original_user_task}"
                f"{first_round_contract}"
                f"{(chr(10) + chr(10) + first_round_evidence) if first_round_evidence else ''}"
            )
            ready_to_send_elapsed_ms = int((time.perf_counter() - turn_send_started_at) * 1000)
            _emit_flow_log(f"首轮请求准备完成，开始真正发送: elapsed_ms={ready_to_send_elapsed_ms}")
            is_first_round = True
            last_announced_skill_key: Optional[str] = None
            max_tool_rounds = int(getattr(self, "max_tool_rounds", 20) or 20)
            max_no_tool_rounds = 3
            no_tool_rounds = 0
            tool_round = 0
            user_message_recorded = False
            while tool_round < max_tool_rounds:
                if self._consume_task_interrupt_requested():
                    raise KeyboardInterrupt
                tool_round += 1
                status_ticker = pre_task_status_ticker
                pre_task_status_ticker = None
                if status_ticker is None:
                    status_ticker = _WorkingStatusTicker(
                        sys.stdout,
                        fps=_WORKING_STATUS_MARQUEE_FPS,
                    )
                    status_ticker.start()
                try:
                    ai_response = self.call_ai(
                        next_input,
                        context=json.dumps(last_result, ensure_ascii=False) if last_result else "",
                        stream=False,
                        return_message=False,
                        history_user_input=original_user_task if not user_message_recorded else None,
                        history_skip_user=user_message_recorded,
                    )
                finally:
                    status_ticker.stop()
                    self._clear_last_thinking_line()
                if self._consume_task_interrupt_requested():
                    raise KeyboardInterrupt
                if not isinstance(ai_response, str):
                    print(f"❌ AI returned invalid response: {ai_response}")
                    break
                if not user_message_recorded:
                    user_message_recorded = True
                if ai_response:
                    try:
                        self._hide_previous_shell_output_if_needed()
                    except Exception:
                        pass
                    display_response = format_assistant_display_response(ai_response)
                    if display_response:
                        sys.stdout.write(f"{_ansi_gray('•')} {display_response}")
                        if not display_response.endswith("\n"):
                            sys.stdout.write("\n")
                        sys.stdout.flush()

                fallback_plan = self._parse_tool_plan_from_response(ai_response)
                if fallback_plan:
                    tool_name, args = fallback_plan
                    if tool_name != "done":
                        self._print_tool_call_feedback(tool_name, args, failed=False)
                else:
                    tool_name, args = "", {}

                if ai_response and not fallback_plan:
                    # Keep output spacing consistent when only narrative is shown.
                    if not ai_response.endswith("\n"):
                        sys.stdout.write("\n")
                    sys.stdout.flush()
                if not fallback_plan:
                    misplaced_plan = self._find_tool_plan_anywhere(ai_response)
                    no_tool_rounds += 1
                    if no_tool_rounds >= max_no_tool_rounds:
                        print("❌ The model repeatedly failed to produce an executable JSON tool plan. Auto-execution has stopped for this round.")
                        break
                    print(
                        f"⚠️ No executable JSON tool plan detected (retry {no_tool_rounds}/{max_no_tool_rounds}): "
                        "the model will be asked again to output {\"tool\":\"...\",\"args\":{...}}."
                    )
                    if misplaced_plan:
                        m_tool, m_args = misplaced_plan
                        next_input = (
                            f"【用户原始需求】\n{original_user_task}\n\n"
                            "你上一条回复包含工具调用 JSON，但它不在回复结尾（后面仍有文本），因此被判无效。\n"
                            "请在下一条回复中把该工具调用原样放在最后一行，且其后不要再有任何文本。\n"
                            f"请输出：{{\"tool\":\"{m_tool}\",\"args\":{json.dumps(m_args, ensure_ascii=False)} }}"
                        )
                    else:
                        next_input = (
                            f"【用户原始需求】\n{original_user_task}\n\n"
                            "你上一条回复没有给出可执行 JSON。\n"
                            "请只输出一个 JSON 对象：{\"tool\":\"工具名\",\"args\":{...}}；"
                            "任务完成时输出 {\"tool\":\"done\",\"args\":{}}。"
                            "若你判断任务已完成，下一条必须直接输出 done，禁止再调用无关工具。"
                        )
                    is_first_round = False
                    continue

                if not tool_name:
                    print("❌ Tool plan is missing tool name. Ending this round.")
                    break

                if tool_name == "apply_patch":
                    patch_path = str(args.get("path") or "").strip() if isinstance(args, dict) else ""
                    patch_text = args.get("patch") if isinstance(args, dict) else None
                    if (not patch_path) or (not isinstance(patch_text, str)) or (not patch_text.strip()):
                        print(
                            "⚠️ apply_patch plan is missing required `path`/`patch`; "
                            "requesting the model to resend a valid unified patch call."
                        )
                        next_input = (
                            f"【用户原始需求】\n{original_user_task}\n\n"
                            "你上一条 apply_patch 工具计划缺少必填参数。\n"
                            "请只输出一个有效 JSON（不要附加其它文本）：\n"
                            "{\"tool\":\"apply_patch\",\"args\":{\"path\":\"<file>\",\"patch\":\"@@ ... @@\\n- old\\n+ new\"}}\n"
                            "要求：`path` 和 `patch` 都必须提供；`patch` 必须是包含至少一个 `@@ ... @@` hunk 的 unified patch 字符串。"
                        )
                        no_tool_rounds = 0
                        is_first_round = False
                        continue

                if (
                    tool_name == "done"
                    and _is_software_development_domain(
                        list(getattr(self, "_active_runtime_task_domains", None) or [])
                    )
                    and code_changed_in_task
                    and (not verification_done_in_task)
                ):
                    suggested_verify = _build_minimal_verification_command(
                        sorted(changed_files_in_task)
                    )
                    print(
                        "⚠️ Code changes were detected in this task but verification is incomplete; 'done' was blocked and minimal verification is required first."
                    )
                    next_input = (
                        f"【用户原始需求】\n{original_user_task}\n\n"
                        "你已经修改了代码，但还没有验证结果。\n"
                        "在 software_development 领域，修改后禁止直接 done。\n"
                        "请先输出一条验证工具调用 JSON；优先执行以下最小验证命令：\n"
                        f"`{suggested_verify}`\n\n"
                        "请输出：{\"tool\":\"shell\",\"args\":{\"command\":\"<验证命令>\"}}"
                    )
                    no_tool_rounds = 0
                    continue

                if tool_name == "request_skill_prompt":
                    sid = str(args.get("skill_id") or "").strip()
                    canon_sid = self._canonical_skill_id(sid)
                    active_sid = self._canonical_skill_id(self._active_skill_id or "")
                    requested_section_raw = args.get("section")
                    requested_section: Optional[int] = None
                    try:
                        if requested_section_raw is not None:
                            requested_section = int(requested_section_raw)
                    except Exception:
                        requested_section = None
                    force_full = bool(args.get("full", False))
                    request_is_expansion = force_full or (requested_section is not None and requested_section > 1)
                    if canon_sid and canon_sid in preloaded_skill_ids and not request_is_expansion:
                        next_input = (
                            f"【用户原始需求】\n{original_user_task}\n\n"
                            f"skill_id=`{sid}` 已由本轮显式 `/skills/<skill-name>` 引用预注入。"
                            "禁止再次调用 request_skill_prompt。请直接输出下一条业务工具调用 JSON。"
                        )
                        no_tool_rounds = 0
                        continue
                    if (
                        active_sid
                        and canon_sid
                        and active_sid == canon_sid
                        and str(self._active_skill_full_prompt or "").strip()
                        and not self._active_skill_chunked
                        and not request_is_expansion
                    ):
                        next_input = (
                            f"【用户原始需求】\n{original_user_task}\n\n"
                            f"skill_id=`{sid}` 的完整提示已在当前会话中注入。"
                            "禁止重复调用 request_skill_prompt。请直接输出下一条业务工具调用 JSON。"
                        )
                        no_tool_rounds = 0
                        continue
                    if (
                        active_sid
                        and canon_sid
                        and active_sid == canon_sid
                        and self._active_skill_chunked
                        and requested_section is None
                        and not force_full
                        and self._active_skill_section > 0
                        and self._active_skill_section < self._active_skill_total_sections
                    ):
                        requested_section = self._active_skill_section + 1
                    full_prompt, meta = self._build_single_skill_prompt(
                        sid,
                        requested_section=requested_section,
                        full=force_full,
                    )
                    if not full_prompt:
                        no_tool_rounds += 1
                        next_input = (
                            f"【用户原始需求】\n{original_user_task}\n\n"
                            f"你请求的 skill_id=`{sid}` 不存在。"
                            "请基于已加载技能索引重试，输出有效的 request_skill_prompt，或直接继续输出业务工具调用 JSON。"
                        )
                        continue
                    if active_sid != canon_sid:
                        print(f"🧩 About to enable skill: {sid}")
                    self._active_skill_full_prompt = full_prompt
                    self._active_skill_id = canon_sid or sid
                    self._active_skill_source = "local" if self._is_local_skill_id(canon_sid or sid) else "mcp"
                    self._active_skill_section = int(meta.get("section") or 0)
                    self._active_skill_total_sections = int(meta.get("total") or 0)
                    self._active_skill_chunked = bool(meta.get("chunked", False))
                    next_input = (
                        f"【用户原始需求】\n{original_user_task}\n\n"
                        f"已注入 skill_id=`{sid}` 的 skill 提示。"
                        f"当前段进度：{self._active_skill_section}/{self._active_skill_total_sections if self._active_skill_total_sections else 1}。"
                        "请继续输出下一条工具调用 JSON。"
                    )
                    no_tool_rounds = 0
                    continue

                pseudo_command = {"tool": tool_name, "args": args}
                if self._is_repeated_tool_call_pattern(tool_name, args):
                    next_input = (
                        f"【用户原始需求】\n{original_user_task}\n\n"
                        "检测到你在重复调用相同的 shell（参数几乎相同）。\n"
                        "请停止重复检索，改为：\n"
                        "1) 基于现有结果先给出阶段性结论；\n"
                        "2) 若证据不足，仅补充一次更有针对性的工具调用；\n"
                        "3) 若已足够，直接输出 done。\n"
                        "下一条请输出一个新的 JSON 工具计划。"
                    )
                    no_tool_rounds = 0
                    continue
                selected_skill = self._infer_selected_skill(pseudo_command, ai_response)
                if selected_skill:
                    skill_key = f"{selected_skill.get('skill_id')}::{selected_skill.get('name')}"
                    if skill_key != last_announced_skill_key:
                        print(f"🧩 Use skill: {selected_skill.get('name')} ({selected_skill.get('skill_id')})")
                        last_announced_skill_key = skill_key

                if self._consume_task_interrupt_requested():
                    raise KeyboardInterrupt
                result = self.execute_tool_call(tool_name, args)
                self._repaint_tool_call_feedback_if_failed(
                    tool_name,
                    args,
                    failed=not bool(result.get("success", True)),
                )
                no_tool_rounds = 0
                self.operation_results.append({
                    "command": pseudo_command,
                    "result": result,
                    "timestamp": datetime.now().isoformat()
                })
                try:
                    self._refresh_chat_history_after_tool_output()
                except Exception:
                    pass
                last_result = result
                is_first_round = False
                if tool_name == "apply_patch" and (not bool(result.get("success", False))):
                    err = str(result.get("error") or result.get("message") or "unknown error").strip()
                    print(f"❌ apply_patch failed: {err}")
                hints = _tool_change_and_verification_hints(tool_name, args, result)
                if bool(hints.get("code_changed", False)):
                    code_changed_in_task = True
                    for fp in list(hints.get("changed_files") or []):
                        fpp = str(fp or "").strip()
                        if fpp:
                            changed_files_in_task.add(fpp)
                if bool(hints.get("verified", False)):
                    verification_done_in_task = True
                summary = str(hints.get("verification_summary") or "").strip()
                if summary:
                    verification_evidence_in_task.append(summary)

                if self._result_indicates_user_cancelled(result):
                    self._force_current_input_as_requirement_once = True
                    self._last_cancelled_task = str(original_user_task or "").strip()
                    if current_task_id:
                        self._close_chat_task(current_task_id, "cancelled")
                    print("⏹️ User cancellation detected. The current task has been terminated.")
                    break

                if result.get("finished"):
                    if (
                        _is_software_development_domain(
                            list(getattr(self, "_active_runtime_task_domains", None) or [])
                        )
                        and code_changed_in_task
                    ):
                        ai_lower = str(ai_response or "").lower()
                        has_verify_summary = ("验证" in str(ai_response or "")) or ("verification" in ai_lower)
                        if not has_verify_summary:
                            evidence_text = (
                                "\n".join(f"- {x}" for x in verification_evidence_in_task[-3:])
                                if verification_evidence_in_task
                                else "- （暂无可提取验证证据）"
                            )
                            next_input = (
                                f"【用户原始需求】\n{original_user_task}\n\n"
                                "在 software_development 领域，完成前必须先给验证审核总结，再 done。\n"
                                "请先给出：修改点、验证命令、验证结果、剩余风险；"
                                "然后在结尾输出 done JSON。\n"
                                f"可用验证证据：\n{evidence_text}\n\n"
                                "请输出自然语言总结，并在最后一行输出："
                                "{\"tool\":\"done\",\"args\":{}}"
                            )
                            no_tool_rounds = 0
                            continue
                    if current_task_id:
                        self._close_chat_task(current_task_id, "done")
                    break
                if bool(result.get("task_changed", False)):
                    new_task = str(result.get("new_task") or "").strip()
                    if not new_task:
                        print("❌ task_changed returned without new_task. Auto-execution has stopped for this round.")
                        break
                    old_task = original_user_task
                    original_user_task = new_task
                    if current_task_id:
                        self._close_chat_task(current_task_id, "switched")
                    domain_info = self._classify_task_domains(original_user_task)
                    current_task_id = self._start_chat_task(
                        root_user_input=original_user_task,
                        domains=list(domain_info.get("domains") or []),
                        classifier=domain_info,
                        switched_from_task_id=str(current_task_id or ""),
                    )
                    code_changed_in_task = False
                    verification_done_in_task = False
                    changed_files_in_task = set()
                    verification_evidence_in_task = []
                    print("🔄 AI judged the user supplement unrelated to the original requirement; switched to a new task.")
                    print(f"   Old task: {old_task}")
                    print(f"   New task: {original_user_task}")
                    reason = str(result.get("reason") or "").strip()
                    next_input = (
                        f"【用户原始需求】\n{original_user_task}\n\n"
                        "你刚调用了 task_changed，系统已将原始需求切换为“新任务”。\n"
                        + (f"切换原因：{reason}\n" if reason else "")
                        + "请基于新的原始需求继续输出下一条 JSON 工具计划。"
                    )
                    continue
                if bool(result.get("needs_user_input", False)) and str(result.get("input_type", "")).strip() == "supplement":
                    q = str(result.get("question") or "").strip() or "Please provide supplementary information:"
                    print("🙋 Supplementary information is required before continuing.")
                    print(f"❓ {q}")
                    supplement_text = ""
                    handoff_to_main_loop = False
                    while True:
                        try:
                            supplement_text = self._get_user_input_with_history().strip()
                        except KeyboardInterrupt:
                            print("\n⏸️ Supplementary input cancelled. This task round is paused.")
                            supplement_text = ""
                            break
                        if not supplement_text:
                            print("⚠️ No supplementary information received. This task round is paused.")
                            break
                        if supplement_text.startswith("/") or supplement_text.startswith("!"):
                            # Route prefixed input back to the main loop so it shares
                            # the exact same parsing/execution path as a normal turn.
                            self._queued_user_input = supplement_text
                            handoff_to_main_loop = True
                            break
                        break
                    if handoff_to_main_loop:
                        break
                    if not supplement_text:
                        break
                    next_input = (
                        f"【用户原始需求】\n{original_user_task}\n\n"
                        f"【用户补充信息】\n{supplement_text}\n\n"
                        "请判断该补充信息是否与原始需求相关：\n"
                        "- 若完全无关：调用 {\"tool\":\"task_changed\",\"args\":{\"new_task\":\"<用户补充信息提炼后的新需求>\",\"reason\":\"...\"}}；\n"
                        "- 若相关：继续输出下一条工具调用 JSON；若信息仍不充分，可再次调用 ask_more_info。"
                    )
                    continue
                if (
                    (not result.get("success", True))
                    and bool(result.get("needs_user_input", False))
                    and (result.get("retryable", True) is False)
                ):
                    hint = str(result.get("error", "") or "需要用户输入后再继续。")
                    print(f"⏸️ Auto-continue paused: {hint}")
                    break

                step_progress = self._build_step_progress_context()
                post_status_rule = ""
                if tool_name in ("mcp_status", "mcp_status_refresh"):
                    post_status_rule = (
                        "你刚执行了 MCP 状态查询工具。下一步必须先根据上一条工具返回里的 status 字段，"
                        "按固定模板输出完整状态报告；该轮禁止直接 done。状态报告输出完成后的下一步再输出 done。"
                    )
                elif tool_name == "mcp_server_info":
                    post_status_rule = (
                        "你刚执行了 mcp_server_info。下一步必须先根据上一条工具返回里的 info/status 字段，"
                        "按固定模板输出该 server 的详情报告；该轮禁止直接 done。"
                        "详情报告输出完成后，请基于【用户原始需求】自行判断："
                        "若原始需求仅为查询/展示该指定 MCP 信息，则下一步必须直接输出 done；"
                        "若原始需求还包含其他未完成目标，则继续输出与原始需求相关的下一条工具调用。"
                        "查询/展示类需求默认只需自然语言回复，禁止创建额外文件或执行 shell 落盘；"
                        "仅当用户明确要求“导出/保存/写入文件”时，才允许创建文件。"
                        "禁止为凑步骤而调用 mcp_status/mcp_status_refresh 或 shell 等无关工具。"
                    )
                post_result_synthesis_rule = self._build_post_result_synthesis_rule(
                    tool_name=tool_name,
                    args=args,
                    result=result,
                )
                next_input = (
                    f"【用户原始需求】\n{original_user_task}\n\n"
                    f"{step_progress}\n\n"
                    f"【上一条工具执行结果（压缩）】\n{self._compact_result_for_next_input(result)}\n\n"
                    "请继续输出下一条 JSON 工具计划：{\"tool\":\"工具名\",\"args\":{...}}；"
                    "任务全部完成时输出 {\"tool\":\"done\",\"args\":{}}。"
                    "若上一条结果已满足原始需求，下一条必须直接输出 done。"
                    + (f"\n{post_status_rule}" if post_status_rule else "")
                    + (f"\n{post_result_synthesis_rule}" if post_result_synthesis_rule else "")
                )
            if tool_round >= max_tool_rounds:
                print(
                    "⏹️ Reached the auto-execution limit for this round (20 steps). Task is paused. "
                    "Ask again to continue, or narrow the task scope and retry."
                )
            in_task_execution = False
            self._in_task_execution = False
            if pre_task_status_ticker is not None:
                pre_task_status_ticker.stop()
                pre_task_status_ticker = None
                self._clear_last_thinking_line()
            self._stop_interrupt_monitor(cancel_task_on_interrupt=True)
            self._schedule_auto_memory_reflect()

        except KeyboardInterrupt:
            if pre_task_status_ticker is not None:
                pre_task_status_ticker.stop()
                pre_task_status_ticker = None
                self._clear_last_thinking_line()
            if in_task_execution:
                in_task_execution = False
                self._in_task_execution = False
                self._stop_interrupt_monitor(cancel_task_on_interrupt=True)
                self._consume_task_interrupt_requested()
                self._force_current_input_as_requirement_once = True
                if current_task_id:
                    self._close_chat_task(current_task_id, "cancelled")
                try:
                    self._last_cancelled_task = str(original_user_task or "").strip()
                except Exception:
                    self._last_cancelled_task = str(getattr(self, "_last_cancelled_task", "") or "")
                try:
                    self._record_conversation_interrupted_history(
                        interrupted_kind="task",
                        reason="user_interrupt",
                        detail=str(self._last_cancelled_task or ""),
                    )
                except Exception:
                    pass
                self._active_skill_full_prompt = ""
                self._active_skill_id = None
                self._active_skill_source = None
                self._active_skill_section = 0
                self._active_skill_total_sections = 0
                self._active_skill_chunked = False
                self._last_auto_removed_ephemeral = None
                if not self._consume_conversation_interrupted_banner_recent():
                    self._print_conversation_interrupted_banner()
                continue

            self._in_task_execution = False
            self._stop_interrupt_monitor(cancel_task_on_interrupt=True)
            print("")
            try:
                should_exit = input("Exit Smart Shell? (y/n): ").strip().lower() == "y"
            except KeyboardInterrupt:
                should_exit = False

            if should_exit:
                self._save_current_workspace_position()
                print("👋 Smart Shell exited. Goodbye!")
                break
            continue
        except Exception as e:
            if pre_task_status_ticker is not None:
                pre_task_status_ticker.stop()
                pre_task_status_ticker = None
                self._clear_last_thinking_line()
            self._in_task_execution = False
            self._stop_interrupt_monitor(cancel_task_on_interrupt=True)
            print(f"❌ Error occurred: {str(e)}")

