import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli.tools.shell import _git_content_before
from cli.tools.shell import _snapshot_workspace_before_content


class _CompletedText:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class GitContentBeforeTests(unittest.TestCase):
    def test_index_version_wins_over_head(self):
        calls = [
            _CompletedText(0, b"index content\n"),
            _CompletedText(1, b"", b"fatal: path does not exist"),
        ]

        with patch("cli.tools.shell._subprocess_mod.run", side_effect=calls) as run_mock:
            content = _git_content_before(Path("D:/repo"), "D:/repo/src/a.py")

        self.assertEqual(content, "index content\n")
        self.assertEqual(run_mock.call_args_list[0].args[0][-1], ":src/a.py")
        self.assertEqual(len(run_mock.call_args_list), 1)

    def test_head_fallback_when_index_misses(self):
        calls = [
            _CompletedText(1, b"", b"fatal: path does not exist"),
            _CompletedText(0, b"head content\n"),
        ]

        with patch("cli.tools.shell._subprocess_mod.run", side_effect=calls):
            content = _git_content_before(Path("D:/repo"), "D:/repo/src/a.py")

        self.assertEqual(content, "head content\n")

    def test_none_when_all_miss(self):
        calls = [
            _CompletedText(1, b"", b"fatal: path does not exist"),
            _CompletedText(1, b"", b"fatal: path does not exist"),
        ]

        with patch("cli.tools.shell._subprocess_mod.run", side_effect=calls):
            content = _git_content_before(Path("D:/repo"), "D:/repo/src/a.py")

        self.assertIsNone(content)

    def test_none_when_path_outside_repo(self):
        content = _git_content_before(Path("D:/repo"), "D:/other/a.py")
        self.assertIsNone(content)


class SnapshotBeforeContentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve()
        self.work = self.repo / "work"
        self.work.mkdir()

    def test_snapshots_untracked_unstaged_and_command_paths(self):
        untracked = self.work / "new.py"
        untracked.write_text("print('new')\n", encoding="utf-8")
        modified = self.work / "mod.py"
        modified.write_text("print('mod')\n", encoding="utf-8")
        clean = self.work / "clean.py"
        clean.write_text("print('clean')\n", encoding="utf-8")

        calls = [
            _CompletedText(0, "work/new.py\0"),
            _CompletedText(0, "work/mod.py\0"),
        ]
        with patch("cli.tools.shell._subprocess_mod.run", side_effect=calls):
            snapshot = _snapshot_workspace_before_content(
                "python clean.py", self.work, self.repo,
            )

        self.assertEqual(snapshot[str(untracked)], "print('new')\n")
        self.assertEqual(snapshot[str(modified)], "print('mod')\n")
        self.assertEqual(snapshot[str(clean)], "print('clean')\n")

    def test_git_failures_still_snapshot_command_paths(self):
        target = self.work / "target.txt"
        target.write_text("hello\n", encoding="utf-8")

        calls = [
            _CompletedText(128, "fatal: not a git repository"),
            _CompletedText(128, "fatal: not a git repository"),
        ]
        with patch("cli.tools.shell._subprocess_mod.run", side_effect=calls):
            snapshot = _snapshot_workspace_before_content(
                "cat target.txt", self.work, self.repo,
            )

        self.assertEqual(snapshot[str(target)], "hello\n")

    def test_non_git_workspace_snapshots_command_paths_only(self):
        target = self.work / "target.txt"
        target.write_text("hello\n", encoding="utf-8")
        other = self.work / "other.txt"
        other.write_text("bye\n", encoding="utf-8")

        snapshot = _snapshot_workspace_before_content(
            "cat target.txt", self.work, None,
        )

        self.assertEqual(snapshot[str(target)], "hello\n")
        self.assertNotIn(str(other), snapshot)

    def test_git_output_without_trailing_nul_is_handled(self):
        untracked = self.work / "new.py"
        untracked.write_text("print('new')\n", encoding="utf-8")

        calls = [
            _CompletedText(0, "work/new.py\0"),
            _CompletedText(0, ""),
        ]
        with patch("cli.tools.shell._subprocess_mod.run", side_effect=calls):
            snapshot = _snapshot_workspace_before_content(
                "true", self.work, self.repo,
            )

        self.assertEqual(snapshot[str(untracked)], "print('new')\n")


if __name__ == "__main__":
    unittest.main()