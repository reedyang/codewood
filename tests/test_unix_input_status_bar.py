import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.completion.unix_input import TabCompleter


class TabCompleterStatusBarTests(unittest.TestCase):
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
