"""Tests for ``prepend_bundled_bin_to_path``.

The bundled ``bin/`` directory ships with prebuilt executables (most
notably ``rg``). Putting it at the head of ``PATH`` once at startup
lets every subprocess we spawn — including pipelines and compound
commands the per-command rg head-rewrite can't reach — resolve those
binaries by name. These tests pin down the contract:

* idempotent across repeated calls;
* deduplicates a pre-existing entry (case-insensitive on Windows) and
  promotes the canonical resolved path to position 0;
* leaves ``PATH`` untouched when the bundled directory is missing;
* uses the platform path separator so existing entries are preserved.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config import app_info


class PrependBundledBinToPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_path = os.environ.get("PATH", "")

    def tearDown(self) -> None:
        os.environ["PATH"] = self._saved_path

    def test_prepend_inserts_resolved_bin_at_head_when_missing(self):
        fake_bin = Path("D:/fake/project/bin")
        os.environ["PATH"] = os.pathsep.join(["C:\\foo", "C:\\bar"])
        with patch.object(app_info, "get_app_bundled_bin_dir", return_value=fake_bin), \
             patch.object(Path, "is_dir", return_value=True), \
             patch.object(Path, "resolve", return_value=fake_bin):
            returned = app_info.prepend_bundled_bin_to_path()
        entries = os.environ["PATH"].split(os.pathsep)
        self.assertEqual(returned, str(fake_bin))
        self.assertEqual(entries[0], str(fake_bin))
        self.assertIn("C:\\foo", entries)
        self.assertIn("C:\\bar", entries)

    def test_prepend_is_idempotent(self):
        fake_bin = Path("D:/fake/project/bin")
        os.environ["PATH"] = "C:\\foo"
        with patch.object(app_info, "get_app_bundled_bin_dir", return_value=fake_bin), \
             patch.object(Path, "is_dir", return_value=True), \
             patch.object(Path, "resolve", return_value=fake_bin):
            for _ in range(5):
                app_info.prepend_bundled_bin_to_path()
        entries = os.environ["PATH"].split(os.pathsep)
        self.assertEqual(entries.count(str(fake_bin)), 1)
        self.assertEqual(entries[0], str(fake_bin))

    def test_prepend_deduplicates_existing_entry_case_insensitively_on_windows(self):
        if os.name != "nt":
            self.skipTest("Case-insensitive dedup only applies on Windows")
        fake_bin = Path("D:/fake/project/bin")
        # Already on PATH but with different casing and not at head.
        os.environ["PATH"] = os.pathsep.join([
            "C:\\foo",
            str(fake_bin).upper(),
            "C:\\bar",
        ])
        with patch.object(app_info, "get_app_bundled_bin_dir", return_value=fake_bin), \
             patch.object(Path, "is_dir", return_value=True), \
             patch.object(Path, "resolve", return_value=fake_bin):
            app_info.prepend_bundled_bin_to_path()
        entries = os.environ["PATH"].split(os.pathsep)
        self.assertEqual(entries[0], str(fake_bin))
        # The old upper-case duplicate must be gone — exactly one
        # bundled-bin entry remains, at position 0.
        normalized = [e.casefold() for e in entries]
        self.assertEqual(normalized.count(str(fake_bin).casefold()), 1)
        self.assertIn("C:\\foo", entries)
        self.assertIn("C:\\bar", entries)

    def test_prepend_no_op_when_bundled_bin_dir_does_not_exist(self):
        fake_bin = Path("D:/fake/project/bin")
        os.environ["PATH"] = "C:\\foo"
        with patch.object(app_info, "get_app_bundled_bin_dir", return_value=fake_bin), \
             patch.object(Path, "is_dir", return_value=False):
            returned = app_info.prepend_bundled_bin_to_path()
        self.assertEqual(returned, "")
        self.assertEqual(os.environ["PATH"], "C:\\foo")

    def test_prepend_returns_actual_bin_dir_when_real_repo_layout_is_present(self):
        # The repo this test runs from ships ``bin/rg.exe`` (or
        # equivalent on Linux). When the helper resolves successfully,
        # the returned path must point inside the actual project
        # ``bin/`` directory.
        bundled = app_info.get_app_bundled_bin_dir()
        if not bundled.is_dir():
            self.skipTest("Bundled bin/ directory missing in this checkout")
        os.environ["PATH"] = "C:\\foo"
        returned = app_info.prepend_bundled_bin_to_path()
        self.assertTrue(returned)
        self.assertEqual(Path(returned).name.lower(), "bin")
        self.assertEqual(os.environ["PATH"].split(os.pathsep)[0], returned)


class AppendWindowsGitToolsToPathTests(unittest.TestCase):
    """Contract for ``append_windows_git_tools_to_path``.

    The helper appends well-known Git-for-Windows directories
    (``C:\\Program Files\\Git`` and ``C:\\Program Files\\Git\\usr\\bin``)
    to the **tail** of PATH when they exist, so the model can reach
    the GNU userland (bash, grep, sed, …) without shadowing Windows
    defaults like ``find.exe``/``sort.exe`` under System32.

    On non-Windows platforms the helper is a strict no-op, regardless
    of whether such directories happen to exist.
    """

    def setUp(self) -> None:
        self._saved_path = os.environ.get("PATH", "")

    def tearDown(self) -> None:
        os.environ["PATH"] = self._saved_path

    def test_no_op_on_non_windows(self):
        os.environ["PATH"] = "/usr/bin"
        with patch.object(app_info.os, "name", "posix"):
            result = app_info.append_windows_git_tools_to_path()
        self.assertEqual(result, [])
        self.assertEqual(os.environ["PATH"], "/usr/bin")

    def test_skips_missing_candidates_silently(self):
        os.environ["PATH"] = r"C:\Windows\System32"
        # Force is_dir to return False for every candidate.
        with patch.object(app_info.os, "name", "nt"), \
             patch.object(Path, "is_dir", return_value=False):
            result = app_info.append_windows_git_tools_to_path()
        self.assertEqual(result, [])
        self.assertEqual(os.environ["PATH"], r"C:\Windows\System32")

    def test_appends_candidates_at_tail_when_present(self):
        os.environ["PATH"] = r"C:\Windows\System32"
        with patch.object(app_info.os, "name", "nt"), \
             patch.object(Path, "is_dir", return_value=True):
            result = app_info.append_windows_git_tools_to_path()
        entries = os.environ["PATH"].split(os.pathsep)
        self.assertEqual(entries[0], r"C:\Windows\System32")  # System32 still wins.
        self.assertEqual(entries[-2], r"C:\Program Files\Git")
        self.assertEqual(entries[-1], r"C:\Program Files\Git\usr\bin")
        self.assertEqual(
            result,
            [r"C:\Program Files\Git", r"C:\Program Files\Git\usr\bin"],
        )

    def test_idempotent_across_repeated_calls(self):
        os.environ["PATH"] = r"C:\Windows\System32"
        with patch.object(app_info.os, "name", "nt"), \
             patch.object(Path, "is_dir", return_value=True):
            for _ in range(5):
                app_info.append_windows_git_tools_to_path()
        entries = os.environ["PATH"].split(os.pathsep)
        # Each Git directory must appear exactly once.
        normalized = [e.casefold() for e in entries]
        self.assertEqual(
            normalized.count(r"C:\Program Files\Git".casefold()),
            1,
        )
        self.assertEqual(
            normalized.count(r"C:\Program Files\Git\usr\bin".casefold()),
            1,
        )

    def test_does_not_duplicate_when_already_present_with_different_casing(self):
        # Pre-seed PATH with the Git directories in different casing
        # to mimic a shell that already exported them; the helper must
        # detect the case-insensitive match and leave PATH alone.
        seeded = os.pathsep.join([
            r"C:\Windows\System32",
            r"c:\program files\git",
            r"C:\PROGRAM FILES\GIT\USR\BIN",
        ])
        os.environ["PATH"] = seeded
        with patch.object(app_info.os, "name", "nt"), \
             patch.object(Path, "is_dir", return_value=True):
            app_info.append_windows_git_tools_to_path()
        # PATH should be unchanged byte-for-byte: nothing was appended,
        # and existing entries keep their original casing.
        self.assertEqual(os.environ["PATH"], seeded)

    def test_only_appends_existing_subset_of_candidates(self):
        os.environ["PATH"] = r"C:\Windows\System32"

        # Pretend only ``C:\Program Files\Git\usr\bin`` exists.
        def _fake_is_dir(self):  # type: ignore[no-redef]
            return str(self).casefold() == r"C:\Program Files\Git\usr\bin".casefold()

        with patch.object(app_info.os, "name", "nt"), \
             patch.object(Path, "is_dir", _fake_is_dir):
            result = app_info.append_windows_git_tools_to_path()
        entries = os.environ["PATH"].split(os.pathsep)
        self.assertEqual(result, [r"C:\Program Files\Git\usr\bin"])
        self.assertNotIn(r"C:\Program Files\Git", entries)
        self.assertEqual(entries[-1], r"C:\Program Files\Git\usr\bin")


if __name__ == "__main__":
    unittest.main()
