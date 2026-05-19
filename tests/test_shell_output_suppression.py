import unittest

from src.actions.command_actions import (
    SHELL_OUTPUT_DISPLAY_TAIL_LINES,
    _build_tail_output_for_display,
    _count_output_lines,
)


class _FakeTty:
    def isatty(self):
        return True


class _FakePipe:
    def isatty(self):
        return False


class ShellOutputSuppressionTests(unittest.TestCase):
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
        self.assertIn("omitted 50 lines", out)
        self.assertIn("line51", out)
        self.assertIn("line80", out)
        self.assertNotIn("line50\n", out)
        self.assertTrue(out.startswith("\x1b[90;3m"))

    def test_build_tail_output_falls_back_plain_notice_on_non_tty(self):
        text = "\n".join(f"line{i}" for i in range(1, 55)) + "\n"
        out = _build_tail_output_for_display(text, _FakePipe(), SHELL_OUTPUT_DISPLAY_TAIL_LINES)
        self.assertTrue(out.startswith("... omitted 24 lines ...\n"))


if __name__ == "__main__":
    unittest.main()
