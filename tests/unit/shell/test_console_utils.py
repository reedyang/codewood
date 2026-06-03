import sys
import unittest
from unittest.mock import patch

from src.core.console_utils import _ansi_blue
from src.core.console_utils import _ansi_green
from src.core.console_utils import _format_elapsed_minutes_seconds
from src.core.console_utils import _render_working_status_line


class ConsoleUtilsTests(unittest.TestCase):
    def test_ansi_blue_emits_escape_sequence_when_color_enabled(self):
        class DummyStdout:
            def isatty(self):
                return True

        with patch.dict("src.core.console_utils.os.environ", {"FORCE_COLOR": "1"}, clear=False), patch(
            "src.core.console_utils._enable_windows_console_vt"
        ), patch.object(sys, "stdout", DummyStdout()):
            out = _ansi_blue("hello")

        self.assertEqual(out, "\033[34mhello\033[0m")

    def test_ansi_green_uses_screenshot_like_rgb(self):
        class DummyStdout:
            def isatty(self):
                return True

        with patch.dict("src.core.console_utils.os.environ", {"FORCE_COLOR": "1"}, clear=False), patch(
            "src.core.console_utils._enable_windows_console_vt"
        ), patch.object(sys, "stdout", DummyStdout()):
            out = _ansi_green("hello")

        self.assertEqual(out, "\033[38;2;152;195;121mhello\033[0m")

    def test_format_elapsed_minutes_seconds(self):
        self.assertEqual(_format_elapsed_minutes_seconds(65), "1m 5s")
        self.assertEqual(_format_elapsed_minutes_seconds(0), "0s")
        self.assertEqual(_format_elapsed_minutes_seconds(59), "59s")
        self.assertEqual(_format_elapsed_minutes_seconds(60), "1m 0s")

    def test_render_working_status_line_marquee_progression(self):
        class DummyStdout:
            def isatty(self):
                return True

        with patch.dict("src.core.console_utils.os.environ", {"FORCE_COLOR": "1"}, clear=False), patch(
            "src.core.console_utils._enable_windows_console_vt"
        ), patch.object(sys, "stdout", DummyStdout()):
            frame0 = _render_working_status_line(65, frame=0)
            frame1 = _render_working_status_line(65, frame=1)
            frame2 = _render_working_status_line(65, frame=2)
            frame3 = _render_working_status_line(65, frame=3)

        self.assertIn("(1m 5s • esc to interrupt)", frame0)
        self.assertIn("\033[90m• \033[0mW\033[90morking... (1m 5s • esc to interrupt)\033[0m", frame0)
        self.assertIn("\033[90m• \033[0mWo\033[90mrking... (1m 5s • esc to interrupt)\033[0m", frame1)
        self.assertIn("\033[90m• \033[0mWor\033[90mking... (1m 5s • esc to interrupt)\033[0m", frame2)
        self.assertIn("\033[90m• W\033[0mork\033[90ming... (1m 5s • esc to interrupt)\033[0m", frame3)

    def test_render_working_status_line_uses_language_specific_labels(self):
        class DummyStdout:
            def isatty(self):
                return True

        with patch.dict("src.core.console_utils.os.environ", {"FORCE_COLOR": "1"}, clear=False), patch(
            "src.core.console_utils._enable_windows_console_vt"
        ), patch.object(sys, "stdout", DummyStdout()):
            line = _render_working_status_line(65, frame=0, language="zh-CN")

        self.assertIn("作中... (1m 5s • 按 Esc 中断)", line)
        self.assertIn("按 Esc 中断", line)


if __name__ == "__main__":
    unittest.main()
