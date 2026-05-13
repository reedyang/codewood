import sys
import tempfile
import types
import unittest
import re
from pathlib import Path


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.smart_shell_agent import SmartShellAgent


class TextEditAndPatchToolTests(unittest.TestCase):
    _ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

    @classmethod
    def _strip_ansi(cls, text: str) -> str:
        return cls._ANSI_RE.sub("", text or "")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        self.agent = SmartShellAgent.__new__(SmartShellAgent)
        self.agent.work_directory = self.root
        self.agent.ai_workspace_dir = self.root / "workspace"
        self.agent.ai_workspace_temp_dir = self.agent.ai_workspace_dir / "temp"
        self.agent.config_dir = self.root / ".smartshell"
        self.agent._self_repo_root = self.root / "repo-protected"
        self.agent._ai_created_path_keys = set()
        self.agent.skills = []
        self.agent._reload_skills_if_workspace_skill_changed = lambda paths: None

        self.agent.ai_workspace_dir.mkdir(parents=True, exist_ok=True)
        self.agent.ai_workspace_temp_dir.mkdir(parents=True, exist_ok=True)
        self.agent.config_dir.mkdir(parents=True, exist_ok=True)
        self.agent._self_repo_root.mkdir(parents=True, exist_ok=True)

    def test_edit_text_insert_success(self):
        target = self.root / "sample.txt"
        target.write_text("a\nb\nc\n", encoding="utf-8")

        result = self.agent.action_edit_text_file(
            file_path=str(target),
            start_line=2,
            line_span=0,
            operation="insert",
            content="x\ny",
            confirmed=True,
        )

        self.assertTrue(result.get("success"), result)
        self.assertEqual(target.read_text(encoding="utf-8"), "a\nx\ny\nb\nc\n")

    def test_edit_text_delete_success(self):
        target = self.root / "sample.txt"
        target.write_text("1\n2\n3\n4\n", encoding="utf-8")

        result = self.agent.action_edit_text_file(
            file_path=str(target),
            start_line=2,
            line_span=2,
            operation="delete",
            confirmed=True,
        )

        self.assertTrue(result.get("success"), result)
        self.assertEqual(target.read_text(encoding="utf-8"), "1\n4\n")

    def test_edit_text_replace_success(self):
        target = self.root / "sample.txt"
        target.write_text("k1\nk2\nk3\n", encoding="utf-8")

        result = self.agent.action_edit_text_file(
            file_path=str(target),
            start_line=2,
            line_span=1,
            operation="replace",
            content="R2\nR3",
            confirmed=True,
        )

        self.assertTrue(result.get("success"), result)
        self.assertEqual(target.read_text(encoding="utf-8"), "k1\nR2\nR3\nk3\n")
        preview = result.get("change_preview") or []
        plain_preview = [self._strip_ansi(str(x)) for x in preview]
        self.assertTrue(any(x.startswith("- ") for x in plain_preview))
        self.assertTrue(any("││ + " in x for x in plain_preview))

    def test_edit_text_delete_with_line_span_zero_defaults_to_one_line(self):
        target = self.root / "sample.txt"
        target.write_text("L1\nL2\nL3\n", encoding="utf-8")

        result = self.agent.action_edit_text_file(
            file_path=str(target),
            start_line=2,
            line_span=0,
            operation="delete",
            confirmed=True,
        )

        self.assertTrue(result.get("success"), result)
        self.assertEqual(result.get("line_span"), 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "L1\nL3\n")

    def test_edit_text_replace_fails_when_start_line_out_of_range(self):
        target = self.root / "sample.txt"
        target.write_text("a\nb\n", encoding="utf-8")
        original = target.read_text(encoding="utf-8")

        result = self.agent.action_edit_text_file(
            file_path=str(target),
            start_line=10,
            line_span=1,
            operation="replace",
            content="X",
            confirmed=True,
        )

        self.assertFalse(result.get("success"), result)
        self.assertIn("起始行超出范围", str(result.get("error", "")))
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_apply_patch_success(self):
        target = self.root / "sample.txt"
        target.write_text("A\nB\nC\n", encoding="utf-8")
        patch = "@@ -1,3 +1,3 @@\n A\n-B\n+BX\n C"

        result = self.agent.action_apply_unified_patch(
            file_path=str(target),
            patch=patch,
            confirmed=True,
        )

        self.assertTrue(result.get("success"), result)
        self.assertEqual(target.read_text(encoding="utf-8"), "A\nBX\nC\n")
        preview = result.get("change_preview") or []
        plain_preview = [self._strip_ansi(str(x)) for x in preview]
        self.assertTrue(any(x.startswith("= ") for x in plain_preview))
        self.assertTrue(any(x.startswith("- ") for x in plain_preview))
        self.assertTrue(any("││ + " in x for x in plain_preview))
        self.assertTrue(any("\x1b[41m" in str(x) for x in preview))
        self.assertTrue(any("\x1b[42m" in str(x) for x in preview))
        first_pipes = [x.find("│") for x in plain_preview if "│" in x]
        self.assertTrue(len(first_pipes) > 0)
        self.assertTrue(all(p == first_pipes[0] for p in first_pipes))
        pipes = [x.find("││") for x in plain_preview if "││" in x]
        self.assertTrue(len(pipes) > 0)
        self.assertTrue(all(p == pipes[0] for p in pipes))

    def test_apply_patch_failure_when_context_mismatch(self):
        target = self.root / "sample.txt"
        target.write_text("A\nB\nC\n", encoding="utf-8")
        patch = "@@ -1,3 +1,3 @@\n A\n-Z\n+BX\n C"

        result = self.agent.action_apply_unified_patch(
            file_path=str(target),
            patch=patch,
            confirmed=True,
        )

        self.assertFalse(result.get("success"), result)
        self.assertIn("不匹配", str(result.get("error", "")))
        self.assertEqual(target.read_text(encoding="utf-8"), "A\nB\nC\n")

    def test_apply_patch_relaxed_header_atat_is_supported(self):
        target = self.root / "sample.txt"
        target.write_text("A\nB\nC\n", encoding="utf-8")
        patch = "@@\n A\n-B\n+BX\n C"

        result = self.agent.action_apply_unified_patch(
            file_path=str(target),
            patch=patch,
            confirmed=True,
        )

        self.assertTrue(result.get("success"), result)
        self.assertEqual(target.read_text(encoding="utf-8"), "A\nBX\nC\n")

    def test_text_file_overwrite_existing_is_rejected(self):
        target = self.root / "sample.txt"
        target.write_text("old\n", encoding="utf-8")

        result = self.agent.action_create_text_file(
            filename=str(target),
            content="new\n",
            confirmed=True,
            overwrite=True,
        )

        self.assertFalse(result.get("success"), result)
        self.assertIn("请改用 edit_text", str(result.get("error", "")))
        self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

    def test_apply_patch_supports_wrapped_begin_end_markers(self):
        target = self.root / "sample.txt"
        target.write_text("begin\nmiddle\nend\n", encoding="utf-8")
        patch = (
            "*** Begin Patch\n"
            "--- a/sample.txt\n"
            "+++ b/sample.txt\n"
            "@@ -1,3 +1,4 @@\n"
            " begin\n"
            "+inserted\n"
            " middle\n"
            " end\n"
            "*** End Patch"
        )

        result = self.agent.action_apply_unified_patch(
            file_path=str(target),
            patch=patch,
            confirmed=True,
        )

        self.assertTrue(result.get("success"), result)
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "begin\ninserted\nmiddle\nend\n",
        )

    def test_change_preview_wraps_long_lines_and_keeps_middle_separator_aligned(self):
        long_old = "A" * 220
        long_new = "B" * 220
        preview = self.agent._format_side_by_side_change_preview([long_old], [long_new], 1, 1)
        plain_preview = [self._strip_ansi(str(x)) for x in preview]

        self.assertGreater(len(plain_preview), 1)
        sep_positions = [x.find("││") for x in plain_preview if "││" in x]
        self.assertTrue(len(sep_positions) > 1)
        self.assertTrue(all(p == sep_positions[0] for p in sep_positions))
        self.assertTrue(any("│ A" in x for x in plain_preview[1:]))
        self.assertTrue(any("│ B" in x for x in plain_preview[1:]))

    def test_replace_rows_are_top_aligned_with_minus_left_plus_right(self):
        preview = self.agent._format_side_by_side_change_preview(
            ["old-1", "old-2"],
            ["new-1", "new-2"],
            10,
            20,
        )
        plain_preview = [self._strip_ansi(str(x)) for x in preview]
        paired_rows = [x for x in plain_preview if x.startswith("- ") and "││ + " in x]
        self.assertEqual(len(paired_rows), 2)


if __name__ == "__main__":
    unittest.main()
