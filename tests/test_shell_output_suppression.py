import unittest
from unittest.mock import patch

from src.actions.command_actions import (
    SHELL_OUTPUT_DISPLAY_TAIL_LINES,
    _build_tail_output_for_display,
    _count_output_lines,
    _dynamic_tail_line_limit,
)


class _FakeTty:
    def isatty(self):
        return True


class _FakePipe:
    def isatty(self):
        return False


class ShellOutputSuppressionTests(unittest.TestCase):
    def test_dynamic_tail_line_limit_caps_at_30(self):
        with patch("src.actions.command_actions._terminal_rows_for_tail_display", return_value=100):
            self.assertEqual(_dynamic_tail_line_limit(_FakePipe()), 30)

    def test_dynamic_tail_line_limit_uses_terminal_height(self):
        with patch("src.actions.command_actions._terminal_rows_for_tail_display", return_value=20):
            self.assertEqual(_dynamic_tail_line_limit(_FakePipe()), 20)

    def test_count_output_lines_handles_crlf(self):
        self.assertEqual(_count_output_lines("a\r\nb\r\n"), 2)
        self.assertEqual(_count_output_lines("a\nb\nc"), 3)
        self.assertEqual(_count_output_lines(""), 0)

    def test_build_tail_output_keeps_full_output_when_not_exceeding_limit(self):
        text = "line1\nline2\n"
        out = _build_tail_output_for_display(text, _FakeTty(), SHELL_OUTPUT_DISPLAY_TAIL_LINES)
        self.assertEqual(out, text)

    def test_build_tail_output_uses_notice_and_last_50_lines(self):
        text = "\n".join(f"line{i}" for i in range(1, 81)) + "\n"
        out = _build_tail_output_for_display(text, _FakeTty(), SHELL_OUTPUT_DISPLAY_TAIL_LINES)
        self.assertIn("omitted 51 lines", out)
        self.assertIn("line52", out)
        self.assertIn("line80", out)
        self.assertNotIn("line51\n", out)
        self.assertTrue(out.startswith("\x1b[90;3m"))

    def test_build_tail_output_falls_back_plain_notice_on_non_tty(self):
        text = "\n".join(f"line{i}" for i in range(1, 55)) + "\n"
        out = _build_tail_output_for_display(text, _FakePipe(), SHELL_OUTPUT_DISPLAY_TAIL_LINES)
        self.assertTrue(out.startswith("... omitted 25 lines ...\n"))

    def test_build_tail_output_keeps_display_within_visual_line_limit(self):
        text = "x" * 120 + "\n"
        with patch("src.actions.command_actions._terminal_columns_for_tail_display", return_value=10):
            out = _build_tail_output_for_display(text, _FakePipe(), 5)
        self.assertIn("omitted 1 lines", out)
        self.assertTrue(out.endswith(("x" * 10) + "\n" + ("x" * 10) + "\n"))

    def test_build_tail_output_omitted_count_uses_real_lines_not_wrapped_visual_lines(self):
        text = "\n".join(("a" * 18) for _ in range(4)) + "\n"
        with patch("src.actions.command_actions._terminal_columns_for_tail_display", return_value=80):
            out = _build_tail_output_for_display(text, _FakePipe(), 3)
        self.assertIn("omitted 2 lines", out)
        self.assertNotIn("omitted 5 lines", out)

    def test_build_tail_output_accounts_for_cjk_visual_width(self):
        text = ("中" * 40) + "\n"
        with patch("src.actions.command_actions._terminal_columns_for_tail_display", return_value=30):
            out = _build_tail_output_for_display(text, _FakePipe(), 2)
        self.assertIn("omitted 1 lines", out)
        self.assertTrue(out.endswith(("中" * 10) + "\n"))


if __name__ == "__main__":
    unittest.main()
