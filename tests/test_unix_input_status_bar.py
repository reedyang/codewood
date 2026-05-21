import io
import time
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.completion import unix_input
from src.completion.unix_input import TabCompleter


class TabCompleterStatusBarTests(unittest.TestCase):
    def test_get_input_auto_hides_status_bar_when_typing_on_tty(self):
        class _TtyBuffer(io.StringIO):
            def isatty(self):
                return True

        class _FakeReadline:
            def __init__(self):
                self.line_buffer = ""

            def get_line_buffer(self):
                return self.line_buffer

        with tempfile.TemporaryDirectory() as td:
            completer = TabCompleter(Path(td))
            fake_rl = _FakeReadline()

            def _fake_input(_prompt):
                time.sleep(0.05)
                fake_rl.line_buffer = "a"
                time.sleep(0.08)
                return "/help"

            with patch.object(unix_input, "READLINE_AVAILABLE", True), patch.object(
                unix_input, "readline", fake_rl, create=True
            ), patch("builtins.input", side_effect=_fake_input):
                out = _TtyBuffer()
                with redirect_stdout(out):
                    result = completer.get_input_with_completion(
                        prompt="workspace>",
                        status_bar_text="Model: gpt | Ctx: 12%",
                        show_status_bar=True,
                    )

        self.assertEqual(result, "/help")
        rendered = out.getvalue()
        # First clear for drawing status, second clear for hide-on-typing.
        self.assertGreaterEqual(rendered.count("\x1b[2K"), 2)

    def test_get_input_renders_status_below_prompt_on_tty(self):
        class _TtyBuffer(io.StringIO):
            def isatty(self):
                return True

        with tempfile.TemporaryDirectory() as td:
            completer = TabCompleter(Path(td))
            with patch("builtins.input", return_value="/help") as input_mock:
                out = _TtyBuffer()
                with redirect_stdout(out):
                    result = completer.get_input_with_completion(
                        prompt="workspace>",
                        status_bar_text="Model: gpt | Ctx: 12%",
                        show_status_bar=True,
                    )
        self.assertEqual(result, "/help")
        input_mock.assert_called_once_with("")
        rendered = out.getvalue()
        self.assertIn("workspace>", rendered)
        self.assertIn("Model: gpt | Ctx: 12%", rendered)
        self.assertLess(rendered.find("workspace>"), rendered.find("Model: gpt | Ctx: 12%"))
        self.assertIn("\n\n", rendered)
        self.assertIn("\x1b[2B\r", rendered)

    def test_get_input_prints_status_bar_when_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            completer = TabCompleter(Path(td))
            with patch("builtins.input", return_value="/help"):
                out = io.StringIO()
                with redirect_stdout(out):
                    result = completer.get_input_with_completion(
                        prompt="workspace>",
                        status_bar_text="Model: gpt | Ctx: 12%",
                        show_status_bar=True,
                    )
        self.assertEqual(result, "/help")
        rendered = out.getvalue()
        self.assertIn("Model: gpt | Ctx: 12%", rendered)

    def test_get_input_hides_status_bar_when_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            completer = TabCompleter(Path(td))
            with patch("builtins.input", return_value="/help"):
                out = io.StringIO()
                with redirect_stdout(out):
                    result = completer.get_input_with_completion(
                        prompt="workspace>",
                        status_bar_text="Model: gpt | Ctx: 12%",
                        show_status_bar=False,
                    )
        self.assertEqual(result, "/help")
        rendered = out.getvalue()
        self.assertNotIn("Model: gpt | Ctx: 12%", rendered)


if __name__ == "__main__":
    unittest.main()
