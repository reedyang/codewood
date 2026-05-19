import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from src.actions.filesystem_actions import action_apply_unified_patch


class _DummyPolicy:
    def can_write_path(self, _path: Path, _action: str) -> Dict[str, Any]:
        return {"allowed": True}


class _DummyAgent:
    def __init__(self, work_directory: Path) -> None:
        self.work_directory = work_directory
        self.ai_workspace_dir = work_directory
        self._ai_created_path_keys = set()
        self.preview_calls: List[Dict[str, Any]] = []

    def _get_path_policy(self) -> _DummyPolicy:
        return _DummyPolicy()

    def _resolve_user_path(self, user_path: str) -> Path:
        p = Path(user_path)
        if not p.is_absolute():
            p = self.work_directory / p
        return p.resolve()

    def _is_path_under(self, _path: Path, _root: Path) -> bool:
        return True

    def _format_side_by_side_change_preview(
        self,
        old_lines: List[str],
        new_lines: List[str],
        old_start_line: int = 1,
        new_start_line: int = 1,
    ) -> List[str]:
        self.preview_calls.append(
            {
                "old_lines": old_lines,
                "new_lines": new_lines,
                "old_start_line": old_start_line,
                "new_start_line": new_start_line,
            }
        )
        return ["preview-line"]

    def _prompt_confirm_yes_no_maybe_always(self, _message: str, offer_always: bool = False, kind: str = "") -> bool:
        return True

    def _ephemeral_path_key(self, resolved: Path) -> str:
        return str(resolved)

    def _reload_skills_if_workspace_skill_changed(self, _paths: List[Path]) -> None:
        return None


class ApplyPatchPreviewTests(unittest.TestCase):
    def test_apply_patch_preview_includes_two_context_lines_when_available(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "demo.txt"
            target.write_text("l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n", encoding="utf-8")
            agent = _DummyAgent(root)

            patch = "@@ -4,1 +4,1 @@\n-l4\n+l4_changed\n"
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertEqual(len(agent.preview_calls), 1)
            preview = agent.preview_calls[0]
            self.assertEqual(preview["old_lines"], ["l2", "l3", "l4", "l5", "l6"])
            self.assertEqual(preview["new_lines"], ["l2", "l3", "l4_changed", "l5", "l6"])
            self.assertEqual(preview["old_start_line"], 2)
            self.assertEqual(preview["new_start_line"], 2)
            self.assertEqual(target.read_text(encoding="utf-8"), "l1\nl2\nl3\nl4_changed\nl5\nl6\nl7\nl8\n")

    def test_apply_patch_preview_uses_available_context_near_file_start(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "demo.txt"
            target.write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")
            agent = _DummyAgent(root)

            patch = "@@ -1,1 +1,1 @@\n-l1\n+l1_changed\n"
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertEqual(len(agent.preview_calls), 1)
            preview = agent.preview_calls[0]
            self.assertEqual(preview["old_lines"], ["l1", "l2", "l3"])
            self.assertEqual(preview["new_lines"], ["l1_changed", "l2", "l3"])
            self.assertEqual(preview["old_start_line"], 1)
            self.assertEqual(preview["new_start_line"], 1)


if __name__ == "__main__":
    unittest.main()
