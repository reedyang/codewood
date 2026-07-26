import unittest
from pathlib import Path
from unittest.mock import patch

from cli.tools.shell import _git_stash_push
from cli.tools.shell import _git_stash_ref_for_hash
from cli.tools.shell import _git_stash_restore


class _CompletedText:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class GitStashGuardTests(unittest.TestCase):
    def test_git_stash_push_returns_none_when_no_new_stash_created(self):
        calls = [
            _CompletedText(0, "existing-stash-hash\n"),
            _CompletedText(0, "No local changes to save\n"),
        ]

        with patch("cli.tools.shell._subprocess_mod.run", side_effect=calls):
            stash_hash = _git_stash_push(Path("D:/repo"))

        self.assertIsNone(stash_hash)

    def test_git_stash_push_returns_none_when_top_stash_does_not_change(self):
        calls = [
            _CompletedText(0, "existing-stash-hash\n"),
            _CompletedText(0, "Saved working directory and index state WIP on master: codewood_shell_pre\n"),
            _CompletedText(0, "existing-stash-hash\n"),
        ]

        with patch("cli.tools.shell._subprocess_mod.run", side_effect=calls):
            stash_hash = _git_stash_push(Path("D:/repo"))

        self.assertIsNone(stash_hash)

    def test_git_stash_push_returns_new_top_stash_hash(self):
        calls = [
            _CompletedText(0, "older-stash-hash\n"),
            _CompletedText(0, "Saved working directory and index state WIP on master: codewood_shell_pre\n"),
            _CompletedText(0, "new-stash-hash\n"),
        ]

        with patch("cli.tools.shell._subprocess_mod.run", side_effect=calls):
            stash_hash = _git_stash_push(Path("D:/repo"))

        self.assertEqual(stash_hash, "new-stash-hash")

    def test_git_stash_ref_for_hash_returns_matching_stash_ref(self):
        calls = [
            _CompletedText(0, "abc123 stash@{1}\ndef456 stash@{0}\n"),
        ]

        with patch("cli.tools.shell._subprocess_mod.run", side_effect=calls):
            stash_ref = _git_stash_ref_for_hash(Path("D:/repo"), "def456")

        self.assertEqual(stash_ref, "stash@{0}")

    def test_git_stash_restore_drops_matching_stash_ref(self):
        calls = [
            _CompletedText(0, "abc123 stash@{1}\ndef456 stash@{0}\n"),
            _CompletedText(0, "Dropped stash@{0}\n"),
        ]

        with patch("cli.tools.shell._subprocess_mod.run", side_effect=calls) as run_mock:
            _git_stash_restore(Path("D:/repo"), "def456")

        self.assertEqual(run_mock.call_args_list[1].args[0][-1], "stash@{0}")

    def test_git_stash_restore_skips_drop_when_hash_not_found(self):
        calls = [
            _CompletedText(0, "abc123 stash@{0}\n"),
        ]

        with patch("cli.tools.shell._subprocess_mod.run", side_effect=calls) as run_mock:
            _git_stash_restore(Path("D:/repo"), "missing")

        self.assertEqual(len(run_mock.call_args_list), 1)


if __name__ == "__main__":
    unittest.main()
