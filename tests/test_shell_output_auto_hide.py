import sys
import types
import unittest
from unittest.mock import patch


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.smart_shell_agent import SmartShellAgent


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


class ShellOutputAutoHideTests(unittest.TestCase):
    def setUp(self):
        self.agent = SmartShellAgent.__new__(SmartShellAgent)
        self.agent._last_shell_output_visible_lines = 0

    def test_estimate_rendered_line_count_wraps_by_terminal_width(self):
        with patch.object(self.agent, "_terminal_columns_for_line_estimate", return_value=10):
            self.assertEqual(self.agent._estimate_rendered_line_count("12345678901"), 2)
            self.assertEqual(self.agent._estimate_rendered_line_count("a\n\nb"), 3)

    def test_register_shell_output_for_auto_hide_accumulates_stdout_stderr(self):
        with patch.object(self.agent, "_terminal_columns_for_line_estimate", return_value=80):
            self.agent._register_shell_output_for_auto_hide("line1\nline2\n", "err1\n")
        self.assertEqual(self.agent._last_shell_output_visible_lines, 3)

    def test_hide_previous_shell_output_clears_exact_count(self):
        fake_stdout = _FakeStdout()
        self.agent._last_shell_output_visible_lines = 2
        with patch("src.smart_shell_agent.sys.stdout", fake_stdout):
            self.agent._hide_previous_shell_output_if_needed()
        self.assertEqual(self.agent._last_shell_output_visible_lines, 0)
        self.assertEqual(fake_stdout.writes.count("\x1b[1A\r\x1b[2K"), 2)


if __name__ == "__main__":
    unittest.main()
