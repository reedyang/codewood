"""Unit tests for agent/tools/grep_tool.py."""

import tempfile
import unittest
from pathlib import Path

from agent.tools.grep_tool import run_grep


class GrepToolTests(unittest.TestCase):
    def test_run_grep_root_finds_match(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "x.py").write_text("alpha\nbeta gamma\n", encoding="utf-8")
            out = root / "hits.txt"
            r = run_grep(
                root=root,
                files=None,
                output_file=out,
                pattern=r"gamma",
                max_workers=2,
            )
            self.assertTrue(r.get("success"))
            self.assertEqual(r.get("match_count"), 1)
            text = out.read_text(encoding="utf-8")
            self.assertIn("beta gamma", text)
            self.assertIn("\t", text)

    def test_run_grep_explicit_files_no_extension_filter(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            weird = root / "data.noext"
            weird.write_text("needle here\n", encoding="utf-8")
            out = root / "out.txt"
            r = run_grep(
                root=None,
                files=[weird],
                output_file=out,
                pattern="needle",
            )
            self.assertTrue(r.get("success"))
            self.assertEqual(r.get("match_count"), 1)


if __name__ == "__main__":
    unittest.main()
