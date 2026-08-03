"""Tests for rename-pair detection in shell file-change monitoring."""

import tempfile
import unittest
from pathlib import Path

from cli.tools.shell import _detect_rename_pairs
from cli.tools.shell import _normalize_line_endings


class NormalizeLineEndingsTests(unittest.TestCase):
    def test_crlf_and_cr_are_normalized(self):
        self.assertEqual(
            _normalize_line_endings("a\r\nb\rc\nd"),
            "a\nb\nc\nd",
        )

    def test_empty_and_none_inputs(self):
        self.assertEqual(_normalize_line_endings(""), "")
        self.assertEqual(_normalize_line_endings(None), "")


class DetectRenamePairsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cwd = Path(self._tmp.name).resolve()

    def _path(self, name: str) -> str:
        return str(self.cwd / name)

    def _write(self, name: str, content: str) -> str:
        p = self.cwd / name
        p.write_text(content, encoding="utf-8")
        return str(p)

    def test_pairs_deleted_and_new_file_with_matching_content(self):
        old = self._write("helloworld.py", "line1\nline2\nline3\n")
        new = self._write("alice_wonderland.py", "line1\nline2\nline3\n")

        pairs = _detect_rename_pairs(
            [new], [old], {old: "line1\nline2\nline3\n"},
            None, {new}, {},
        )

        self.assertEqual(pairs, [(old, new)])

    def test_content_mismatch_is_not_a_rename(self):
        old = self._write("a.py", "aaa\n")
        new = self._write("b.py", "bbb\n")

        pairs = _detect_rename_pairs(
            [new], [old], {old: "aaa\n"}, None, {new}, {},
        )

        self.assertEqual(pairs, [])

    def test_crlf_difference_still_pairs(self):
        # The before-content snapshot carries CRLF while the on-disk file is
        # LF (or vice versa) — line endings must not break rename pairing.
        old = self._write("a.py", "aaa\nbbb\n")
        new = self._write("b.py", "aaa\nbbb\n")

        pairs = _detect_rename_pairs(
            [new], [old], {old: "aaa\r\nbbb\r\n"}, None, {new}, {},
        )

        self.assertEqual(pairs, [(old, new)])

    def test_new_path_not_in_cmd_paths_is_not_paired(self):
        # The command did not explicitly reference the new file, so the
        # deletion must NOT be reclassified as a rename (avoids attributing
        # side-effect renames performed by other processes).
        old = self._write("a.py", "same\n")
        new = self._write("b.py", "same\n")

        pairs = _detect_rename_pairs(
            [new], [old], {old: "same\n"}, None, set(), {},
        )

        self.assertEqual(pairs, [])

    def test_old_path_captured_by_delete_parser_is_not_paired(self):
        # ``rm a.py`` deletes the file through the dedicated delete-target
        # path; an identical new file is a genuine create, not a rename.
        old = self._write("a.py", "same\n")
        new = self._write("b.py", "same\n")

        pairs = _detect_rename_pairs(
            [new], [old], {old: "same\n"}, None, {new}, {old: "same\n"},
        )

        self.assertEqual(pairs, [])

    def test_empty_deleted_content_is_not_paired(self):
        old = self._write("a.py", "")
        new = self._write("b.py", "")

        pairs = _detect_rename_pairs(
            [new], [old], {old: ""}, None, {new}, {},
        )

        self.assertEqual(pairs, [])

    def test_git_fallback_used_when_snapshot_misses(self):
        old = self._path("a.py")
        new = self._write("b.py", "from git\n")

        def fake_git_content_before(repo_root, path):
            return "from git\n"

        from unittest.mock import patch

        with patch("cli.tools.shell._git_content_before", fake_git_content_before):
            pairs = _detect_rename_pairs(
                [new], [old], {}, Path("D:/repo"), {new}, {},
            )

        self.assertEqual(pairs, [(old, new)])


if __name__ == "__main__":
    unittest.main()
