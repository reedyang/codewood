import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.runtime.runtime_loop import (
    _consume_streaming_ai_response,
    _compute_unreviewed_changed_files,
    _extract_done_reviewed_files,
    _build_minimal_verification_command,
    _format_worked_for_summary_line,
    _format_startup_directory,
    _model_tool_result_was_aborted,
    _render_aborted_direct_shell_feedback,
    _refresh_context_usage_after_task_boundary,
    _resolve_worked_summary_terminal_width,
    _sanitize_prompt_pollution,
    _sync_command_input_history,
    _should_record_command_input_history,
    _shell_command_indicates_verification,
    _stream_visible_text_with_json_pause,
    _stop_pre_task_status_ticker_for_console_output,
    _try_record_user_task_message,
    _tool_change_and_verification_hints,
    _parse_tool_plans_from_model_message,
    _parse_tool_plan_from_model_message,
    _is_textless_done_only_tool_call,
)


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

    def test_shell_command_indicates_verification(self):
        self.assertTrue(_shell_command_indicates_verification("pytest -q"))
        self.assertTrue(_shell_command_indicates_verification("python -m py_compile a.py"))
        self.assertFalse(_shell_command_indicates_verification("echo hello"))

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

    def test_build_minimal_verification_command_prefers_py_compile(self):
        cmd = _build_minimal_verification_command(["a.py", "b.txt"])
        self.assertIn("python -m py_compile", cmd)
        self.assertIn("a.py", cmd)

    def test_tool_change_and_verification_hints(self):
        hints_change = _tool_change_and_verification_hints(
            "apply_patch",
            {"path": "helloworld.py"},
            {"success": True},
        )
        self.assertTrue(bool(hints_change.get("code_changed")))
        self.assertIn("helloworld.py", hints_change.get("changed_files") or [])

        hints_verify = _tool_change_and_verification_hints(
            "shell",
            {"command": "pytest -q"},
            {"success": True},
        )
        self.assertTrue(bool(hints_verify.get("verified")))

    def test_extract_done_reviewed_files_parses_list_and_string(self):
        self.assertEqual(
            _extract_done_reviewed_files({"reviewed_files": ["a.py", " b.py ", ""]}),
            ["a.py", "b.py"],
        )
        self.assertEqual(
            _extract_done_reviewed_files({"reviewed_files": "README.md"}),
            ["README.md"],
        )
        self.assertEqual(_extract_done_reviewed_files({"reviewed_files": 123}), [])

    def test_compute_unreviewed_changed_files_reports_only_missing(self):
        changed = ["src/A.py", "docs/Guide.md", "README.md"]
        reviewed = ["src/a.py", "Guide.md"]
        missing = _compute_unreviewed_changed_files(changed, reviewed)
        self.assertEqual(missing, ["README.md"])

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
        self.assertEqual(len(line), 40)
        self.assertTrue(line.startswith("─ Worked for 1m 5s "))
        self.assertTrue(line.endswith("─"))

    def test_format_worked_for_summary_line_hides_minutes_when_under_one_minute(self):
        line = _format_worked_for_summary_line(elapsed_seconds=59, terminal_width=40)
        self.assertEqual(len(line), 40)
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

    def test_stream_visible_text_with_json_pause_keeps_incomplete_json_fence(self):
        raw = "先做检查\n```json\n{\"tool\":\"shell\",\"args\":{"
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw)

    def test_stream_visible_text_with_json_pause_keeps_partial_json_fence_opener(self):
        raw = "先做检查\n```"
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw)

    def test_stream_visible_text_with_json_pause_keeps_tool_json_fence_when_complete(self):
        raw = (
            "准备执行\n"
            "```json\n"
            "{\"tool\":\"shell\",\"args\":{\"command\":\"Get-Content a.py\"}}\n"
            "```\n"
            "继续"
        )
        out = _stream_visible_text_with_json_pause(raw, final=True)
        self.assertEqual(out, raw)

    def test_stream_visible_text_with_json_pause_keeps_non_tool_json_fence(self):
        raw = (
            "说明\n"
            "```json\n"
            "{\"foo\":1}\n"
            "```\n"
            "完成"
        )
        out = _stream_visible_text_with_json_pause(raw, final=True)
        self.assertEqual(out, raw)

    def test_stream_visible_text_with_json_pause_keeps_trailing_plain_tool_json(self):
        raw = (
            "我先说明一下。\n\n"
            "{\"tool\":\"done\",\"args\":{}}\n"
        )
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw)

    def test_stream_visible_text_with_json_pause_keeps_trailing_plain_tool_json_when_final(self):
        raw = (
            "我先说明一下。\n\n"
            "{\"tool\":\"done\",\"args\":{}}\n"
        )
        out = _stream_visible_text_with_json_pause(raw, final=True)
        self.assertEqual(out, raw)

    def test_stream_visible_text_with_json_pause_keeps_partial_plain_tool_json(self):
        raw = (
            "我先说明一下。\n\n"
            "{\"tool"
        )
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw)

    def test_stream_visible_text_with_json_pause_keeps_plain_non_tool_json(self):
        raw = (
            "数据如下：\n\n"
            "{\"foo\":1}"
        )
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw)

    def test_stream_visible_text_keeps_pseudo_tool_calls_block(self):
        raw = (
            "准备执行\n\n"
            "<tool_calls>\n"
            "{\"tool\":\"shell\",\"name\":\"shell\",\"arguments\":{\"command\":\"ping www.baidu.com -n 4\"}}\n"
            "</tool_calls>"
        )
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw)

    def test_stream_visible_text_keeps_unclosed_pseudo_tool_calls_block(self):
        raw = (
            "准备执行\n\n"
            "<tool_calls>\n"
            "{\"tool\":\"shell\""
        )
        out = _stream_visible_text_with_json_pause(raw, final=False)
        self.assertEqual(out, raw)

    def test_stream_visible_text_with_json_pause_keeps_tool_json_array_fence_when_complete(self):
        raw = (
            "准备执行\n"
            "```json\n"
            "[\n"
            "  {\"tool\":\"shell\",\"args\":{\"command\":\"Get-Content a.py\"}},\n"
            "  {\"tool\":\"project_context_search\",\"args\":{\"query\":\"foo\"}}\n"
            "]\n"
            "```\n"
            "继续"
        )
        out = _stream_visible_text_with_json_pause(raw, final=True)
        self.assertEqual(out, raw)

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
            "你好，我是小雨，很高兴为你服务！\n\n",
            "{\"tool",
            "\":\"done\",\"args\":{}}",
        ]
        with patch("src.runtime.runtime_loop.sys.stdout", fake_out):
            ai_response, streamed_any = _consume_streaming_ai_response(_Agent(), chunks)

        rendered = "".join(fake_out.writes)
        self.assertIn("\"tool\":\"done\"", ai_response)
        self.assertTrue(streamed_any)
        self.assertEqual(rendered.count("你好，我是小雨，很高兴为你服务！"), 1)
        self.assertIn('{"tool', rendered)
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
            "你好！\n\n",
            "{\"tool\":\"done\",\"args\":{}}",
        ]
        with patch("src.runtime.runtime_loop.sys.stdout", fake_out):
            ai_response, streamed_any = _consume_streaming_ai_response(_Agent(), chunks)

        rendered = "".join(fake_out.writes)
        self.assertIn("\"tool\":\"done\"", ai_response)
        self.assertTrue(streamed_any)
        self.assertIn("你好！", rendered)
        self.assertIn("\"tool\":\"done\"", rendered)

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
            "你好！有什么我可以帮您处理的任务吗？\n",
            "<|assistant",
            " tool_calls|>{\"tool\":\"done\",\"args\":{}}<|assistant tool_calls|>",
        ]
        with patch("src.runtime.runtime_loop.sys.stdout", fake_out):
            ai_response, streamed_any = _consume_streaming_ai_response(_Agent(), chunks)

        rendered = "".join(fake_out.writes)
        self.assertIn("<|assistant tool_calls|>", ai_response)
        self.assertTrue(streamed_any)
        self.assertIn("你好！有什么我可以帮您处理的任务吗？", rendered)
        self.assertIn("<|assistant", rendered)
        self.assertIn('{"tool"', rendered)

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

    def test_parse_tool_plans_from_model_message_ignores_text_pseudo_tool_calls(self):
        message = {
            "role": "assistant",
            "content": (
                "<tool_calls>\n"
                "{\"tool\":\"shell\",\"name\":\"shell\",\"arguments\":{\"command\":\"ping www.baidu.com -n 4\"}}\n"
                "</tool_calls>"
            ),
        }
        self.assertEqual(_parse_tool_plans_from_model_message(message), [])

    def test_textless_done_only_tool_call_detection(self):
        self.assertTrue(
            _is_textless_done_only_tool_call("", [("done", {})])
        )
        self.assertTrue(
            _is_textless_done_only_tool_call(None, [("done", {}), ("done", {"reviewed_files": []})])
        )
        self.assertFalse(
            _is_textless_done_only_tool_call("已完成。", [("done", {})])
        )
        self.assertFalse(
            _is_textless_done_only_tool_call("", [("read_file", {"path": "README.md"})])
        )
        self.assertFalse(_is_textless_done_only_tool_call("", []))


if __name__ == "__main__":
    unittest.main()
