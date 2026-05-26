import sys
import types
import unittest
from unittest.mock import patch


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.smart_shell_agent import SmartShellAgent


class _FakeHistoryManager:
    def reset_index(self):
        return None


class _FakeInputHandler:
    renders_prompt_separator_inline = False

    def get_input_with_completion(self, prompt, **kwargs):
        return "hello"


class _FakeInputHandlerWithColumns(_FakeInputHandler):
    def get_terminal_columns(self, default=80):
        return 6


class _FakeStdout:
    def __init__(self):
        self.writes = []

    def isatty(self):
        return True

    def write(self, text):
        self.writes.append(str(text))
        return len(str(text))

    def flush(self):
        return None


class PromptSeparatorBehaviorTests(unittest.TestCase):
    def _build_agent(self) -> SmartShellAgent:
        agent = SmartShellAgent.__new__(SmartShellAgent)
        agent._startup_prompt_pending = False
        agent._suppress_next_separator = False
        agent._show_separator_next_prompt = False
        agent._prompt_separator_rendered = False
        agent._chat_history_reload_last_terminal_width = 0
        agent._chat_history_first_visible_index_map = {}
        agent._force_reload_chat_history_from_anchor_once = False
        agent.active_chat_id = "chat-1"
        agent.workspace_id = "default"
        agent.conversation_history = []
        agent.active_chat_name = "Demo Chat"
        agent.input_handler = _FakeInputHandler()
        agent.history_manager = _FakeHistoryManager()
        return agent

    def test_separator_is_rendered_only_when_requested(self):
        agent = self._build_agent()
        agent._show_separator_next_prompt = True
        with (
            patch.object(agent, "_status_bar_render_data", return_value=([], "status")),
            patch.object(agent, "_print_prompt_separator") as mock_separator,
        ):
            out = agent._get_user_input_with_history()
        self.assertEqual(out, "hello")
        mock_separator.assert_called_once_with()
        self.assertFalse(agent._show_separator_next_prompt)

    def test_separator_is_not_rendered_by_default(self):
        agent = self._build_agent()
        with (
            patch.object(agent, "_status_bar_render_data", return_value=([], "status")),
            patch.object(agent, "_print_prompt_separator") as mock_separator,
        ):
            out = agent._get_user_input_with_history()
        self.assertEqual(out, "hello")
        mock_separator.assert_not_called()

    def test_separator_has_blank_line_above_and_below(self):
        agent = self._build_agent()
        agent.input_handler = _FakeInputHandlerWithColumns()
        fake_stdout = _FakeStdout()
        with (
            patch("src.smart_shell_agent._ansi_gray", side_effect=lambda s: s),
            patch("src.smart_shell_agent.sys.stdout", fake_stdout),
        ):
            agent._print_prompt_separator()
        out = "".join(fake_stdout.writes)
        self.assertEqual(out, "\n──────\n\n")

    def test_chat_history_direct_shell_output_always_prints_separator(self):
        agent = self._build_agent()
        agent.active_chat_name = "Demo Chat"
        agent.conversation_history = [
            {
                "role": "user",
                "content": agent._build_direct_shell_user_history_content("git status"),
            },
            {
                "role": "assistant",
                "content": agent._build_direct_shell_result_history_content(
                    "!git status",
                    "git status",
                    "D:/ws",
                    0,
                    "ok\n",
                    "",
                ),
            },
        ]
        with (
            patch("src.smart_shell_agent._ansi_gray", side_effect=lambda s: s),
            patch.object(agent, "_print_direct_shell_command_feedback"),
            patch.object(agent, "_print_direct_shell_history_output"),
            patch.object(agent, "_print_direct_shell_history_separator") as mock_sep,
            patch("builtins.print"),
        ):
            agent._print_chat_history()
        mock_sep.assert_called_once_with()
        self.assertFalse(agent._show_separator_next_prompt)

    def test_chat_history_multiple_direct_shell_outputs_each_get_separator(self):
        agent = self._build_agent()
        agent.active_chat_name = "Demo Chat"
        agent.conversation_history = [
            {
                "role": "user",
                "content": agent._build_direct_shell_user_history_content("git status"),
            },
            {
                "role": "assistant",
                "content": agent._build_direct_shell_result_history_content(
                    "!git status",
                    "git status",
                    "D:/ws",
                    0,
                    "ok\n",
                    "",
                ),
            },
            {"role": "user", "content": "普通消息"},
            {
                "role": "assistant",
                "content": agent._build_direct_shell_result_history_content(
                    "!echo hi",
                    "echo hi",
                    "D:/ws",
                    0,
                    "hi\n",
                    "",
                ),
            },
            {"role": "assistant", "content": "普通回复"},
        ]
        with (
            patch("src.smart_shell_agent._ansi_gray", side_effect=lambda s: s),
            patch.object(agent, "_print_direct_shell_command_feedback"),
            patch.object(agent, "_print_direct_shell_history_output"),
            patch.object(agent, "_print_direct_shell_history_separator") as mock_sep,
            patch("builtins.print"),
        ):
            agent._print_chat_history()
        self.assertEqual(mock_sep.call_count, 2)
        self.assertFalse(agent._show_separator_next_prompt)

    def test_chat_history_aborted_direct_shell_output_does_not_print_separator(self):
        agent = self._build_agent()
        agent.active_chat_name = "Demo Chat"
        agent.conversation_history = [
            {
                "role": "user",
                "content": agent._build_direct_shell_user_history_content("sleep 10"),
            },
            {
                "role": "assistant",
                "content": agent._build_direct_shell_result_history_content(
                    "!sleep 10",
                    "sleep 10",
                    "D:/ws",
                    137,
                    "command aborted by user\n",
                    "",
                    aborted_by_user=True,
                ),
            },
        ]
        with (
            patch("src.smart_shell_agent._ansi_gray", side_effect=lambda s: s),
            patch.object(agent, "_print_direct_shell_command_feedback"),
            patch.object(agent, "_print_direct_shell_history_output"),
            patch.object(agent, "_print_direct_shell_history_separator") as mock_sep,
            patch.object(agent, "_print_conversation_interrupted_banner") as mock_banner,
            patch("builtins.print"),
        ):
            agent._print_chat_history()
        mock_sep.assert_not_called()
        mock_banner.assert_called_once_with()
        self.assertFalse(agent._show_separator_next_prompt)

    def test_chat_history_aborted_direct_shell_output_always_moves_abort_marker_to_final_tail(self):
        agent = self._build_agent()
        agent.active_chat_name = "Demo Chat"
        raw_stdout = "Version: Platform(x86/x64): Branch name: command aborted by user\n"
        raw_stderr = "02:00:41 [Info] Searching artifacts...\n"
        agent.conversation_history = [
            {
                "role": "user",
                "content": agent._build_direct_shell_user_history_content("d:/tmp/builds/install-zr.bat"),
            },
            {
                "role": "assistant",
                "content": agent._build_direct_shell_result_history_content(
                    "!d:/tmp/builds/install-zr.bat",
                    "d:/tmp/builds/install-zr.bat",
                    "D:/ws",
                    137,
                    raw_stdout,
                    raw_stderr,
                    aborted_by_user=True,
                ),
            },
        ]
        with (
            patch("src.smart_shell_agent._ansi_gray", side_effect=lambda s: s),
            patch.object(agent, "_print_direct_shell_command_feedback"),
            patch.object(agent, "_print_direct_shell_history_separator") as mock_sep,
            patch.object(agent, "_print_conversation_interrupted_banner") as mock_banner,
            patch("builtins.print"),
        ):
            captured = {}

            def _capture_output(stdout_text, stderr_text, force_first_line_continuation=False):
                captured["stdout"] = str(stdout_text)
                captured["stderr"] = str(stderr_text)
                captured["force"] = bool(force_first_line_continuation)

            with patch.object(agent, "_print_direct_shell_history_output", side_effect=_capture_output):
                agent._print_chat_history()
        self.assertEqual(captured.get("stderr"), "")
        rendered = str(captured.get("stdout") or "")
        self.assertTrue(rendered.endswith("command aborted by user\n"))
        self.assertIn("02:00:41 [Info] Searching artifacts...\n", rendered)
        self.assertFalse(bool(captured.get("force", False)))
        mock_sep.assert_not_called()
        mock_banner.assert_called_once_with()

    def test_chat_history_task_interrupted_event_prints_banner(self):
        agent = self._build_agent()
        agent.active_chat_name = "Demo Chat"
        agent.conversation_history = [
            {"role": "user", "content": "继续执行上一个任务"},
            {
                "role": "assistant",
                "content": agent._build_conversation_interrupted_history_content(
                    interrupted_kind="task",
                    reason="user_interrupt",
                    detail="修复构建脚本",
                ),
            },
        ]
        with (
            patch("src.smart_shell_agent._ansi_gray", side_effect=lambda s: s),
            patch.object(agent, "_print_direct_shell_history_separator") as mock_sep,
            patch.object(agent, "_print_conversation_interrupted_banner") as mock_banner,
            patch("builtins.print"),
        ):
            agent._print_chat_history()
        mock_sep.assert_not_called()
        mock_banner.assert_called_once_with()

    def test_chat_history_replays_internal_slash_command_and_output(self):
        agent = self._build_agent()
        agent.active_chat_name = "Demo Chat"
        agent.conversation_history = [
            {
                "role": "user",
                "content": agent._build_internal_slash_user_history_content("/chat reload"),
            },
            {
                "role": "assistant",
                "content": agent._build_internal_slash_result_history_content(
                    raw_user_command="/chat reload",
                    output_text="line-1\nline-2\n",
                ),
            },
        ]
        with (
            patch("src.smart_shell_agent._ansi_gray", side_effect=lambda s: s),
            patch.object(agent, "_print_internal_slash_history_output") as mock_slash_out,
            patch("builtins.print") as mock_print,
        ):
            agent._print_chat_history()
        joined = "\n".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
        self.assertIn("› /chat reload", joined)
        mock_slash_out.assert_called_once_with("line-1\nline-2\n")

    def test_record_internal_slash_execution_history_skips_chat_reload(self):
        agent = self._build_agent()
        with patch.object(agent, "_append_chat_message") as mock_append:
            agent._record_internal_slash_execution_history(
                raw_user_command="/chat reload",
                output_text="should-not-be-recorded\n",
            )
        mock_append.assert_not_called()

    def test_record_internal_slash_execution_history_skips_clear_context_and_clear_screen(self):
        agent = self._build_agent()
        with patch.object(agent, "_append_chat_message") as mock_append:
            agent._record_internal_slash_execution_history(
                raw_user_command="/clear context",
                output_text="should-not-be-recorded\n",
            )
            agent._record_internal_slash_execution_history(
                raw_user_command="/clear screen",
                output_text="should-not-be-recorded\n",
            )
        mock_append.assert_not_called()

    def test_record_internal_slash_execution_history_skips_chat_switch(self):
        agent = self._build_agent()
        with patch.object(agent, "_append_chat_message") as mock_append:
            agent._record_internal_slash_execution_history(
                raw_user_command="/chat switch demo",
                output_text="should-not-be-recorded\n",
            )
            agent._record_internal_slash_execution_history(
                raw_user_command="/CHAT   SWITCH   2",
                output_text="should-not-be-recorded\n",
            )
        mock_append.assert_not_called()

    def test_record_internal_slash_execution_history_records_other_commands(self):
        agent = self._build_agent()
        with patch.object(agent, "_append_chat_message") as mock_append:
            agent._record_internal_slash_execution_history(
                raw_user_command="/chat list",
                output_text="listed\n",
            )
        self.assertEqual(mock_append.call_count, 2)

    def test_chat_history_replays_task_worked_summary_line(self):
        agent = self._build_agent()
        agent.active_chat_name = "Demo Chat"
        agent.conversation_history = [
            {
                "role": "assistant",
                "content": agent._build_task_worked_summary_history_content(125),
            },
        ]
        with (
            patch.object(agent, "_print_task_worked_summary_line") as mock_worked,
            patch("builtins.print"),
        ):
            agent._print_chat_history()
        mock_worked.assert_called_once_with(125)

    def test_resize_reload_uses_recorded_history_anchor(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old-reply"},
            {"role": "user", "content": "new"},
        ]
        agent._remember_active_chat_history_first_visible_index(2)
        widths = iter([100, 100])
        with (
            patch.object(agent, "_status_bar_render_data", return_value=([], "status")),
            patch.object(agent, "_terminal_columns_for_prompt_separator", side_effect=lambda default=80: next(widths)),
            patch.object(agent, "_print_chat_history") as mock_history,
            patch("src.smart_shell_agent.os.system"),
            patch("src.runtime.runtime_loop._print_startup_overview"),
        ):
            agent._chat_history_reload_last_terminal_width = 80
            out = agent._get_user_input_with_history()
        self.assertEqual(out, "hello")
        mock_history.assert_called_once_with(start_index=2)

    def test_resize_check_first_snapshot_does_not_reload(self):
        agent = self._build_agent()
        with (
            patch.object(agent, "_status_bar_render_data", return_value=([], "status")),
            patch.object(agent, "_terminal_columns_for_prompt_separator", return_value=120),
            patch.object(agent, "_reload_chat_history_from_anchor_on_resize") as mock_reload,
        ):
            out = agent._get_user_input_with_history()
        self.assertEqual(out, "hello")
        mock_reload.assert_not_called()

    def test_chat_history_with_end_anchor_does_not_print_empty_hint(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
        ]
        with patch("builtins.print") as mock_print:
            agent._print_chat_history(start_index=2)
        printed_text = "\n".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
        self.assertNotIn("当前 Chat 暂无历史消息", printed_text)

    def test_resize_detection_uses_same_input_handler_width_source_as_separator(self):
        agent = self._build_agent()
        class _InputHandlerCols100(_FakeInputHandler):
            def get_terminal_columns(self, default=80):
                return 100
        agent.input_handler = _InputHandlerCols100()
        with (
            patch.object(agent, "_status_bar_render_data", return_value=([], "status")),
            patch("src.smart_shell_agent.os.get_terminal_size", return_value=types.SimpleNamespace(columns=80)),
            patch.object(agent, "_reload_chat_history_from_anchor_on_resize") as mock_reload,
        ):
            agent._chat_history_reload_last_terminal_width = 80
            out = agent._get_user_input_with_history()
        self.assertEqual(out, "hello")
        mock_reload.assert_called_once_with()

    def test_force_reload_flag_triggers_reload_before_prompt(self):
        agent = self._build_agent()
        agent._force_reload_chat_history_from_anchor_once = True
        with (
            patch.object(agent, "_status_bar_render_data", return_value=([], "status")),
            patch.object(agent, "_reload_chat_history_from_anchor_on_resize") as mock_reload,
            patch.object(agent, "_maybe_reload_chat_history_on_terminal_resize") as mock_maybe,
        ):
            out = agent._get_user_input_with_history()
        self.assertEqual(out, "hello")
        mock_reload.assert_called_once_with()
        mock_maybe.assert_not_called()
        self.assertFalse(agent._force_reload_chat_history_from_anchor_once)

    def test_terminal_resize_callback_sets_force_reload_flag(self):
        agent = self._build_agent()
        should_interrupt = agent._handle_terminal_columns_changed_during_input(80, 120)
        self.assertTrue(should_interrupt)
        self.assertTrue(agent._force_reload_chat_history_from_anchor_once)
        self.assertEqual(agent._chat_history_reload_last_terminal_width, 120)

    def test_rewrite_previous_prompt_as_user_clears_all_multiline_rows_and_indents_continuation(self):
        agent = self._build_agent()
        fake_stdout = _FakeStdout()
        with (
            patch("src.smart_shell_agent._ansi_gray", side_effect=lambda s: s),
            patch("src.smart_shell_agent.sys.stdout", fake_stdout),
            patch.object(agent, "_estimate_rendered_line_count", return_value=2),
        ):
            agent._rewrite_previous_prompt_as_user("hello\n你好")
        out = "".join(fake_stdout.writes)
        self.assertIn("\x1b[1A\r\x1b[2K\x1b[1A\r\x1b[2K", out)
        self.assertIn("› hello\n  你好\n", out)
        self.assertNotIn("› hello\n你好\n", out)

    def test_chat_history_tool_feedback_marks_failed_from_operation_results(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {"role": "assistant", "content": "{\"tool\":\"read\",\"args\":{\"path\":\"a.txt\"}}"},
        ]
        agent.operation_results = [
            {
                "command": {"tool": "read", "args": {"path": "a.txt"}},
                "result": {"success": False},
            }
        ]
        with (
            patch("builtins.print"),
            patch.object(agent, "_print_tool_call_feedback") as mock_feedback,
        ):
            agent._print_chat_history()
        mock_feedback.assert_called_once_with("read", {"path": "a.txt"}, failed=True)

    def test_chat_history_replays_model_shell_output_using_direct_shell_renderer(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {
                "role": "assistant",
                "content": "{\"tool\":\"shell\",\"args\":{\"command\":\"echo hi\"}}",
            },
        ]
        agent.operation_results = [
            {
                "command": {"tool": "shell", "args": {"command": "echo hi"}},
                "result": {
                    "success": True,
                    "display_output": "  └ hi\n",
                    "display_stderr": "",
                },
            }
        ]
        with (
            patch("builtins.print"),
            patch.object(agent, "_print_tool_call_feedback") as mock_feedback,
            patch.object(agent, "_print_direct_shell_history_output") as mock_shell_output,
            patch.object(agent, "_print_direct_shell_history_separator") as mock_sep,
        ):
            agent._print_chat_history()
        mock_feedback.assert_called_once_with("shell", {"command": "echo hi"}, failed=False)
        mock_shell_output.assert_called_once_with("  └ hi\n", "")
        mock_sep.assert_not_called()

    def test_chat_history_model_shell_result_replays_failed_output_without_operation_results(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {
                "role": "assistant",
                "content": "{\"tool\":\"shell\",\"args\":{\"command\":\"test\"}}",
            },
            {
                "role": "assistant",
                "content": agent._build_model_tool_result_history_content(
                    "shell",
                    {"command": "test"},
                    {
                        "success": False,
                        "display_output": "'test' is not recognized\n",
                        "display_stderr": "",
                    },
                ),
            },
        ]
        agent.operation_results = []
        with (
            patch("builtins.print"),
            patch.object(agent, "_print_tool_call_feedback") as mock_feedback,
            patch.object(agent, "_print_direct_shell_history_output") as mock_shell_output,
            patch.object(agent, "_print_direct_shell_history_separator") as mock_sep,
        ):
            agent._print_chat_history()
        mock_feedback.assert_called_once_with("shell", {"command": "test"}, failed=True)
        mock_shell_output.assert_called_once_with("'test' is not recognized\n", "")
        mock_sep.assert_not_called()

    def test_refresh_after_tool_output_keeps_history_anchor_position(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ]
        agent._remember_active_chat_history_first_visible_index(2)
        with (
            patch("src.smart_shell_agent.os.system"),
            patch("src.runtime.runtime_loop._print_startup_overview"),
            patch.object(agent, "_sync_active_chat_messages") as mock_sync,
            patch.object(agent, "_print_chat_history") as mock_history,
        ):
            agent._refresh_chat_history_after_tool_output()
        mock_sync.assert_called_once_with()
        mock_history.assert_called_once_with(start_index=2)


if __name__ == "__main__":
    unittest.main()
