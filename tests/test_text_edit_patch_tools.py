import sys
import tempfile
import types
import unittest
from pathlib import Path


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.smart_shell_agent import SmartShellAgent


class TextEditAndPatchToolTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
