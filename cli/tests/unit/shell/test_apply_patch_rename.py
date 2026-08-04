import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from cli.tools.apply_patch import ApplyPatchTool, action_apply_unified_patch


class _DummyPolicy:
    def can_write_path(self, _path: Path, _action: str) -> Dict[str, Any]:
        return {"allowed": True}


class _DummyAgent:
    def __init__(self, work_directory: Path) -> None:
        self.work_directory = work_directory
        self.workspace_root = work_directory
        self.workspace_config_dir = work_directory
        self.execution_policy = "confirmation"
        self._ai_created_path_keys = set()
        self._freedom_script_review_entries = {}
        self.prompt_calls = 0

    def _get_path_policy(self) -> _DummyPolicy:
        return _DummyPolicy()

    def _resolve_user_path(self, user_path: str) -> Path:
        p = Path(user_path)
        if not p.is_absolute():
            p = self.work_directory / p
        return p.resolve()

    def _is_path_under(self, _path: Path, _root: Path) -> bool:
        try:
            Path(_path).resolve().relative_to(Path(_root).resolve())
            return True
        except Exception:
            return False

    def _format_side_by_side_change_preview_segments(
        self,
        segments: List[Dict[str, Any]],
        file_path: Any = None,
    ) -> List[str]:
        return []

    def _prompt_confirm_yes_no_maybe_always(
        self, _message: str, offer_always: bool = False, kind: str = "", **_kwargs: object
    ) -> bool:
        self.prompt_calls += 1
        return True

    def _ephemeral_path_key(self, resolved: Path) -> str:
        return str(resolved.resolve())

    def _reload_skills_if_workspace_skill_changed(self, _paths: List[Path]) -> None:
        return None


class ApplyPatchRenameTests(unittest.TestCase):
    def _assert_rename_rejected(self, agent, target, patch, old, new):
        result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)
        self.assertFalse(result.get("success"), "rename patch must be rejected")
        error = str(result.get("error") or "")
        self.assertIn("does not support renaming files", error)
        self.assertIn(old, error)
        self.assertIn(new, error)
        self.assertIn("shell", error)
        return result

    def test_apply_patch_rejects_git_rename_from_to_lines(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "old.py"
            target.write_text("print('hi')\n", encoding="utf-8")
            agent = _DummyAgent(root)
            patch = (
                "diff --git a/old.py b/new.py\n"
                "similarity index 100%\n"
                "rename from old.py\n"
                "rename to new.py\n"
            )
            self._assert_rename_rejected(agent, target, patch, "old.py", "new.py")
            self.assertTrue(target.exists(), "old file must not be touched")
            self.assertFalse((root / "new.py").exists(), "new file must not be created")

    def test_apply_patch_rejects_diff_git_header_with_different_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "old.py"
            target.write_text("line1\nline2\n", encoding="utf-8")
            agent = _DummyAgent(root)
            patch = (
                "diff --git a/old.py b/new.py\n"
                "--- a/old.py\n"
                "+++ b/new.py\n"
                "@@ -1,2 +1,2 @@\n"
                " line1\n"
                "-line2\n"
                "+line2 changed\n"
            )
            self._assert_rename_rejected(agent, target, patch, "old.py", "new.py")
            self.assertTrue(target.exists())
            self.assertFalse((root / "new.py").exists())

    def test_apply_patch_rejects_rename_via_dev_null_header_pair(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "old.txt"
            target.write_text("content\n", encoding="utf-8")
            agent = _DummyAgent(root)
            patch = (
                "--- a/old.txt\n"
                "+++ b/new.txt\n"
                "@@ -1 +1 @@\n"
                "-content\n"
                "+content\n"
            )
            self._assert_rename_rejected(agent, target, patch, "old.txt", "new.txt")
            self.assertTrue(target.exists())
            self.assertFalse((root / "new.txt").exists())

    def test_apply_patch_tool_execute_rejects_rename_patch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "old.py"
            target.write_text("print('x')\n", encoding="utf-8")
            agent = _DummyAgent(root)
            patch = (
                "--- a/old.py\n"
                "+++ b/new.py\n"
                "@@ -1 +1 @@\n"
                "-print('x')\n"
                "+print('y')\n"
            )
            result = ApplyPatchTool().execute(agent, {"path": str(target), "patch": patch})
            self.assertFalse(result.get("success"))
            self.assertIn("does not support renaming files", str(result.get("error") or ""))
            self.assertIn("shell", str(result.get("error") or ""))

    def test_apply_patch_still_accepts_normal_edit_patch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "same.py"
            target.write_text("print('old')\n", encoding="utf-8")
            agent = _DummyAgent(root)
            patch = (
                "--- a/same.py\n"
                "+++ b/same.py\n"
                "@@ -1 +1 @@\n"
                "-print('old')\n"
                "+print('new')\n"
            )
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)
            self.assertTrue(result.get("success"), result.get("error"))
            self.assertEqual(target.read_text(encoding="utf-8"), "print('new')\n")

    def test_apply_patch_still_accepts_create_and_delete_patches(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agent = _DummyAgent(root)
            create = (
                "--- /dev/null\n"
                "+++ b/brand_new.py\n"
                "@@ -0,0 +1 @@\n"
                "+hello\n"
            )
            create_result = action_apply_unified_patch(
                agent, str(root / "brand_new.py"), create, confirmed=False
            )
            self.assertTrue(create_result.get("success"), create_result.get("error"))

            delete = (
                "--- a/brand_new.py\n"
                "+++ /dev/null\n"
                "@@ -1 +0,0 @@\n"
                "-hello\n"
            )
            delete_result = action_apply_unified_patch(
                agent, str(root / "brand_new.py"), delete, confirmed=False
            )
            self.assertTrue(delete_result.get("success"), delete_result.get("error"))
            self.assertTrue(delete_result.get("deleted"))
            self.assertFalse((root / "brand_new.py").exists())


if __name__ == "__main__":
    unittest.main()
