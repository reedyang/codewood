import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.runtime.runtime_loop import (
    _build_minimal_verification_command,
    _format_worked_for_summary_line,
    _format_startup_directory,
    _refresh_context_usage_after_task_boundary,
    _resolve_worked_summary_terminal_width,
    _sanitize_prompt_pollution,
    _sync_command_input_history,
    _should_record_command_input_history,
    _shell_command_indicates_verification,
    _stop_pre_task_status_ticker_for_console_output,
    _try_record_user_task_message,
    _tool_change_and_verification_hints,
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


if __name__ == "__main__":
    unittest.main()
