"""Runtime main loop extracted from SmartShellAgent.run."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ..config.app_info import get_app_display_version, get_app_name
from ..config.startup_tips import (
    format_tip_with_highlights,
    get_random_startup_tip_entry,
)
from ..core.assistant_output_highlighter import format_assistant_display_response
from ..core.logging.app_logging import get_log_file_path
from ..controllers.builtin_command_router import dispatch_builtin_command
from ..core.console_utils import _ansi_bold, _ansi_gray, _ansi_white, _ansi_cyan, _ansi_bright_blue


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
    # Header line: Smart Shell + version white, prefix gray.
    print(
        _ansi_gray("│ ")
        + _ansi_gray(">_ ")
        + _ansi_white(app_name)
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
        + _ansi_white(model_name)
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
        + _ansi_white(workspace_name)
        + _ansi_gray(" " * max(0, width - 1 - len(line3)))
        + _ansi_gray("│")
    )
    # directory line
    prefix_directory = "directory: "
    print(
        _ansi_gray("│ ")
        + _ansi_gray(prefix_directory)
        + _ansi_white(workspace_dir)
        + _ansi_gray(" " * max(0, width - 1 - len(line4)))
        + _ansi_gray("│")
    )
    print(_ansi_gray(bottom))
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
    print(_ansi_white("  ") + _ansi_bold("Tip: ") + rendered_tip)
    print("")


def run_agent_loop(agent: Any):
    """运行 AI Agent 主循环，使用 OpenAI tools 进行多轮自动执行，调用 done 结束。"""
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
                "知识库依赖在当前 Python 版本下不可用；主程序可继续运行。建议使用 Python 3.12 或 3.13 并安装知识库依赖。"
            )
        else:
            print("知识库依赖未就绪；主程序可继续运行。需要时请安装 requirements 中的知识库相关包。")
    elif KNOWLEDGE_AVAILABLE and self.knowledge_manager is not None:
        svc = self.knowledge_manager
        if svc.is_ready() and not svc.is_available():
            lp = get_log_file_path()
            print(
                "知识库初始化失败；请查看日志"
                + (f" ({lp})" if lp else "")
                + "，并检查 sentence-transformers、网络（首次需下载模型）与配置目录 workspace/knowledge/。"
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
            self._clear_prompt_separator()
        
            # 保存到历史记录（非空输入）
            if user_input.strip():
                self.history_manager.add_entry(user_input)

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
            # part as the task text to avoid the model treating "/skill-id" itself as work.
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
                        "ℹ️ 内置命令需以 / 开头，"
                        "例如 /exit、/help、/clear screen、/knowledge status、/memory status；单独输入 / 无效。"
                        "不经过 AI 的本机命令与脚本请以 ! 开头，例如 !ls、!git status。"
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
                    print("用法: /clear <screen|history|context>")
                    continue
                if bl == 'clear history':
                    self.history_manager.clear_history()
                    if self.input_handler is not None and hasattr(
                        self.input_handler, "reset_command_history"
                    ):
                        self.input_handler.reset_command_history(
                            self.history_manager.get_all_history()
                        )
                    print("✅ 历史记录已清除")
                    continue
                if bl == "clear context":
                    self.conversation_history.clear()
                    self._sync_active_chat_messages()
                    self.operation_results.clear()
                    self._last_auto_removed_ephemeral = None
                    self._session_summary_llm = ""
                    self._session_summary_rolling = ""
                    self._last_llm_summary_pair_count = 0
                    print("✅ 已清空 AI 上下文（对话历史与近期操作结果缓存，不影响命令行输入历史）")
                    continue
                if bl == "knowledge":
                    print("用法: /knowledge <status|sync|stats|search <query>>")
                    continue
                if bl == "knowledge status":
                    self._print_knowledge_status_details()
                    continue

                if bl == "memory":
                    print("用法: /memory <enable|disable|status|stats|list|search <query>|remember <text>|delete <id>>")
                    continue
                if bl == "memory enable":
                    self.memory_enabled = True
                    ok = self._save_memory_enabled_to_config()
                    print(
                        "✅ 经验记忆功能已开启"
                        + ("；已写入 config.json" if ok else "（配置保存失败，仅本次进程生效）")
                    )
                    continue
                if bl == "memory disable":
                    self.memory_enabled = False
                    ok = self._save_memory_enabled_to_config()
                    print(
                        "✅ 经验记忆功能已关闭"
                        + ("；已写入 config.json" if ok else "（配置保存失败，仅本次进程生效）")
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
                        print("❌ 请提供检索内容")
                    continue
                if bl.startswith("memory remember "):
                    text = builtin_line[len("memory remember ") :].strip()
                    if not text:
                        print("❌ 请提供要记住的内容")
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
                        print("❌ 请提供记忆 id")
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
                        print("用法: /execution-policy <show|unlimited|moderate|confirmation>")
                    else:
                        self.execute_tool_call("execution_policy_set", {"policy": policy})
                    continue
                if bl == "execution-policy":
                    print("用法: /execution-policy <show|unlimited|moderate|confirmation>")
                    continue

                if bl.startswith("session-summary "):
                    sub = bl[len("session-summary ") :].strip().lower()
                    if sub in ("on", "enable", "true", "1"):
                        self.session_summary_llm_enabled = True
                        ok = self._save_session_summary_llm_to_config()
                        print(
                            f"✅ 已开启会话 LLM 摘要（周期性压缩，用于经验记忆检索 query）"
                            f"{'；已写入 config.json' if ok else '（配置保存失败，仅本次进程生效）'}"
                        )
                        continue
                    if sub in ("off", "disable", "false", "0"):
                        self.session_summary_llm_enabled = False
                        ok = self._save_session_summary_llm_to_config()
                        print(
                            f"✅ 已关闭会话 LLM 摘要（仍保留滚动摘录 [会话摘录]）"
                            f"{'；已写入 config.json' if ok else '（配置保存失败，仅本次进程生效）'}"
                        )
                        continue
                    if sub == "show":
                        on = bool(getattr(self, "session_summary_llm_enabled", True))
                        cfg_path = self.config_dir / "config.json"
                        print(
                            f"会话 LLM 摘要 session_summary_llm：{'开启' if on else '关闭'}\n"
                            f"  配置项：config.json 中的 \"session_summary_llm\"（布尔）\n"
                            f"  配置文件：{cfg_path}"
                        )
                        continue
                    print(
                        "用法: /session-summary <on|off|show>\n"
                        "  on/off   - 开关周期性 LLM 会话摘要（关闭后仍用廉价滚动摘录）\n"
                        "  show     - 查看当前开关与配置文件路径"
                    )
                    continue
                if bl == "session-summary":
                    print(
                        "用法: /session-summary <on|off|show>\n"
                        "  /session-summary on     - 开启 LLM 会话摘要\n"
                        "  /session-summary off    - 关闭（仅滚动摘录）\n"
                        "  /session-summary show   - 查看状态"
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
                        print("❌ 请提供搜索查询内容")
                    continue
                if bl == 'help':

                    self._print_main_help()

                    continue

                print(
                    "❌ 未识别的内置命令。请使用 /help 查看列表。"
                    "在本机直接执行 shell 或脚本请使用 ! 前缀，例如 !git status、!dir。"
                )
                continue

            # Direct local execution without AI: requires leading "!" on all platforms.
            run_direct_shell: Optional[str] = None
            if stripped_in.startswith("!"):
                run_direct_shell = stripped_in[1:].lstrip()
                if not run_direct_shell:
                    print(
                        "ℹ️ 不经过 AI 直接执行的系统命令或可执行文件需以 ! 开头，"
                        "例如 !ls、!dir、!ping 127.0.0.1、!git status；单独输入 ! 无效。"
                    )
                    continue

            if run_direct_shell is not None:
                ui = run_direct_shell
                if self._is_executable_file(ui):
                    self._execute_file_directly(ui)
                    continue

                user_input_cmd = ui
                if system_cmd_re.match(ui):
                    if user_input_cmd.lower().startswith('ls') and os_name == 'nt':
                        user_input_cmd = 'dir ' + user_input_cmd[2:].strip()
                    elif user_input_cmd.lower().startswith('list') and os_name == 'nt':
                        user_input_cmd = 'dir ' + user_input_cmd[4:].strip()
                    elif user_input_cmd.lower().startswith('dir') and os_name != 'nt':
                        user_input_cmd = 'ls ' + user_input_cmd[3:].strip()

                    try:
                        if user_input_cmd.lower().startswith('cd '):
                            path = user_input_cmd[3:].strip()
                            result = self.action_change_directory(path)
                            if not result["success"]:
                                print(f"❌ {result['error']}")
                        else:
                            try:
                                process = subprocess.Popen(
                                    user_input_cmd,
                                    shell=True,
                                    stdin=sys.stdin,
                                    stdout=sys.stdout,
                                    stderr=sys.stderr,
                                    cwd=str(self.work_directory)
                                )
                                process.wait()
                            except Exception as e:
                                print(f"❌ 命令执行异常: {e}")
                    except Exception as e:
                        print(f"❌ 系统命令执行异常: {e}")
                    continue

                # e.g. !git status — not in the small whitelist but still direct shell
                try:
                    process = subprocess.Popen(
                        ui,
                        shell=True,
                        stdin=sys.stdin,
                        stdout=sys.stdout,
                        stderr=sys.stderr,
                        cwd=str(self.work_directory)
                    )
                    process.wait()
                except Exception as e:
                    print(f"❌ 命令执行异常: {e}")
                continue

            # Natural-language turn: rewrite prompt line as chat-style user line.
            in_task_execution = True
            self._in_task_execution = True
            self._rewrite_previous_prompt_as_user(raw_user_input.strip())

            last_result = None
            self._last_auto_removed_ephemeral = None
            original_user_task = task_user_input
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
                    print(f"🧩 启用 MCP 引用: {srv}/{name} ({kind})")
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
                        print(f"🧩 启用 Skill: {sname}")
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
                "若该 skill 已注入（例如用户通过 `/skill-id` 显式启用），通常不应重复调用 request_skill_prompt。"
                "但若系统提示明确为「分段注入」且你确需后续段，可调用带 section/full 参数的 request_skill_prompt。"
                "如确需请求，也必须先给出上述步骤编排，再在结尾输出 "
                "{\"tool\":\"request_skill_prompt\",\"args\":{\"skill_id\":\"...\"}}。\n"
                "4) 对于需要两步及以上完成的任务，禁止首轮直接只给工具调用 JSON 而不做“事项简述 + 步骤编排”。\n"
                "5) 若用户问题可被上一条 system 开头的【经验记忆】单独完整回答，"
                "首轮应直接给出简短自然语言并以 {\"tool\":\"done\",\"args\":{}} 结束，不要输出 Step 编排或 memory_search。\n"
                "6) 若任务需要把自然语言指称解析为稳定标识符/映射：先阅读【经验记忆】，仍不足则首轮或次轮使用 memory_search，再执行检索、shell 或 request_skill_prompt；禁止在未核对记忆时先猜标识符再搜网。\n"
                "7) 若你已输出 Step 1..N 且含「检索/搜索」与后续「分析、再跑脚本、再请求其它 skill」等，禁止在仅完成靠前步骤且仍有 pending 时 {\"tool\":\"done\"}；须继续直至各步完成或显式说明改计划原因。\n\n"
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
                if first_round_evidence:
                    print("🧭 首轮 Evidence Block 已注入。")
            next_input = (
                f"{forced_mcp_prefix}{forced_skill_prefix}{original_user_task}"
                f"{first_round_contract}"
                f"{(chr(10) + chr(10) + first_round_evidence) if first_round_evidence else ''}"
            )
            is_first_round = True
            last_announced_skill_key: Optional[str] = None
            max_tool_rounds = int(getattr(self, "max_tool_rounds", 20) or 20)
            max_no_tool_rounds = 3
            no_tool_rounds = 0
            tool_round = 0
            user_message_recorded = False
            while tool_round < max_tool_rounds:
                tool_round += 1
                print("正在思考...")
                ai_response = self.call_ai(
                    next_input,
                    context=json.dumps(last_result, ensure_ascii=False) if last_result else "",
                    stream=False,
                    return_message=False,
                    history_user_input=original_user_task if not user_message_recorded else None,
                    history_skip_user=user_message_recorded,
                )
                if not isinstance(ai_response, str):
                    print(f"❌ AI返回异常: {ai_response}")
                    break
                self._clear_last_thinking_line()
                if not user_message_recorded:
                    user_message_recorded = True
                if ai_response:
                    display_response = format_assistant_display_response(ai_response)
                    if display_response:
                        sys.stdout.write(f"{_ansi_gray('助手:')} {display_response}")
                        if not display_response.endswith("\n"):
                            sys.stdout.write("\n")
                        sys.stdout.flush()

                fallback_plan = self._parse_tool_plan_from_response(ai_response)
                if fallback_plan:
                    tool_name, args = fallback_plan
                    if tool_name != "done":
                        print(f"{_ansi_gray('执行工具:')} {_ansi_bright_blue(self._tool_call_summary(tool_name, args))}")
                    if tool_name == "text_file":
                        content = ""
                        if isinstance(args, dict):
                            content = str(args.get("content") or "")
                        print("📄 text_file 内容:")
                        print(content)
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
                        print("❌ 模型连续未给出可执行 JSON 工具计划，已停止本轮自动执行。")
                        break
                    print(
                        f"⚠️ 未检测到可执行 JSON 工具计划（重试 {no_tool_rounds}/{max_no_tool_rounds}）："
                        "将继续要求模型输出 {\"tool\":\"...\",\"args\":{...}}。"
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
                    print("❌ 工具计划缺少名称，结束本轮。")
                    break

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
                            f"skill_id=`{sid}` 已由本轮显式 `/skill` 引用预注入。"
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
                        print(f"🧩 即将启用 Skill: {sid}")
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
                        "检测到你在重复调用相同的 read/grep（参数几乎相同）。\n"
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
                        print(f"🧩 本步使用 Skill: {selected_skill.get('name')} ({selected_skill.get('skill_id')})")
                        last_announced_skill_key = skill_key

                result = self.execute_tool_call(tool_name, args)
                no_tool_rounds = 0
                self.operation_results.append({
                    "command": pseudo_command,
                    "result": result,
                    "timestamp": datetime.now().isoformat()
                })
                last_result = result
                is_first_round = False

                if self._result_indicates_user_cancelled(result):
                    print("⏹️ 检测到用户取消，已结束当前任务，不再自动续步。")
                    break

                if result.get("finished"):
                    break
                if bool(result.get("task_changed", False)):
                    new_task = str(result.get("new_task") or "").strip()
                    if not new_task:
                        print("❌ task_changed 返回缺少 new_task，已停止本轮自动执行。")
                        break
                    old_task = original_user_task
                    original_user_task = new_task
                    print("🔄 AI判定用户补充信息与原需求无关，已切换为新任务。")
                    print(f"   旧任务: {old_task}")
                    print(f"   新任务: {original_user_task}")
                    reason = str(result.get("reason") or "").strip()
                    next_input = (
                        f"【用户原始需求】\n{original_user_task}\n\n"
                        "你刚调用了 task_changed，系统已将原始需求切换为“新任务”。\n"
                        + (f"切换原因：{reason}\n" if reason else "")
                        + "请基于新的原始需求继续输出下一条 JSON 工具计划。"
                    )
                    continue
                if bool(result.get("needs_user_input", False)) and str(result.get("input_type", "")).strip() == "supplement":
                    q = str(result.get("question") or "").strip() or "请提供补充信息："
                    print("🙋 需要你补充信息后才能继续。")
                    print(f"❓ {q}")
                    supplement_text = ""
                    handoff_to_main_loop = False
                    while True:
                        try:
                            supplement_text = self._get_user_input_with_history().strip()
                        except KeyboardInterrupt:
                            print("\n⏸️ 已取消补充信息输入，本轮任务暂停。")
                            supplement_text = ""
                            break
                        if not supplement_text:
                            print("⚠️ 未收到补充信息，本轮任务暂停。")
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
                    print(f"⏸️ 已暂停自动续步：{hint}")
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
                        "查询/展示类需求默认只需自然语言回复，禁止创建 text_file 或执行 shell 落盘；"
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
                    "⏹️ 已达到本轮自动执行上限（20 步），任务已暂停。"
                    "请继续提问以续跑，或缩小目标范围后重试。"
                )
            in_task_execution = False
            self._in_task_execution = False
            self._schedule_auto_memory_reflect()

        except KeyboardInterrupt:
            if in_task_execution:
                in_task_execution = False
                self._in_task_execution = False
                self._active_skill_full_prompt = ""
                self._active_skill_id = None
                self._active_skill_source = None
                self._active_skill_section = 0
                self._active_skill_total_sections = 0
                self._active_skill_chunked = False
                self._last_auto_removed_ephemeral = None
                print("\n⏹️ 已取消当前任务")
                continue

            self._in_task_execution = False
            print("")
            try:
                should_exit = input("是否结束 Smart Shell？(y/n): ").strip().lower() == "y"
            except KeyboardInterrupt:
                should_exit = False

            if should_exit:
                self._save_current_workspace_position()
                print("👋 已退出 Smart Shell，再见！")
                break
            continue
        except Exception as e:
            self._in_task_execution = False
            print(f"❌ 发生错误: {str(e)}")

