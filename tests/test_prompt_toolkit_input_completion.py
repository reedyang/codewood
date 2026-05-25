import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.completion import prompt_toolkit_input as pti
from src.completion.prompt_toolkit_input import FileCompleter


class _Doc:
    def __init__(self, text: str):
        self.text_before_cursor = text


class _FakeSession:
    def __init__(self, returned_text: str):
        self.returned_text = returned_text
        self.calls = []

    def prompt(self, prompt_line: str, **kwargs):
        self.calls.append((prompt_line, kwargs))
        return self.returned_text


class PromptToolkitInputCompletionTests(unittest.TestCase):
    def test_windows_shift_state_detector_returns_false_on_non_windows(self):
        with patch.object(pti.os, "name", "posix"):
            self.assertFalse(pti._is_windows_shift_pressed())

    def test_windows_shift_state_detector_uses_native_key_state(self):
        with (
            patch.object(pti.os, "name", "nt"),
            patch.object(pti, "_windows_get_async_key_state", return_value=0),
            patch.object(pti, "_windows_get_key_state", side_effect=[0, 0x8000, 0]),
        ):
            self.assertTrue(pti._is_windows_shift_pressed())

    def test_windows_shift_state_detector_returns_false_when_no_shift_down(self):
        with (
            patch.object(pti.os, "name", "nt"),
            patch.object(pti, "_windows_get_async_key_state", return_value=0),
            patch.object(pti, "_windows_get_key_state", return_value=0),
        ):
            self.assertFalse(pti._is_windows_shift_pressed())

    def test_shift_enter_aliases_include_common_terminal_sequences(self):
        aliases = set(pti.SHIFT_ENTER_KEY_ALIASES)
        self.assertIn(("escape", "[", "1", "3", ";", "2", "u"), aliases)
        self.assertIn(("escape", "[", "2", "7", ";", "2", ";", "1", "3", "~"), aliases)
        self.assertIn(("escape", "enter"), aliases)
        self.assertIn(("escape", "O", "M"), aliases)

    def test_lone_slash_on_posix_uses_slash_commands_not_root_files(self):
        with tempfile.TemporaryDirectory() as td:
            completer = FileCompleter(Path(td))
            with patch.object(completer, "_get_path_completions", side_effect=AssertionError("path completion should not run for lone slash")):
                with patch.object(pti.os, "name", "posix"):
                    out = list(completer.get_completions(_Doc("/"), None))
        self.assertTrue(len(out) > 0)
        self.assertTrue(any(str(getattr(c, "text", "")).startswith("/") for c in out))

    def test_posix_absolute_path_still_uses_path_completion(self):
        with tempfile.TemporaryDirectory() as td:
            completer = FileCompleter(Path(td))
            with patch.object(completer, "_get_path_completions", return_value=["/tmp/demo.txt"]) as mocked:
                with patch.object(pti.os, "name", "posix"):
                    out = list(completer.get_completions(_Doc("/tmp/de"), None))
        self.assertTrue(mocked.called)
        self.assertTrue(any(getattr(c, "text", "") == "/tmp/demo.txt" for c in out))

    def test_local_completion_prefers_workspace_directory(self):
        with tempfile.TemporaryDirectory() as wd, tempfile.TemporaryDirectory() as ws:
            wd_path = Path(wd)
            ws_path = Path(ws)
            (wd_path / "from_work_dir.txt").write_text("x", encoding="utf-8")
            (ws_path / "from_workspace.txt").write_text("x", encoding="utf-8")
            completer = FileCompleter(wd_path, workspace_directory=ws_path)
            out = completer._get_local_completions("from_")
        self.assertIn("from_workspace.txt", out)
        self.assertNotIn("from_work_dir.txt", out)

    def test_get_input_uses_multiline_prompt_and_two_space_continuation(self):
        handler = pti.PromptToolkitInputHandler.__new__(pti.PromptToolkitInputHandler)
        handler.session = _FakeSession("line1\nline2")
        handler.history = []
        handler.work_directory = Path.cwd()
        handler._status_bar_text = ""
        handler._status_bar_fragments = []
        handler._status_bar_enabled = True

        out = handler.get_input_with_completion("› ")
        self.assertEqual(out, "line1\nline2")
        self.assertEqual(handler.history, ["line1\nline2"])

        _, kwargs = handler.session.calls[0]
        self.assertTrue(bool(kwargs.get("multiline", False)))
        continuation = kwargs.get("prompt_continuation")
        self.assertTrue(callable(continuation))
        self.assertEqual(continuation(2, 1, True), "  ")

    def test_get_input_normalizes_mixed_newlines(self):
        handler = pti.PromptToolkitInputHandler.__new__(pti.PromptToolkitInputHandler)
        handler.session = _FakeSession("a\r\nb\rc")
        handler.history = []
        handler.work_directory = Path.cwd()
        handler._status_bar_text = ""
        handler._status_bar_fragments = []
        handler._status_bar_enabled = True

        out = handler.get_input_with_completion("› ")
        self.assertEqual(out, "a\nb\nc")
        self.assertEqual(handler.history[-1], "a\nb\nc")


if __name__ == "__main__":
    unittest.main()
