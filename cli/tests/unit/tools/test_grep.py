import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli.tools.grep import action_grep


class _FakeAgent:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root


class GrepToolPathHandlingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "AppContext.tsx").write_text(
            "const x = 1;\nconst draft = 2;\n", encoding="utf-8"
        )
        (self.root / "src" / "Other.ts").write_text("no match\n", encoding="utf-8")
        # action_grep resolves paths; on Windows resolve() may yield 8.3 short
        # names, so normalize the expected directory the same way.
        self.src_dir = (self.root / "src").resolve()
        self.agent = _FakeAgent(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, pattern: str, path: str = ".", include: str = "") -> tuple:
        with patch("cli.tools.grep._find_rg", return_value=Path("rg")):
            with patch("cli.tools.grep.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = ""
                mock_run.return_value.stderr = ""
                result = action_grep(
                    self.agent, pattern, path=path, include=include
                )
                return result, mock_run

    def test_file_path_uses_parent_dir_and_filename_glob(self):
        # Regression: passing a FILE as ``path`` used to run rg with the file as
        # its cwd/search root, which fails on Windows ("directory name is
        # invalid"). It must search the parent directory with a filename glob.
        result, mock_run = self._run("draft", path="src/AppContext.tsx")
        self.assertTrue(result["success"])
        args = mock_run.call_args.args[0]
        self.assertIn("--glob", args)
        self.assertIn("AppContext.tsx", args)
        self.assertNotIn("Other.ts", args)
        # Search root and cwd are the parent directory, not the file.
        self.assertEqual(Path(args[-1]).resolve(), self.src_dir)
        self.assertEqual(Path(mock_run.call_args.kwargs["cwd"]).resolve(), self.src_dir)

    def test_absolute_file_path_is_normalized(self):
        result, mock_run = self._run(
            "draft", path=str(self.root / "src" / "AppContext.tsx")
        )
        self.assertTrue(result["success"])
        args = mock_run.call_args.args[0]
        self.assertIn("AppContext.tsx", args)
        self.assertEqual(Path(args[-1]).resolve(), self.src_dir)

    def test_include_plus_file_path_keeps_both_globs(self):
        result, mock_run = self._run(
            "draft", path="src/AppContext.tsx", include="*.tsx"
        )
        self.assertTrue(result["success"])
        args = mock_run.call_args.args[0]
        globs = [args[i + 1] for i, a in enumerate(args) if a == "--glob"]
        self.assertEqual(globs, ["*.tsx", "AppContext.tsx"])

    def test_directory_path_unchanged(self):
        result, mock_run = self._run("draft", path="src")
        self.assertTrue(result["success"])
        args = mock_run.call_args.args[0]
        self.assertNotIn("--glob", args)
        self.assertEqual(Path(args[-1]).resolve(), self.src_dir)

    def test_directory_path_with_include_uses_single_glob(self):
        result, mock_run = self._run("draft", path="src", include="*.tsx")
        self.assertTrue(result["success"])
        args = mock_run.call_args.args[0]
        globs = [args[i + 1] for i, a in enumerate(args) if a == "--glob"]
        self.assertEqual(globs, ["*.tsx"])

    def test_missing_path_reports_directory_not_found(self):
        result, _ = self._run("draft", path="does/not/exist")
        self.assertFalse(result["success"])
        self.assertIn("Directory not found", result["error"])


if __name__ == "__main__":
    unittest.main()
