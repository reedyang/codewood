import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from cli.tools.shell import _git_content_before
from cli.tools.shell import _SNAPSHOT_MAX_FILE_BYTES
from cli.tools.shell import _snapshot_workspace_before_content


class _CompletedText:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class GitContentBeforeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows drive-letter repo path")
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

    @unittest.skipUnless(os.name == "nt", "Windows drive-letter repo path")
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

    def test_directory_expansion_skips_git_and_virtualenvs(self):
        git_dir = self.work / ".git"
        git_dir.mkdir()
        pack = git_dir / "pack.pack"
        pack.write_bytes(b"x" * 2048)
        venv = self.work / ".venv"
        venv.mkdir()
        venv_file = venv / "site.py"
        venv_file.write_text("import os\n", encoding="utf-8")
        normal = self.work / "src"
        normal.mkdir()
        normal_file = normal / "a.py"
        normal_file.write_text("print('a')\n", encoding="utf-8")

        calls = [_CompletedText(0, ""), _CompletedText(0, "")]
        with patch("cli.tools.shell._subprocess_mod.run", side_effect=calls):
            snapshot = _snapshot_workspace_before_content(
                f"git -C {self.work.as_posix()} status", self.work, self.repo,
            )

        self.assertNotIn(str(pack), snapshot)
        self.assertNotIn(str(venv_file), snapshot)
        self.assertIn(str(normal_file), snapshot)
        self.assertEqual(snapshot[str(normal_file)], "print('a')\n")

    def test_oversized_file_skipped(self):
        target = self.work / "big.txt"
        target.write_bytes(b"x" * (_SNAPSHOT_MAX_FILE_BYTES + 1))

        calls = [_CompletedText(0, ""), _CompletedText(0, "")]
        with patch("cli.tools.shell._subprocess_mod.run", side_effect=calls):
            snapshot = _snapshot_workspace_before_content(
                f"cat {(self.work / 'big.txt').as_posix()}", self.work, self.repo,
            )

        self.assertNotIn(str(target), snapshot)
        self.assertEqual(len(snapshot), 0)

    def test_file_count_budget_stops_reading(self):
        for i in range(5):
            f = self.work / f"f{i}.txt"
            f.write_text(f"content {i}\n", encoding="utf-8")

        calls = [_CompletedText(0, ""), _CompletedText(0, "")]
        with patch("cli.tools.shell._subprocess_mod.run", side_effect=calls):
            with patch("cli.tools.shell._SNAPSHOT_MAX_FILES", 2):
                snapshot = _snapshot_workspace_before_content(
                    f"cat {self.work.as_posix()}", self.work, self.repo,
                )

        self.assertEqual(len(snapshot), 2)

    def test_byte_budget_stops_reading(self):
        for i in range(5):
            f = self.work / f"g{i}.txt"
            f.write_text("y" * 1000, encoding="utf-8")

        calls = [_CompletedText(0, ""), _CompletedText(0, "")]
        with patch("cli.tools.shell._subprocess_mod.run", side_effect=calls):
            with patch("cli.tools.shell._SNAPSHOT_MAX_BYTES", 2500):
                snapshot = _snapshot_workspace_before_content(
                    f"cat {self.work.as_posix()}", self.work, self.repo,
                )

        self.assertEqual(len(snapshot), 2)

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