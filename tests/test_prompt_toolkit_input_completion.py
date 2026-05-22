import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.completion import prompt_toolkit_input as pti
from src.completion.prompt_toolkit_input import FileCompleter


class _Doc:
    def __init__(self, text: str):
        self.text_before_cursor = text


class PromptToolkitInputCompletionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
