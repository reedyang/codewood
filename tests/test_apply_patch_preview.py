import re
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from src.actions.filesystem_actions import action_apply_unified_patch
from src.core.change_preview_formatter import ChangePreviewFormatter


class _DummyPolicy:
    def can_write_path(self, _path: Path, _action: str) -> Dict[str, Any]:
        return {"allowed": True}


class _DummyAgent:
    def __init__(self, work_directory: Path) -> None:
        self.work_directory = work_directory
        self.ai_workspace_dir = work_directory
        self._ai_created_path_keys = set()
        self.preview_segments_calls: List[List[Dict[str, Any]]] = []

    def _get_path_policy(self) -> _DummyPolicy:
        return _DummyPolicy()

    def _resolve_user_path(self, user_path: str) -> Path:
        p = Path(user_path)
        if not p.is_absolute():
            p = self.work_directory / p
        return p.resolve()

    def _is_path_under(self, _path: Path, _root: Path) -> bool:
        return True

    def _format_side_by_side_change_preview_segments(
        self,
        segments: List[Dict[str, Any]],
    ) -> List[str]:
        self.preview_segments_calls.append(segments)
        return ChangePreviewFormatter.format_side_by_side_segments(segments)

    def _prompt_confirm_yes_no_maybe_always(self, _message: str, offer_always: bool = False, kind: str = "") -> bool:
        return True

    def _ephemeral_path_key(self, resolved: Path) -> str:
        return str(resolved)

    def _reload_skills_if_workspace_skill_changed(self, _paths: List[Path]) -> None:
        return None


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


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
            self.assertEqual(len(agent.preview_segments_calls), 1)
            self.assertEqual(len(agent.preview_segments_calls[0]), 1)
            preview = agent.preview_segments_calls[0][0]
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
            self.assertEqual(len(agent.preview_segments_calls), 1)
            self.assertEqual(len(agent.preview_segments_calls[0]), 1)
            preview = agent.preview_segments_calls[0][0]
            self.assertEqual(preview["old_lines"], ["l1", "l2", "l3"])
            self.assertEqual(preview["new_lines"], ["l1_changed", "l2", "l3"])
            self.assertEqual(preview["old_start_line"], 1)
            self.assertEqual(preview["new_start_line"], 1)

    def test_apply_patch_preview_shows_omitted_line_marker_and_keeps_alignment(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "demo.txt"
            source = "\n".join(f"l{i}" for i in range(1, 21)) + "\n"
            target.write_text(source, encoding="utf-8")
            agent = _DummyAgent(root)

            patch = (
                "@@ -3,1 +3,1 @@\n"
                "-l3\n"
                "+l3_changed\n"
                "@@ -18,1 +18,1 @@\n"
                "-l18\n"
                "+l18_changed\n"
            )
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            rows = [str(x) for x in (result.get("change_preview") or [])]
            clean_rows = [_strip_ansi(r) for r in rows]
            omitted_idx = -1
            for idx, row in enumerate(clean_rows):
                if "... omitted 10 lines ..." in row:
                    omitted_idx = idx
                    break
            self.assertGreater(omitted_idx, 0)
            self.assertLess(omitted_idx + 1, len(clean_rows))
            delim_before = clean_rows[omitted_idx - 1].find(" ││ ")
            delim_omitted = clean_rows[omitted_idx].find(" ││ ")
            delim_after = clean_rows[omitted_idx + 1].find(" ││ ")
            self.assertGreaterEqual(delim_before, 0)
            self.assertEqual(delim_before, delim_omitted)
            self.assertEqual(delim_before, delim_after)
            omitted_row_raw = rows[omitted_idx]
            self.assertIn("\x1b[90m ││ \x1b[0m", omitted_row_raw)
            self.assertIn("│ \x1b[0m\x1b[3;90m... omitted 10 lines ...\x1b[0m", omitted_row_raw)

            add_row_raw = next((r for r in rows if "+    3│" in _strip_ansi(r)), "")
            self.assertTrue(add_row_raw)
            self.assertRegex(add_row_raw, r"\x1b\[90m\+\s+\d+│ \x1b\[0m")


if __name__ == "__main__":
    unittest.main()
