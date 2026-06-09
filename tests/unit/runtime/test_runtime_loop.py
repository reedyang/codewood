import os
import re
import tempfile
import unittest
import unicodedata
from pathlib import Path
from unittest.mock import patch

from src.runtime.runtime_loop import (
    _consume_streaming_ai_response,
    _format_active_plan_reminder,
    _format_worked_for_summary_line,
    _format_startup_directory,
    _model_tool_result_was_aborted,
    _render_aborted_direct_shell_feedback,
    _refresh_context_usage_after_task_boundary,
    _resolve_worked_summary_terminal_width,
    _sanitize_prompt_pollution,
    _should_prioritize_project_context_for_task,
    _summarize_active_plan,
    _sync_command_input_history,
    _should_record_command_input_history,
    _strip_leaked_internal_history_markers,
    _stream_visible_text_with_json_pause,
    _stop_pre_task_status_ticker_for_console_output,
    _try_record_user_task_message,
    _parse_tool_plans_from_model_message,
    _parse_tool_plan_from_model_message,
    _looks_like_pseudo_tool_call_text,
    _split_trailing_pseudo_tool_calls_text,
    _split_trailing_pseudo_tool_calls_text_details,
    _replace_latest_assistant_history_content,
    _build_pseudo_tool_call_retry_prompt,
    _PSEUDO_TOOL_CALL_RETRY_EXAMPLE_JSON,
)


def _display_width(text: str) -> int:
    total = 0
    for ch in str(text or ""):
        if not ch or unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


class RuntimeLoopTests(unittest.TestCase):
    class _FakeHistoryManager:
        def __init__(self, entries=None):
            self.history = list(entries or [])

        def add_entry(self, command: str):
            c = str(command or "").strip()
            if not c:
                return
            self.history = [x for x in self.history if x != c]
            self.history.append(c)

        def get_all_history(self):
            return list(self.history)

    class _FakeInputHandler:
        def __init__(self):
            self.calls = 0
            self.last_entries = None

        def reset_command_history(self, entries):
            self.calls += 1
            self.last_entries = list(entries or [])

    def test_format_startup_directory_replaces_user_home_with_tilde(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            fake_home = base / "home_user"
            inside_path = fake_home / "projects" / "demo"
            outside_path = base / "outside" / "demo"
            inside_path.mkdir(parents=True, exist_ok=True)
            outside_path.parent.mkdir(parents=True, exist_ok=True)

            with patch("src.runtime.runtime_loop.Path.home", return_value=fake_home):
                self.assertEqual(
                    _format_startup_directory(str(inside_path)),
                    f"~{os.sep}projects{os.sep}demo",
                )
                self.assertEqual(_format_startup_directory(str(fake_home)), "~")
                self.assertEqual(
                    _format_startup_directory(str(outside_path)),
                    str(outside_path),
                )

    def test_model_tool_result_was_aborted_detects_shell_interrupts(self):
        self.assertTrue(
            _model_tool_result_was_aborted(
                "shell",
                {"success": False, "aborted_by_user": True, "output": ""},
            )
        )
        self.assertTrue(
            _model_tool_result_was_aborted(
                "shell",
                {"success": False, "output": "line\ncommand aborted by user\n"},
            )
        )
        self.assertFalse(
            _model_tool_result_was_aborted(
                "shell",
                {"success": False, "output": "ordinary failure\n"},
            )
        )
        self.assertFalse(
            _model_tool_result_was_aborted(
                "read",
                {"success": False, "output": "command aborted by user\n"},
            )
        )

    def test_render_aborted_direct_shell_feedback_repaints_then_prints_banner(self):
        calls = []

        class _Agent:
            def _repaint_direct_shell_command_feedback_if_failed(
                self,
                command,
                rendered_output_lines,
                cursor_at_line_start,
                failed,
            ):
                calls.append(
                    (
                        "repaint",
                        command,
                        rendered_output_lines,
                        cursor_at_line_start,
                        failed,
                    )
                )

            def _print_conversation_interrupted_banner(self):
                calls.append(("banner",))
                return 3

        agent = _Agent()

        _render_aborted_direct_shell_feedback(
            agent,
            "ping www.baidu.com",
            {"rendered_output_lines": 4, "cursor_at_line_start": True},
        )

        self.assertTrue(bool(getattr(agent, "_suppress_next_prompt_chat_reload_once", False)))
        self.assertEqual(
            calls,
            [
                ("repaint", "ping www.baidu.com", 4, True, True),
                ("banner",),
            ],
        )

    def test_sanitize_prompt_pollution_strips_fixed_prompt(self):
        cleaned = _sanitize_prompt_pollution("› /help", Path("D:/ws"))
        self.assertEqual(cleaned, "/help")

    def test_sanitize_prompt_pollution_strips_multiple_fixed_prompts(self):
        cleaned = _sanitize_prompt_pollution("› › !git status", Path("D:/ws"))
        self.assertEqual(cleaned, "!git status")

    def test_should_record_command_input_history_keeps_chat_reload_in_memory_history(self):
        self.assertTrue(_should_record_command_input_history("/chat reload"))
        self.assertTrue(_should_record_command_input_history("/CHAT   reload"))

    def test_should_record_command_input_history_keeps_other_inputs(self):
        self.assertTrue(_should_record_command_input_history("/chat list"))
        self.assertTrue(_should_record_command_input_history("!git status"))
        self.assertFalse(_should_record_command_input_history("   "))

    def test_stop_pre_task_status_ticker_for_console_output_clears_line(self):
        class _Agent:
            def __init__(self):
                self.clear_calls = 0

            def _clear_last_thinking_line(self):
                self.clear_calls += 1

        class _Ticker:
            def __init__(self):
                self.stop_calls = 0

            def stop(self):
                self.stop_calls += 1

        agent = _Agent()
        ticker = _Ticker()

        result = _stop_pre_task_status_ticker_for_console_output(agent, ticker)

        self.assertIsNone(result)
        self.assertEqual(ticker.stop_calls, 1)
        self.assertEqual(agent.clear_calls, 1)

    def test_sync_command_input_history_records_slash_commands_for_current_session(self):
        class _Agent:
            pass

        agent = _Agent()
        agent.history_manager = self._FakeHistoryManager(
            ["/help", "/chat reload", "/model list"]
        )
        agent.input_handler = self._FakeInputHandler()

        _sync_command_input_history(agent, "/chat reload")

        self.assertEqual(
            agent.history_manager.get_all_history(),
            ["/help", "/model list", "/chat reload"],
        )
        self.assertEqual(agent.input_handler.calls, 1)
        self.assertEqual(
            agent.input_handler.last_entries,
            ["/help", "/model list", "/chat reload"],
        )

    def test_sync_command_input_history_adds_absent_slash_command(self):
        class _Agent:
            pass

        agent = _Agent()
        agent.history_manager = self._FakeHistoryManager(["/help", "/model list"])
        agent.input_handler = self._FakeInputHandler()

        _sync_command_input_history(agent, "/chat reload")

        self.assertEqual(
            agent.history_manager.get_all_history(),
            ["/help", "/model list", "/chat reload"],
        )
        self.assertEqual(agent.input_handler.calls, 1)
        self.assertEqual(
            agent.input_handler.last_entries,
            ["/help", "/model list", "/chat reload"],
        )

    def test_format_worked_for_summary_line_fills_terminal_width(self):
        line = _format_worked_for_summary_line(elapsed_seconds=65, terminal_width=40)
        self.assertEqual(_display_width(line), 40)
        self.assertTrue(line.startswith("─ Worked for 1m 5s "))
        self.assertTrue(line.endswith("─"))

    def test_format_worked_for_summary_line_uses_language_specific_label(self):
        line = _format_worked_for_summary_line(elapsed_seconds=65, terminal_width=40, language="zh-CN")
        self.assertEqual(_display_width(line), 40)
        self.assertTrue(line.startswith("─ 已运行 1m 5s "))
        self.assertTrue(line.endswith("─"))

    def test_format_worked_for_summary_line_hides_minutes_when_under_one_minute(self):
        line = _format_worked_for_summary_line(elapsed_seconds=59, terminal_width=40)
        self.assertEqual(_display_width(line), 40)
        self.assertTrue(line.startswith("─ Worked for 59s "))
        self.assertNotIn("m ", line)

    def test_resolve_worked_summary_terminal_width_prefers_line_estimate(self):
        class _Agent:
            def _terminal_columns_for_line_estimate(self):
                return 124

            def _terminal_columns_for_prompt_separator(self, default=80):
                return 123

        width = _resolve_worked_summary_terminal_width(_Agent(), default=80)
        self.assertEqual(width, 124)

    def test_resolve_worked_summary_terminal_width_falls_back_to_prompt_separator(self):
        class _Agent:
            def _terminal_columns_for_prompt_separator(self, default=80):
                return 123

        width = _resolve_worked_summary_terminal_width(_Agent(), default=80)
        self.assertEqual(width, 123)

    def test_try_record_user_task_message_records_once_when_not_recorded(self):
        class _Agent:
            def __init__(self):
                self.calls = []

            def _append_chat_message(self, role, content):
                self.calls.append((role, content))

        agent = _Agent()
        recorded = _try_record_user_task_message(agent, "  fix build  ", already_recorded=False)
        self.assertTrue(recorded)
        self.assertEqual(agent.calls, [("user", "fix build")])

    def test_try_record_user_task_message_skips_when_already_recorded(self):
        class _Agent:
            def __init__(self):
                self.calls = []

            def _append_chat_message(self, role, content):
                self.calls.append((role, content))

        agent = _Agent()
        recorded = _try_record_user_task_message(agent, "fix build", already_recorded=True)
        self.assertTrue(recorded)
        self.assertEqual(agent.calls, [])

    def test_refresh_context_usage_after_task_boundary_calls_sync_and_async(self):
        class _Svc:
            def __init__(self):
                self.calls = []

            def schedule_context_usage_refresh_async(self, **kwargs):
                self.calls.append(dict(kwargs))
                return True

        class _Agent:
            def __init__(self):
                self.active_chat_id = "chat-9"
                self.session_memory_service = _Svc()
                self.sync_calls = []

            def _refresh_status_context_usage_snapshot(self, **kwargs):
                self.sync_calls.append(dict(kwargs))

        agent = _Agent()
        _refresh_context_usage_after_task_boundary(
            agent,
            user_input_hint="new task",
            context_hint="task finished",
        )
        self.assertEqual(len(agent.sync_calls), 1)
        self.assertEqual(agent.sync_calls[0].get("context_hint"), "task finished")
        self.assertEqual(len(agent.session_memory_service.calls), 1)
        self.assertEqual(agent.session_memory_service.calls[0].get("expected_chat_id"), "chat-9")

    def test_refresh_context_usage_after_task_boundary_is_best_effort(self):
        class _Agent:
            def __init__(self):
                self.active_chat_id = "chat-1"
                self.session_memory_service = object()

        _refresh_context_usage_after_task_boundary(
            _Agent(),
            user_input_hint="u",
            context_hint="ask_more_info paused",
        )

    def test_project_context_priority_detection_targets_software_development_tasks(self):
        self.assertTrue(_should_prioritize_project_context_for_task("请帮我修复这个 bug"))
        self.assertTrue(_should_prioritize_project_context_for_task("Explain this function in src/runtime/runtime_loop.py"))
        self.assertTrue(_should_prioritize_project_context_for_task("帮我重构这个模块并补测试"))
        self.assertFalse(_should_prioritize_project_context_for_task("帮我写一封邮件"))
        self.assertFalse(_should_prioritize_project_context_for_task("今天上海天气怎么样"))

    def test_stream_visible_text_with_json_pause_caches_incomplete_tool_json_fence(self):
        raw = "Run a quick check first\n```json\n{\"tool\":\"shell\",\"args\":{"
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw.split("```json", 1)[0].rstrip())

    def test_stream_visible_text_with_json_pause_keeps_partial_json_fence_opener(self):
        raw = "Run a quick check first\n```"
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw)

    def test_stream_visible_text_caches_unclosed_json_fence_before_tool_key(self):
        raw = (
            "Plan\n"
            "Step 1 [in_progress]: Run command\n\n"
            "```json\n"
            "{\n"
        )
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw.split("```json", 1)[0].rstrip())

    def test_stream_visible_text_with_json_pause_hides_tool_json_fence_when_complete(self):
        raw = (
            "Preparing to run\n"
            "```json\n"
            "{\"tool\":\"shell\",\"args\":{\"command\":\"Get-Content a.py\"}}\n"
            "```\n"
            "Continue"
        )
        out = _stream_visible_text_with_json_pause(raw, final=True)
        self.assertEqual(out, raw.split("```json", 1)[0].rstrip())

    def test_stream_visible_text_with_json_pause_keeps_non_tool_json_fence(self):
        raw = (
            "Explanation\n"
            "```json\n"
            "{\"foo\":1}\n"
            "```\n"
            "Done"
        )
        out = _stream_visible_text_with_json_pause(raw, final=True)
        self.assertEqual(out, raw)

    def test_stream_visible_text_hides_tool_calls_json_fence_when_complete(self):
        raw = (
            "Plan\n"
            "Step 1 [in_progress]: Run command\n\n"
            "```json\n"
            "{\n"
            "  \"tool_calls\": [\n"
            "    {\"function\":{\"name\":\"shell\"}}\n"
            "  ]\n"
            "}\n"
            "```"
        )
        out = _stream_visible_text_with_json_pause(raw, final=True)
        self.assertEqual(out, raw.split("```json", 1)[0].rstrip())

    def test_stream_visible_text_caches_partial_tool_calls_key(self):
        raw = (
            "Plan\n"
            "{\n"
            "  \"tool_calls"
        )
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw.split("{", 1)[0].rstrip())

    def test_stream_visible_text_caches_bare_trailing_json_object_start(self):
        raw = (
            "Plan\n"
            "Step 1 [in_progress]: Load skill\n\n"
            "{"
        )
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw.split("{", 1)[0].rstrip())

    def test_stream_visible_text_caches_bare_trailing_json_array_start(self):
        raw = (
            "Plan\n"
            "Step 1 [in_progress]: Load skill\n\n"
            "[\n"
            "  {"
        )
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw.split("\n\n[", 1)[0].rstrip())

    def test_stream_visible_text_with_json_pause_caches_trailing_plain_tool_json(self):
        raw = (
            "Let me explain first.\n\n"
            "{\"tool\":\"done\",\"args\":{}}\n"
        )
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw.split('{"tool', 1)[0].rstrip())

    def test_stream_visible_text_with_json_pause_hides_trailing_plain_tool_json_when_final(self):
        raw = (
            "Let me explain first.\n\n"
            "{\"tool\":\"done\",\"args\":{}}\n"
        )
        out = _stream_visible_text_with_json_pause(raw, final=True)
        self.assertEqual(out, raw.split('{"tool', 1)[0].rstrip())

    def test_stream_visible_text_with_json_pause_hides_trailing_tool_array_when_final(self):
        raw = (
            "Done\n\n"
            "[\n"
            "  {\"tool\":\"done\",\"args\":{}}\n"
            "]"
        )
        out = _stream_visible_text_with_json_pause(raw, final=True)
        self.assertEqual(out, raw.split("[", 1)[0].rstrip())

    def test_stream_visible_text_hides_internal_model_tool_result_marker(self):
        raw = (
            "Done\n\n"
            "[MODEL_TOOL_RESULT]{\"kind\":\"model_tool_result\",\"tool\":\"done\",\"args\":{}}"
        )
        out = _stream_visible_text_with_json_pause(raw, final=True)
        self.assertEqual(out, "Done")

    def test_stream_visible_text_with_json_pause_caches_partial_plain_tool_json(self):
        raw = (
            "Let me explain first.\n\n"
            "{\"tool"
        )
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw.split('{"tool', 1)[0].rstrip())

    def test_stream_visible_text_with_json_pause_keeps_plain_non_tool_json(self):
        raw = (
            "Here is the data:\n\n"
            "{\"foo\":1}"
        )
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw)

    def test_stream_visible_text_with_json_pause_releases_partial_false_alarm(self):
        raw = "data:\n{\"toolbox\": true}"
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw)

    def test_stream_visible_text_caches_serialized_message_until_confirmed(self):
        raw = (
            "Planning...\n"
            "{\"content\":\"Step 1 [in_progress]: Load skill\","
        )
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw.split("{\"content\"", 1)[0].rstrip())

    def test_stream_visible_text_hides_serialized_message_with_tool_calls(self):
        raw = (
            "Planning...\n"
            "{\"content\":\"Step 1 [in_progress]: Load skill\","
            "\"tool_calls\":[{\"function\":{\"name\":\"request_skill_prompt\"}}]}"
        )
        out = _stream_visible_text_with_json_pause(raw, final=True)
        self.assertEqual(out, raw.split("{\"content\"", 1)[0].rstrip())

    def test_stream_visible_text_releases_content_json_without_tool_calls(self):
        raw = "data:\n{\"content\":\"ordinary data\"}"
        out = _stream_visible_text_with_json_pause(raw, final=True)
        self.assertEqual(out, raw)

    def test_stream_visible_text_hides_envelope_starting_with_role_key(self):
        """Models occasionally dump a fully-serialized message object
        (``{"role":"assistant","content":"...","tool_calls":[...]}``)
        after the visible prose. The earlier ``"tool_calls"`` /
        ``"content"`` patterns only fired when those keys were the
        *first* key after ``{``; when ``"role":"assistant"`` led the
        envelope, the JSON tail used to leak verbatim to the terminal
        and into the persisted chat history. This regression test
        guards the more permissive envelope matcher that scans the
        opening line for any toolish key."""
        raw = (
            "刚才的分析尝试由于数据格式问题失败了，我将调整命令。\n\n"
            "{\"role\":\"assistant\","
            "\"content\":\"刚才的分析尝试由于数据格式问题失败了，我将调整命令。\","
            "\"tool_calls\":[{\"id\":\"abc\",\"type\":\"function\","
            "\"function\":{\"name\":\"shell\",\"arguments\":\"{}\"}}]}"
        )
        out = _stream_visible_text_with_json_pause(raw, final=True)
        self.assertEqual(out, "刚才的分析尝试由于数据格式问题失败了，我将调整命令。")

    def test_stream_visible_text_withholds_envelope_during_streaming(self):
        """During streaming (``final=False``) the cutter must withhold
        a JSON envelope as soon as we can recognise it — i.e. once the
        body carries an envelope-marker key or grows past a benign
        prose-JSON length. This prevents the leak the user reported
        where a paragraph break followed by ``{"role":"assistant",...``
        printed character-by-character to the terminal until the late-
        arriving ``"tool_calls":`` key finally tripped a cut."""
        raw = (
            "刚才的分析尝试由于数据格式问题失败了。\n\n"
            "{\"role\":\"assistant\",\"content\":\"刚才的分析尝试"
        )
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, "刚才的分析尝试由于数据格式问题失败了。")

    def test_stream_visible_text_does_not_withhold_short_benign_prose_json(self):
        """The withholding logic must not catch ordinary short JSON
        blobs the model prints alongside prose. A self-contained
        ``{"foo":1}`` after a blank line is benign and should be
        released to the terminal during streaming."""
        raw = "Here is the data:\n\n{\"foo\":1}"
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw)

    def test_stream_visible_text_withholds_trailing_lone_brace(self):
        """When the streaming buffer ends right at a paragraph-leading
        ``\\n\\n{`` with no quote yet, the next chunk may turn the
        ``{`` into ``{"<prose>`` (envelope leak) or into something
        else. The append-only streamer cannot retract characters that
        already reached the terminal, so we must withhold the lone
        brace until the next chunk disambiguates it."""
        raw = "现在进行深度诊断分析。\n\n{"
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, "现在进行深度诊断分析。")

    def test_stream_visible_text_withholds_trailing_brace_quote_pair(self):
        """When the buffer ends exactly at ``\\n\\n{"`` (or with
        whitespace between), the cutter doesn't yet know whether the
        following character will be a valid JSON-key start. Be
        conservative and withhold from the ``{`` so the partial
        ``{"`` cannot leak to the terminal before the next chunk
        arrives."""
        raw = "现在进行深度诊断分析。\n\n{\""
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, "现在进行深度诊断分析。")

    def test_stream_visible_text_hides_malformed_envelope_with_cjk_first_char(self):
        """Real-world regression: some models echo their own visible
        prose back inside a malformed ``{"<prose>...`` JSON object —
        no surrounding key=value structure, just the prose
        immediately after the opening ``{"``. Because the first
        "key" character is non-ASCII, this can never be valid prose
        JSON, so both streaming and the final pass must cut at the
        opening ``{`` and keep only the natural-language prose."""
        raw = (
            "已经获取到比亚迪的实时行情数据，接下来我将调用分析工具为您生成详细的行情报告。\n\n"
            "**计划更新：**\n"
            "1. 获取比亚迪实时行情数据 `completed`\n"
            "2. 分析行情并生成报告 `in_progress`\n"
            "3. 总结结果并回答用户 `pending`\n\n"
            "{\"已经获取到比亚迪的实时行情数据，接下来我将调用分析工具为您生成详细的行情报告。\n\n"
            "**计划更新：**\n"
            "1. 获取比亚迪实时行情数据 `completed`\n"
            "2. 分析行情并生成报告 `in_progress`\n"
            "3. 总结结果并回答用户 `pending`\n\n"
            "tool_calls\":[...]}"
        )
        expected_visible = (
            "已经获取到比亚迪的实时行情数据，接下来我将调用分析工具为您生成详细的行情报告。\n\n"
            "**计划更新：**\n"
            "1. 获取比亚迪实时行情数据 `completed`\n"
            "2. 分析行情并生成报告 `in_progress`\n"
            "3. 总结结果并回答用户 `pending`"
        )
        self.assertEqual(_stream_visible_text_with_json_pause(raw, final=True), expected_visible)
        self.assertEqual(_stream_visible_text_with_json_pause(raw, final=False), expected_visible)

    def test_stream_visible_text_hides_pseudo_tool_calls_block(self):
        raw = (
            "Preparing to run\n\n"
            "<tool_calls>\n"
            "{\"tool\":\"shell\",\"name\":\"shell\",\"arguments\":{\"command\":\"ping www.baidu.com -n 4\"}}\n"
            "</tool_calls>"
        )
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw.split("<tool_calls", 1)[0].rstrip())

    def test_stream_visible_text_caches_unclosed_pseudo_tool_calls_block(self):
        raw = (
            "Preparing to run\n\n"
            "<tool_calls>\n"
            "{\"tool\":\"shell\""
        )
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw.split("<tool_calls", 1)[0].rstrip())

    def test_stream_visible_text_with_json_pause_hides_tool_json_array_fence_when_complete(self):
        raw = (
            "Preparing to run\n"
            "```json\n"
            "[\n"
            "  {\"tool\":\"shell\",\"args\":{\"command\":\"Get-Content a.py\"}},\n"
            "  {\"tool\":\"project_context_search\",\"args\":{\"query\":\"foo\"}}\n"
            "]\n"
            "```\n"
            "Continue"
        )
        out = _stream_visible_text_with_json_pause(raw, final=True)
        self.assertEqual(out, raw.split("```json", 1)[0].rstrip())

    def test_stream_visible_text_hides_plain_fenced_single_tool_object(self):
        raw = (
            "Done\n\n"
            "```\n"
            "{\n"
            "  \"tool\": \"done\",\n"
            "  \"args\": {}\n"
            "}\n"
            "```"
        )
        out = _stream_visible_text_with_json_pause(raw, final=True)
        self.assertEqual(out, raw.split("```", 1)[0].rstrip())

    def test_stream_visible_text_hides_plain_fenced_tool_calls_object(self):
        raw = (
            "Done\n\n"
            "```\n"
            "{\"tool_calls\":[{\"tool\":\"done\",\"args\":{}}]}\n"
            "```"
        )
        out = _stream_visible_text_with_json_pause(raw, final=True)
        self.assertEqual(out, raw.split("```", 1)[0].rstrip())

    def test_consume_streaming_ai_response_calls_callback_before_first_visible_output(self):
        class _FakeStream:
            def __init__(self, state):
                self.state = state

            def write(self, text):
                s = str(text or "")
                if s and (not self.state["stopped"]):
                    self.state["wrote_before_stop"] = True
                return len(s)

            def flush(self):
                return None

            def isatty(self):
                return False

        class _Agent:
            def _hide_previous_shell_output_if_needed(self):
                return None

            def _ensure_terminal_line_start(self):
                return None

        state = {"stopped": False, "wrote_before_stop": False}

        def _before_first_visible_output():
            state["stopped"] = True

        with patch("src.runtime.runtime_loop.sys.stdout", _FakeStream(state)):
            ai_response, streamed_any = _consume_streaming_ai_response(
                _Agent(),
                ["he", "llo"],
                before_first_visible_output=_before_first_visible_output,
            )

        self.assertEqual(ai_response, "hello")
        self.assertTrue(streamed_any)
        self.assertFalse(state["wrote_before_stop"])

    def test_consume_streaming_ai_response_no_visible_output_skips_callback(self):
        class _FakeStream:
            def write(self, text):
                return len(str(text or ""))

            def flush(self):
                return None

            def isatty(self):
                return False

        class _Agent:
            def _hide_previous_shell_output_if_needed(self):
                raise AssertionError("should not be called")

            def _ensure_terminal_line_start(self):
                raise AssertionError("should not be called")

        callback_calls = 0

        def _before_first_visible_output():
            nonlocal callback_calls
            callback_calls += 1

        with patch("src.runtime.runtime_loop.sys.stdout", _FakeStream()):
            ai_response, streamed_any = _consume_streaming_ai_response(
                _Agent(),
                [],
                before_first_visible_output=_before_first_visible_output,
            )

        self.assertEqual(ai_response, "")
        self.assertFalse(streamed_any)
        self.assertEqual(callback_calls, 0)

    def test_consume_streaming_ai_response_closes_stream_on_interrupt(self):
        class _FakeAiStream:
            def __init__(self):
                self.closed = False

            def __iter__(self):
                yield "hello"

            def close(self):
                self.closed = True

        class _FakeStdout:
            def write(self, text):
                return len(str(text or ""))

            def flush(self):
                return None

            def isatty(self):
                return False

        class _Agent:
            def _consume_task_interrupt_requested(self):
                return True

            def _hide_previous_shell_output_if_needed(self):
                return None

            def _ensure_terminal_line_start(self):
                return None

        stream = _FakeAiStream()
        with patch("src.runtime.runtime_loop.sys.stdout", _FakeStdout()):
            with self.assertRaises(KeyboardInterrupt):
                _consume_streaming_ai_response(_Agent(), stream)
        self.assertTrue(stream.closed)

    def test_consume_streaming_ai_response_tty_append_mode_avoids_block_clear_redraw(self):
        class _FakeTtyStream:
            def __init__(self):
                self.writes = []

            def write(self, text):
                s = str(text or "")
                self.writes.append(s)
                return len(s)

            def flush(self):
                return None

            def isatty(self):
                return True

        class _AppendStream:
            def __init__(self, base):
                self._base = base
                self._line_start = True
                self._visual_col = 0

            def write(self, text):
                return self._base.write(text)

            def flush(self):
                return self._base.flush()

        class _Agent:
            def _hide_previous_shell_output_if_needed(self):
                return None

            def _ensure_terminal_line_start(self):
                return None

            def _build_internal_slash_output_stream(self, base_stream, terminal_columns=None):
                _ = terminal_columns
                return _AppendStream(base_stream)

        fake_out = _FakeTtyStream()
        with patch("src.runtime.runtime_loop.sys.stdout", fake_out):
            ai_response, streamed_any = _consume_streaming_ai_response(
                _Agent(),
                ["he", "llo"],
            )

        merged = "".join(fake_out.writes)
        self.assertEqual(ai_response, "hello")
        self.assertTrue(streamed_any)
        self.assertNotIn("\x1b[1A\r\x1b[2K", merged)
        self.assertIn("hello", re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", merged))

    def test_consume_streaming_ai_response_does_not_duplicate_text_before_plain_tool_json(self):
        class _FakeStdout:
            def __init__(self):
                self.writes = []

            def write(self, text):
                self.writes.append(str(text or ""))
                return len(str(text or ""))

            def flush(self):
                return None

            def isatty(self):
                return False

        class _Agent:
            def _hide_previous_shell_output_if_needed(self):
                return None

            def _ensure_terminal_line_start(self):
                return None

        fake_out = _FakeStdout()
        chunks = [
            "Hello, I am Xiaoyu, and I'm happy to help!\n\n",
            "{\"tool",
            "\":\"done\",\"args\":{}}",
        ]
        with patch("src.runtime.runtime_loop.sys.stdout", fake_out):
            ai_response, streamed_any = _consume_streaming_ai_response(_Agent(), chunks)

        rendered = "".join(fake_out.writes)
        self.assertIn("\"tool\":\"done\"", ai_response)
        self.assertTrue(streamed_any)
        self.assertEqual(rendered.count("Hello, I am Xiaoyu, and I'm happy to help!"), 1)
        self.assertNotIn('{"tool', rendered)
        self.assertIn("\n\n", rendered)

    def test_consume_streaming_ai_response_buffers_blank_lines_before_plain_tool_json(self):
        class _FakeStdout:
            def __init__(self):
                self.writes = []

            def write(self, text):
                self.writes.append(str(text or ""))
                return len(str(text or ""))

            def flush(self):
                return None

            def isatty(self):
                return False

        class _Agent:
            def _hide_previous_shell_output_if_needed(self):
                return None

            def _ensure_terminal_line_start(self):
                return None

        fake_out = _FakeStdout()
        chunks = [
            "Hello!\n\n",
            "{\"tool\":\"done\",\"args\":{}}",
        ]
        with patch("src.runtime.runtime_loop.sys.stdout", fake_out):
            ai_response, streamed_any = _consume_streaming_ai_response(_Agent(), chunks)

        rendered = "".join(fake_out.writes)
        self.assertIn("\"tool\":\"done\"", ai_response)
        self.assertTrue(streamed_any)
        self.assertIn("Hello!", rendered)
        self.assertNotIn("\"tool\":\"done\"", rendered)

    def test_consume_streaming_ai_response_keeps_assistant_tool_call_marker_text(self):
        class _FakeStdout:
            def __init__(self):
                self.writes = []

            def write(self, text):
                self.writes.append(str(text or ""))
                return len(str(text or ""))

            def flush(self):
                return None

            def isatty(self):
                return False

        class _Agent:
            def _hide_previous_shell_output_if_needed(self):
                return None

            def _ensure_terminal_line_start(self):
                return None

        fake_out = _FakeStdout()
        chunks = [
            "Hello! What task can I help you with?\n",
            "<|assistant",
            " tool_calls|>{\"tool\":\"done\",\"args\":{}}<|assistant tool_calls|>",
        ]
        with patch("src.runtime.runtime_loop.sys.stdout", fake_out):
            ai_response, streamed_any = _consume_streaming_ai_response(_Agent(), chunks)

        rendered = "".join(fake_out.writes)
        self.assertIn("<|assistant tool_calls|>", ai_response)
        self.assertTrue(streamed_any)
        self.assertIn("Hello! What task can I help you with?", rendered)
        self.assertNotIn("<|assistant", rendered)
        self.assertNotIn('{"tool"', rendered)

    def test_streamed_final_message_tool_calls_can_be_parsed_after_text_stream(self):
        class _FakeStream:
            final_message = {
                "role": "assistant",
                "content": "Reading",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"README.md"}',
                        },
                    }
                ],
            }

            def __iter__(self):
                yield "Read"
                yield "ing"

        class _FakeStdout:
            def write(self, text):
                return len(str(text or ""))

            def flush(self):
                return None

            def isatty(self):
                return False

        class _Agent:
            def _hide_previous_shell_output_if_needed(self):
                return None

            def _ensure_terminal_line_start(self):
                return None

        stream = _FakeStream()
        with patch("src.runtime.runtime_loop.sys.stdout", _FakeStdout()):
            ai_response, streamed_any = _consume_streaming_ai_response(_Agent(), stream)
        self.assertEqual(ai_response, "Reading")
        self.assertTrue(streamed_any)
        tool_plan = _parse_tool_plan_from_model_message(stream.final_message)
        self.assertEqual(tool_plan, ("read_file", {"path": "README.md"}))

    def test_parse_tool_plans_from_model_message_returns_all_tool_calls(self):
        message = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": "{\"path\":\"README.md\"}",
                    },
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "project_context_search",
                        "arguments": {
                            "query": "streaming",
                        },
                    },
                },
            ],
        }
        self.assertEqual(
            _parse_tool_plans_from_model_message(message),
            [
                ("read_file", {"path": "README.md"}),
                ("project_context_search", {"query": "streaming"}),
            ],
        )
        self.assertEqual(
            _parse_tool_plan_from_model_message(message),
            ("read_file", {"path": "README.md"}),
        )

    def test_parse_tool_plans_from_model_message_ignores_non_suffix_pseudo_tool_calls(self):
        message = {
            "role": "assistant",
            "content": (
                "<tool_calls>\n"
                "{\"tool\":\"shell\",\"name\":\"shell\",\"arguments\":{\"command\":\"ping www.baidu.com -n 4\"}}\n"
                "</tool_calls>"
            ),
        }
        self.assertEqual(_parse_tool_plans_from_model_message(message), [])

    def test_parse_tool_plans_from_model_message_accepts_trailing_text_tool_calls(self):
        message = {
            "role": "assistant",
            "content": (
                "I will query Codex usage.\n\n"
                "Step 1 [in_progress]: Query Codex usage\n\n"
                "{\n"
                "  \"tool_calls\": [\n"
                "    {\n"
                "      \"tool\": \"shell\",\n"
                "      \"args\": {\n"
                "        \"command\": \"npx @ccusage/codex@latest daily --compact\"\n"
                "      }\n"
                "    }\n"
                "  ]\n"
                "}"
            ),
        }
        self.assertEqual(
            _parse_tool_plans_from_model_message(message),
            [("shell", {"command": "npx @ccusage/codex@latest daily --compact"})],
        )

    def test_parse_tool_plans_from_model_message_accepts_trailing_single_tool_object(self):
        message = {
            "role": "assistant",
            "content": (
                "Done\n\n"
                "{\n"
                "  \"tool\": \"done\",\n"
                "  \"args\": {}\n"
                "}"
            ),
        }
        self.assertEqual(_parse_tool_plans_from_model_message(message), [("done", {})])

    def test_parse_tool_plans_from_model_message_accepts_minified_single_tool_object(self):
        message = {
            "role": "assistant",
            "content": "Done\n\n{\"tool\":\"done\",\"args\":{}}",
        }
        self.assertEqual(_parse_tool_plans_from_model_message(message), [("done", {})])

    def test_parse_tool_plans_from_model_message_accepts_plain_fenced_single_tool_object(self):
        message = {
            "role": "assistant",
            "content": (
                "Done\n\n"
                "```\n"
                "{\n"
                "  \"tool\": \"done\",\n"
                "  \"args\": {}\n"
                "}\n"
                "```"
            ),
        }
        self.assertEqual(_parse_tool_plans_from_model_message(message), [("done", {})])

    def test_parse_tool_plans_from_model_message_accepts_plain_fenced_tool_calls_object(self):
        message = {
            "role": "assistant",
            "content": (
                "Done\n\n"
                "```\n"
                "{\"tool_calls\":[{\"tool\":\"done\",\"args\":{}}]}\n"
                "```"
            ),
        }
        self.assertEqual(_parse_tool_plans_from_model_message(message), [("done", {})])

    def test_parse_tool_plans_from_model_message_accepts_plain_fenced_tool_array(self):
        message = {
            "role": "assistant",
            "content": (
                "Done\n\n"
                "```\n"
                "[{\"tool\":\"done\",\"args\":{}}]\n"
                "```"
            ),
        }
        self.assertEqual(_parse_tool_plans_from_model_message(message), [("done", {})])

    def test_parse_tool_plans_from_model_message_accepts_trailing_tool_array(self):
        message = {
            "role": "assistant",
            "content": (
                "Done\n\n"
                "[\n"
                "  {\"tool\":\"done\",\"args\":{}}\n"
                "]"
            ),
        }
        self.assertEqual(_parse_tool_plans_from_model_message(message), [("done", {})])

    def test_split_trailing_pseudo_tool_calls_text_removes_suffix(self):
        text = (
            "Plan\n"
            "Step 1 [in_progress]: Run command\n\n"
            "{\n"
            "  \"tool_calls\": [\n"
            "    {\"tool\": \"shell\", \"args\": {\"command\": \"echo hi\"}}\n"
            "  ]\n"
            "}"
        )
        visible, plans = _split_trailing_pseudo_tool_calls_text(text)
        self.assertEqual(visible, "Plan\nStep 1 [in_progress]: Run command")
        self.assertEqual(plans, [("shell", {"command": "echo hi"})])

    def test_split_trailing_pseudo_tool_calls_text_removes_tool_array_suffix(self):
        text = (
            "Done\n\n"
            "[\n"
            "  {\"tool\":\"done\",\"args\":{}}\n"
            "]"
        )
        visible, plans, pseudo_text = _split_trailing_pseudo_tool_calls_text_details(text)
        self.assertEqual(visible, "Done")
        self.assertEqual(plans, [("done", {})])
        self.assertTrue(pseudo_text.startswith("["))

    def test_split_trailing_pseudo_tool_calls_text_uses_serialized_content(self):
        text = (
            "{"
            "\"content\":\"Final answer\","
            "\"tool_calls\":[{\"function\":{\"name\":\"done\",\"arguments\":\"{}\"}}]"
            "}"
        )
        visible, plans, pseudo_text = _split_trailing_pseudo_tool_calls_text_details(text)
        self.assertEqual(visible, "Final answer")
        self.assertEqual(plans, [("done", {})])
        self.assertIn('"tool_calls"', pseudo_text)

    def test_split_trailing_pseudo_tool_calls_text_details_returns_suffix(self):
        text = (
            "Plan\n\n"
            "{\n"
            "  \"tool_calls\": [\n"
            "    {\"tool\": \"shell\", \"args\": {\"command\": \"echo hi\"}}\n"
            "  ]\n"
            "}"
        )
        visible, plans, pseudo_text = _split_trailing_pseudo_tool_calls_text_details(text)
        self.assertEqual(visible, "Plan")
        self.assertEqual(plans, [("shell", {"command": "echo hi"})])
        self.assertIn('"tool_calls"', pseudo_text)
        self.assertIn('"command": "echo hi"', pseudo_text)

    def test_strip_leaked_internal_history_markers_removes_suffix(self):
        text = (
            "Final answer\n\n"
            "[MODEL_TOOL_RESULT]{\"kind\":\"model_tool_result\",\"tool\":\"done\"}"
        )
        self.assertEqual(_strip_leaked_internal_history_markers(text), "Final answer")

    def test_strip_leaked_internal_history_markers_handles_bare_prefix(self):
        """Malformed sentinels missing the closing bracket (e.g. when the
        model echoes the prefix as ``[MODEL_TOOL_RESULT接下来...``) must also
        be stripped from persisted assistant content."""
        text = (
            "Final answer\n\n"
            "[MODEL_TOOL_RESULT接下来我将继续工作"
        )
        self.assertEqual(_strip_leaked_internal_history_markers(text), "Final answer")

    def test_stream_visible_text_withholds_partial_internal_marker_prefix(self):
        """While a streaming chunk only contains the beginning of
        ``[MODEL_TOOL_RESULT]``, the partial prefix must be withheld from the
        terminal until the rest arrives, otherwise users see leaks like
        ``[MODEL_TOOL_RES`` flash into the live output."""
        raw = "Hello\n[MODEL_TOOL_R"
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, "Hello")

    def test_stream_visible_text_hides_internal_model_tool_result_marker_without_closing_bracket(self):
        """If the model echoes the literal sentinel without the trailing
        ``]`` (a common hallucination pattern when echoing chat history),
        the live stream must still cut at the leak so the user never sees
        ``[MODEL_TOOL_RESULT接下来...`` in the terminal."""
        raw = "Done\n[MODEL_TOOL_RESULT接下来我将使用获取到的实时数据"
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, "Done")
        out_final = _stream_visible_text_with_json_pause(raw, final=True)
        self.assertEqual(out_final, "Done")

    def test_stream_visible_text_does_not_withhold_plain_bracket_prefix(self):
        """Lone ``[`` characters in normal prose (e.g. ``[link](...)``) must
        not be withheld by the partial-marker guard."""
        raw = "see [docs](https://example.com) for details"
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw)

    def test_replace_latest_assistant_history_records_pseudo_tool_call_metadata(self):
        old_content = (
            "Plan\n\n"
            "{\"tool_calls\":[{\"tool\":\"shell\",\"args\":{\"command\":\"echo hi\"}}]}"
        )

        class _Agent:
            def __init__(self):
                self.conversation_history = [
                    {"role": "user", "content": "x"},
                    {"role": "assistant", "content": old_content},
                ]
                self.synced = 0

            def _sync_active_chat_messages(self):
                self.synced += 1

        agent = _Agent()
        _replace_latest_assistant_history_content(
            agent,
            old_content,
            "Plan",
            pseudo_tool_call_text='{"tool_calls":[{"tool":"shell"}]}',
            pseudo_tool_call_tools=["shell"],
        )

        msg = agent.conversation_history[-1]
        self.assertEqual(msg.get("content"), "Plan")
        self.assertEqual(msg.get("pseudo_tool_call_text"), '{"tool_calls":[{"tool":"shell"}]}')
        self.assertEqual(msg.get("pseudo_tool_call_tools"), ["shell"])
        self.assertEqual(agent.synced, 1)

    def test_parse_tool_plans_from_model_message_accepts_trailing_fenced_tool_calls(self):
        message = {
            "role": "assistant",
            "content": (
                "Plan\n\n"
                "```json\n"
                "{\"tool_calls\":[{\"function\":{\"name\":\"done\",\"arguments\":\"{}\"}}]}\n"
                "```"
            ),
        }
        self.assertEqual(_parse_tool_plans_from_model_message(message), [("done", {})])

    def test_standard_tool_calls_take_precedence_over_trailing_text_tool_calls(self):
        message = {
            "role": "assistant",
            "content": "{\"tool_calls\":[{\"tool\":\"shell\",\"args\":{\"command\":\"wrong\"}}]}",
            "tool_calls": [
                {
                    "function": {
                        "name": "done",
                        "arguments": "{}",
                    },
                },
            ],
        }
        self.assertEqual(_parse_tool_plans_from_model_message(message), [("done", {})])

    def test_pseudo_tool_call_text_detection(self):
        self.assertTrue(_looks_like_pseudo_tool_call_text('{"tool":"done","args":{}}'))
        self.assertTrue(_looks_like_pseudo_tool_call_text('<tool_calls>\n{"tool":"done"}\n</tool_calls>'))
        self.assertTrue(_looks_like_pseudo_tool_call_text('tool: done\nargs: {}'))
        self.assertTrue(_looks_like_pseudo_tool_call_text('```json\n{"tool_calls":[{"function":{"name":"done"}}]}\n```'))
        self.assertTrue(_looks_like_pseudo_tool_call_text('{"content":"plan","tool_calls":[{"function":{"name":"done"}}]}'))
        self.assertFalse(_looks_like_pseudo_tool_call_text('{"toolbox": true}'))
        self.assertFalse(_looks_like_pseudo_tool_call_text('I checked the tool output and summarized it.'))


class ActivePlanReminderTests(unittest.TestCase):
    """Cover the helpers that re-inject the persisted `update_plan` state
    into the model's follow-up context. Without this re-injection the model
    loses sight of in-flight steps and forgets to flush completion."""

    class _Manager:
        def __init__(self, plan_record):
            self._plan_record = plan_record

        def active_chat_plan(self):
            return self._plan_record

    class _Agent:
        def __init__(self, plan_record):
            self._chat_state_manager = ActivePlanReminderTests._Manager(plan_record)

    def test_summarize_active_plan_returns_none_when_no_chat_or_manager(self):
        class _NoManagerAgent:
            pass

        self.assertIsNone(_summarize_active_plan(_NoManagerAgent()))

    def test_summarize_active_plan_returns_none_for_empty_record(self):
        agent = self._Agent({"plan": [], "explanation": "", "updated_at": ""})
        self.assertIsNone(_summarize_active_plan(agent))

    def test_summarize_active_plan_drops_unusable_entries_and_flags_pending(self):
        agent = self._Agent({
            "plan": [
                {"step": "Survey code paths", "status": "completed"},
                {"step": "  ", "status": "pending"},
                "not-a-dict",
                {"step": "Write tests", "status": "in_progress"},
                {"step": "Run suite", "status": "pending"},
            ],
            "explanation": "",
            "updated_at": "",
        })
        summary = _summarize_active_plan(agent)
        self.assertIsNotNone(summary)
        self.assertEqual(
            summary["items"],
            [
                {"step": "Survey code paths", "status": "completed"},
                {"step": "Write tests", "status": "in_progress"},
                {"step": "Run suite", "status": "pending"},
            ],
        )
        self.assertTrue(summary["has_pending"])
        self.assertEqual(summary["in_progress_step"], "Write tests")

    def test_summarize_active_plan_reports_all_completed(self):
        agent = self._Agent({
            "plan": [
                {"step": "A", "status": "completed"},
                {"step": "B", "status": "completed"},
            ],
            "explanation": "",
            "updated_at": "",
        })
        summary = _summarize_active_plan(agent)
        self.assertIsNotNone(summary)
        self.assertFalse(summary["has_pending"])
        self.assertEqual(summary["in_progress_step"], "")

    def test_summarize_active_plan_coerces_unknown_status_to_pending(self):
        agent = self._Agent({
            "plan": [
                {"step": "Mystery", "status": "weird"},
            ],
            "explanation": "",
            "updated_at": "",
        })
        summary = _summarize_active_plan(agent)
        self.assertIsNotNone(summary)
        self.assertEqual(summary["items"][0]["status"], "pending")
        self.assertTrue(summary["has_pending"])

    def test_format_active_plan_reminder_emits_pending_hint_when_unfinished(self):
        summary = {
            "items": [
                {"step": "Survey", "status": "completed"},
                {"step": "Write tests", "status": "in_progress"},
                {"step": "Ship", "status": "pending"},
            ],
            "has_pending": True,
            "in_progress_step": "Write tests",
        }
        text = _format_active_plan_reminder(summary)
        self.assertIn("[Active plan]", text)
        # Status markers must appear in order, so the model sees what is done
        # vs. still in flight at a glance.
        self.assertLess(text.index("[x] Survey"), text.index("[~] Write tests"))
        self.assertLess(text.index("[~] Write tests"), text.index("[ ] Ship"))
        # The hint must explicitly request another `update_plan` call so the
        # model is nudged to flush completion before answering.
        self.assertIn("`update_plan`", text)
        self.assertIn("completed", text)

    def test_format_active_plan_reminder_allows_ask_more_info_escape_when_pending(self):
        """The pending-plan reminder must explicitly allow the model to call
        ``ask_more_info`` instead of finalizing the plan. Otherwise the
        finalization pressure can starve out a legitimate clarifying
        question and force the model to mark pending steps ``completed``
        prematurely just to end the turn."""
        summary = {
            "items": [
                {"step": "Investigate ambiguity", "status": "in_progress"},
                {"step": "Implement decision", "status": "pending"},
            ],
            "has_pending": True,
            "in_progress_step": "Investigate ambiguity",
        }
        text = _format_active_plan_reminder(summary)
        self.assertIn("`ask_more_info`", text)
        self.assertIn(
            "do not mark pending steps as `completed` just to end the turn",
            text,
        )

    def test_format_active_plan_reminder_emits_done_hint_when_all_completed(self):
        summary = {
            "items": [
                {"step": "A", "status": "completed"},
                {"step": "B", "status": "completed"},
            ],
            "has_pending": False,
            "in_progress_step": "",
        }
        text = _format_active_plan_reminder(summary)
        self.assertIn("All plan steps are `completed`", text)
        # When everything is finished we must not keep pestering the model to
        # call `update_plan` again — only to wrap up in natural language.
        self.assertNotIn("flip it to", text)

    def test_format_active_plan_reminder_returns_empty_for_no_items(self):
        self.assertEqual(_format_active_plan_reminder({}), "")
        self.assertEqual(_format_active_plan_reminder({"items": []}), "")


class PseudoToolCallRetryPromptTests(unittest.TestCase):
    """Pin down the staged retry-prompt escalation.

    The runtime asks the model to retry whenever it writes a pseudo
    tool call in assistant text instead of using standard
    ``tool_calls``. The first retry uses a purely descriptive nudge;
    if the model fails again, the second retry must additionally
    embed a concrete OpenAI-API-shape ``tool_calls`` example so the
    model has an unambiguous template to mirror.
    """

    def test_first_attempt_uses_base_prompt_without_example(self):
        prompt = _build_pseudo_tool_call_retry_prompt(
            original_user_task="reproduce the bug",
            attempt=1,
        )
        self.assertIn("[Original user request]\nreproduce the bug", prompt)
        self.assertIn("pseudo tool call", prompt)
        # The example must NOT appear yet — we want to give the
        # descriptive prompt a chance to work first.
        self.assertNotIn(_PSEUDO_TOOL_CALL_RETRY_EXAMPLE_JSON, prompt)
        self.assertNotIn("previous retry also produced", prompt)

    def test_second_attempt_appends_example_block(self):
        prompt = _build_pseudo_tool_call_retry_prompt(
            original_user_task="reproduce the bug",
            attempt=2,
        )
        # Base content remains.
        self.assertIn("[Original user request]\nreproduce the bug", prompt)
        # Escalation banner.
        self.assertIn("previous retry also produced pseudo tool-call text", prompt)
        # Concrete example.
        self.assertIn(_PSEUDO_TOOL_CALL_RETRY_EXAMPLE_JSON, prompt)
        # Reinforcing notes following the example.
        self.assertIn("`arguments` MUST be a JSON string", prompt)
        self.assertIn("client-unique `id`", prompt)

    def test_third_and_later_attempts_keep_example_block(self):
        prompt = _build_pseudo_tool_call_retry_prompt(
            original_user_task="task",
            attempt=3,
        )
        self.assertIn(_PSEUDO_TOOL_CALL_RETRY_EXAMPLE_JSON, prompt)

    def test_zero_attempt_is_treated_as_base_only(self):
        # Defensive: callers should always pass attempt >= 1 since
        # the prompt only fires on a retry, but a zero must not
        # leak the example block either.
        prompt = _build_pseudo_tool_call_retry_prompt(
            original_user_task="task",
            attempt=0,
        )
        self.assertNotIn(_PSEUDO_TOOL_CALL_RETRY_EXAMPLE_JSON, prompt)

    def test_example_json_is_valid_openai_tool_calls_payload(self):
        # The example must literally match what the OpenAI tool API
        # produces, so the model can copy the shape verbatim. We
        # check the wire-level invariants here rather than just
        # inspecting the string.
        import json

        parsed = json.loads(_PSEUDO_TOOL_CALL_RETRY_EXAMPLE_JSON)
        self.assertIsInstance(parsed, dict)
        self.assertIn("tool_calls", parsed)
        calls = parsed["tool_calls"]
        self.assertIsInstance(calls, list)
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call.get("type"), "function")
        self.assertTrue(str(call.get("id") or "").strip())
        fn = call.get("function")
        self.assertIsInstance(fn, dict)
        self.assertTrue(str(fn.get("name") or "").strip())
        # ``arguments`` MUST be a JSON-encoded STRING (not a dict)
        # per the OpenAI tool-calling spec; the inner string itself
        # must parse back to a JSON object describing the args.
        args_field = fn.get("arguments")
        self.assertIsInstance(args_field, str)
        inner = json.loads(args_field)
        self.assertIsInstance(inner, dict)


if __name__ == "__main__":
    unittest.main()
