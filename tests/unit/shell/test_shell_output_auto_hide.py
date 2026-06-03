import sys
import types
import unittest
from unittest.mock import patch


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.agent import Agent


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


class _FakeInputHandler:
    def get_terminal_columns(self, default=80):
        return 40


class _FakePromptToolkitInputHandler:
    def __init__(self, session=None):
        self.session = session

    def get_terminal_columns(self, default=80):
        # Mirrors prompt_toolkit handler behavior: when session is unavailable,
        # return a fallback default width (usually 80).
        return int(default or 80)


class ShellOutputAutoHideTests(unittest.TestCase):
    def setUp(self):
        self.agent = Agent.__new__(Agent)
        self.agent._last_shell_output_visible_lines = 0

    def test_estimate_rendered_line_count_wraps_by_terminal_width(self):
        with patch.object(self.agent, "_terminal_columns_for_line_estimate", return_value=10):
            self.assertEqual(self.agent._estimate_rendered_line_count("12345678901"), 2)
            self.assertEqual(self.agent._estimate_rendered_line_count("a\n\nb"), 3)
            self.assertEqual(self.agent._estimate_rendered_line_count("🙂" * 6), 2)

    def test_register_shell_output_for_auto_hide_accumulates_stdout_stderr(self):
        with patch.object(self.agent, "_terminal_columns_for_line_estimate", return_value=80):
            self.agent._register_shell_output_for_auto_hide("line1\nline2\n", "err1\n")
        self.assertEqual(self.agent._last_shell_output_visible_lines, 3)

    def test_register_shell_output_for_auto_hide_is_additive(self):
        with patch.object(self.agent, "_terminal_columns_for_line_estimate", return_value=80):
            self.agent._register_shell_output_for_auto_hide("line1\n", "")
            self.agent._register_shell_output_for_auto_hide("line2\nline3\n", "")
        self.assertEqual(self.agent._last_shell_output_visible_lines, 3)

    def test_hide_previous_shell_output_clears_exact_count(self):
        fake_stdout = _FakeStdout()
        self.agent._last_shell_output_visible_lines = 2
        with patch("src.agent.sys.stdout", fake_stdout):
            self.agent._hide_previous_shell_output_if_needed()
        self.assertEqual(self.agent._last_shell_output_visible_lines, 0)
        self.assertEqual(fake_stdout.writes.count("\x1b[1A\r\x1b[2K"), 2)

    def test_hide_previous_shell_output_honors_safety_buffer(self):
        fake_stdout = _FakeStdout()
        self.agent._last_shell_output_visible_lines = 2
        with patch("src.agent.sys.stdout", fake_stdout):
            self.agent._hide_previous_shell_output_if_needed(safety_buffer_lines=2)
        self.assertEqual(self.agent._last_shell_output_visible_lines, 0)
        self.assertEqual(fake_stdout.writes.count("\x1b[1A\r\x1b[2K"), 4)

    def test_terminal_columns_for_line_estimate_prefers_input_handler_width(self):
        self.agent.input_handler = _FakeInputHandler()

        class _Sz:
            def __init__(self, columns):
                self.columns = columns

        with patch("src.agent.os.get_terminal_size", side_effect=[_Sz(120)]):
            cols = self.agent._terminal_columns_for_line_estimate()
        self.assertEqual(cols, 40)

    def test_terminal_columns_for_line_estimate_falls_back_to_real_terminal_size(self):
        class _Sz:
            def __init__(self, columns):
                self.columns = columns

        with patch("src.agent.os.get_terminal_size", side_effect=[_Sz(120)]):
            cols = self.agent._terminal_columns_for_line_estimate()
        self.assertEqual(cols, 120)

    def test_terminal_columns_for_line_estimate_ignores_inactive_prompt_toolkit_session_fallback(self):
        self.agent.input_handler = _FakePromptToolkitInputHandler(session=None)

        class _Sz:
            def __init__(self, columns):
                self.columns = columns

        with patch("src.agent.os.get_terminal_size", side_effect=[_Sz(120)]):
            cols = self.agent._terminal_columns_for_line_estimate()
        self.assertEqual(cols, 120)


if __name__ == "__main__":
    unittest.main()

