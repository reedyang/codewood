import sys
import unittest
from unittest.mock import patch

from src.core.console_utils import _ansi_blue
from src.core.console_utils import _ansi_green


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


if __name__ == "__main__":
    unittest.main()
