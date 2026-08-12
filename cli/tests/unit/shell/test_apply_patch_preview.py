import json
import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from cli.core.console_utils import GUI_DIFF_BEGIN, GUI_DIFF_END
from cli.tools.apply_patch import ApplyPatchTool, action_apply_unified_patch
from cli.core.change_preview_formatter import ChangePreviewFormatter
from cli.core.file_change_tracker import FileChangeTracker


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
        self.preview_segments_calls: List[List[Dict[str, Any]]] = []
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
        self.preview_segments_calls.append(segments)
        code_language = ChangePreviewFormatter.language_from_path(file_path)
        return ChangePreviewFormatter.format_side_by_side_segments(
            segments, code_language=code_language
        )

    def _prompt_confirm_yes_no_maybe_always(self, _message: str, offer_always: bool = False, kind: str = "", **_kwargs: object) -> bool:
        self.prompt_calls += 1
        return True

    def _ephemeral_path_key(self, resolved: Path) -> str:
        return str(resolved.resolve())

    def _reload_skills_if_workspace_skill_changed(self, _paths: List[Path]) -> None:
        return None


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class ApplyPatchPreviewTests(unittest.TestCase):
    def test_apply_patch_tool_execute_does_not_preflight_auto_confirm(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "hello.py"
            target.write_text("print('old')\n", encoding="utf-8")
            agent = _DummyAgent(root)
            agent.execution_policy = "moderate"

            patch = (
                "--- a/hello.py\n"
                "+++ b/hello.py\n"
                "@@ -1,1 +1,1 @@\n"
                "-print('old')\n"
                "+print('new')\n"
            )

            result = ApplyPatchTool().execute(
                agent,
                {"path": str(target), "patch": patch},
            )

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertEqual(target.read_text(encoding="utf-8"), "print('new')\n")
            self.assertFalse(hasattr(agent, "_freedom_auto_confirm"))

    def test_apply_patch_accepts_legacy_begin_add_file_format(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "prompts.md"
            agent = _DummyAgent(root)

            patch = (
                "*** Begin Patch\n"
                "*** Add File: prompts.md\n"
                "+# Prompts Collection\n"
                "+\n"
                "+## System Prompt (Full)\n"
                "*** End Patch\n"
            )
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "# Prompts Collection\n\n## System Prompt (Full)\n")

    def test_apply_patch_legacy_repeated_end_patch_warns_but_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "prompts.md"
            agent = _DummyAgent(root)

            patch = (
                "*** Begin Patch\n"
                "*** Add File: prompts.md\n"
                "+line1\n"
                "+line2\n"
                "*** End Patch\n"
                "*** End Patch\n"
            )
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertEqual(target.read_text(encoding="utf-8"), "line1\nline2\n")
            warnings = [str(x) for x in (result.get("warnings") or [])]
            self.assertTrue(any("repeated '*** End Patch'" in w for w in warnings))

    def test_apply_patch_can_create_new_file_from_dev_null_patch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "prompts.md"
            agent = _DummyAgent(root)

            patch = (
                "--- /dev/null\n"
                "+++ b/prompts.md\n"
                "@@ -0,0 +1,3 @@\n"
                "+# Prompts Collection\n"
                "+\n"
                "+## System Prompt (Full)\n"
            )
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "# Prompts Collection\n\n## System Prompt (Full)\n")

    def test_apply_patch_deletes_file_from_git_dev_null_patch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "obsolete.py"
            target.write_text("line1\nline2\nline3\n", encoding="utf-8")
            agent = _DummyAgent(root)

            patch = (
                "--- a/obsolete.py\n"
                "+++ /dev/null\n"
                "@@ -1,3 +0,0 @@\n"
                "-line1\n"
                "-line2\n"
                "-line3\n"
            )
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertTrue(result.get("deleted"))
            self.assertFalse(target.exists())
            self.assertIn("Successfully deleted file", result.get("message", ""))

    def test_apply_patch_deletes_file_with_zero_new_side_hunk(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "legacy.txt"
            target.write_text("a\nb\n", encoding="utf-8")
            agent = _DummyAgent(root)

            patch = "@@ -1,2 +0,0 @@\n-a\n-b\n"
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertTrue(result.get("deleted"))
            self.assertFalse(target.exists())

    def test_apply_patch_delete_records_file_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "notes.txt"
            target.write_text("keep me\n", encoding="utf-8")
            agent = _DummyAgent(root)
            tracker = FileChangeTracker()
            agent.file_change_tracker = tracker

            patch = (
                "--- a/notes.txt\n"
                "+++ /dev/null\n"
                "@@ -1,1 +0,0 @@\n"
                "-keep me\n"
            )
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertFalse(target.exists())
            changes = tracker.get_changes()
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0].change_type, "delete")
            self.assertEqual(changes[0].source, "apply_patch")
            self.assertTrue(changes[0].content_before.startswith("keep me"))
            summary = tracker.get_summary()
            self.assertEqual(summary["totalFiles"], 1)
            self.assertEqual(summary["files"][0]["changeType"], "delete")
            self.assertEqual(summary["files"][0]["deletedLines"], 1)

    def test_apply_patch_delete_clears_freedom_review_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "tool.py"
            target.write_text("print(1)\n", encoding="utf-8")
            agent = _DummyAgent(root)
            tracker = FileChangeTracker()
            agent.file_change_tracker = tracker
            key = agent._ephemeral_path_key(target)
            agent._freedom_script_review_entries[key] = {
                "script_sha256": "abc",
                "command_sha256": "def",
                "skip_confirm": True,
                "reason": "safe",
                "updated_at": "2026-01-01T00:00:00",
            }

            patch = (
                "--- a/tool.py\n"
                "+++ /dev/null\n"
                "@@ -1,1 +0,0 @@\n"
                "-print(1)\n"
            )
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertFalse(target.exists())
            self.assertNotIn(key, agent._freedom_script_review_entries)
            cache_file = root / "freedom_script_review_cache.json"
            self.assertTrue(cache_file.exists())
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            self.assertNotIn(key, payload.get("entries", {}))

    def test_apply_patch_delete_all_lines_without_markers_keeps_empty_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "blank.txt"
            target.write_text("x\ny\n", encoding="utf-8")
            agent = _DummyAgent(root)

            patch = "@@ -1,2 +1,1 @@\n-x\n-y\n"
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertFalse(result.get("deleted", False))
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "")

    def test_apply_patch_tolerates_phantom_trailing_empty_deletion(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "demo.txt"
            target.write_text("l1\nl2\n", encoding="utf-8")
            agent = _DummyAgent(root)

            patch = (
                "--- a/demo.txt\n"
                "+++ /dev/null\n"
                "@@ -1,3 +0,0 @@\n"
                "-l1\n"
                "-l2\n"
                "-\n"
            )
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertTrue(result.get("deleted"))
            self.assertFalse(target.exists())

    def test_apply_patch_tolerates_phantom_trailing_empty_deletion_in_edit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "demo.txt"
            target.write_text("l1\nl2\nl3\n", encoding="utf-8")
            agent = _DummyAgent(root)

            patch = "@@ -2,2 +1,1 @@\n-l2\n-l3\n-\n"
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertFalse(result.get("deleted", False))
            self.assertEqual(target.read_text(encoding="utf-8"), "l1\n")

    def test_apply_patch_deletes_bom_file_with_phantom_trailing_line(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "bom.py"
            target.write_bytes(b"\xef\xbb\xbfdef main():\n    pass\n")
            agent = _DummyAgent(root)

            patch = (
                "--- a/bom.py\n"
                "+++ /dev/null\n"
                "@@ -1,3 +0,0 @@\n"
                "-def main():\n"
                "-    pass\n"
                "-\n"
            )
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertTrue(result.get("deleted"))
            self.assertFalse(target.exists())

    def test_apply_patch_partial_deletion_does_not_delete_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "partial.txt"
            target.write_text("l1\nl2\nl3\n", encoding="utf-8")
            agent = _DummyAgent(root)

            patch = "@@ -1,3 +1,2 @@\n-l1\n l2\n l3\n"
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertFalse(result.get("deleted", False))
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "l2\nl3\n")

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

    def test_apply_patch_preview_uses_chinese_omitted_marker_when_language_is_zh_cn(self):
        segments = [
            {
                "old_lines": ["l1"],
                "new_lines": ["l1"],
                "old_start_line": 1,
                "new_start_line": 1,
            },
            {
                "old_lines": ["l12"],
                "new_lines": ["l12"],
                "old_start_line": 12,
                "new_start_line": 12,
            },
        ]

        rows = ChangePreviewFormatter.format_side_by_side_segments(segments, language="zh-CN")

        self.assertTrue(any("已省略 10 行" in _strip_ansi(row) for row in rows))

    def test_moderate_mode_workspace_text_patch_skips_confirm(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace_root = root / "workspace"
            ai_workspace = root / "ai_workspace"
            workspace_root.mkdir(parents=True, exist_ok=True)
            ai_workspace.mkdir(parents=True, exist_ok=True)
            target = workspace_root / "demo.txt"
            target.write_text("hello\n", encoding="utf-8")
            agent = _DummyAgent(workspace_root)
            agent.workspace_config_dir = ai_workspace
            agent.workspace_root = workspace_root
            agent.execution_policy = "moderate"

            patch = "@@ -1,1 +1,1 @@\n-hello\n+hello_mod\n"
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertEqual(agent.prompt_calls, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), "hello_mod\n")

    def test_unlimited_mode_outside_workspace_skips_confirm(self):
        # Regression: unlimited policy must skip the y/n confirmation even
        # when the target file lives outside the workspace root.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace_root = root / "workspace"
            ai_workspace = root / "ai_workspace"
            workspace_root.mkdir(parents=True, exist_ok=True)
            ai_workspace.mkdir(parents=True, exist_ok=True)
            target = root / "outside_demo.txt"
            target.write_text("hello\n", encoding="utf-8")
            agent = _DummyAgent(workspace_root)
            agent.workspace_config_dir = ai_workspace
            agent.workspace_root = workspace_root
            agent.execution_policy = "unlimited"

            patch = "@@ -1,1 +1,1 @@\n-hello\n+hello_mod\n"
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertEqual(agent.prompt_calls, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), "hello_mod\n")

    def test_moderate_mode_gui_still_emits_live_diff_block(self):
        # Regression: under moderate policy the confirm prompt is skipped, but
        # the diff data must still be present in the result so
        # _record_model_tool_execution_history can emit it via the live suffix
        # (previously the diff was printed synchronously to stdout here).
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "demo.txt"
            target.write_text("hello\n", encoding="utf-8")
            agent = _DummyAgent(root)
            agent.execution_policy = "moderate"
            # Mark GUI mode: apply_patch detects it via a callable confirm
            # choice provider.
            agent._confirm_choice_provider = lambda *a, **k: True

            patch = "@@ -1,1 +1,1 @@\n-hello\n+hello_mod\n"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                result = action_apply_unified_patch(
                    agent, str(target), patch, confirmed=False
                )
            out = buf.getvalue()

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertEqual(agent.prompt_calls, 0)
            # The structured diff rows are carried in the result so
            # _record_model_tool_execution_history can stream them to
            # the frontend via the live suffix.
            rows = result.get("change_preview_rows")
            self.assertIsInstance(rows, list)
            self.assertTrue(len(rows) > 0)

    def test_apply_patch_falls_back_to_context_when_hunk_line_number_is_wrong(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "demo.txt"
            target.write_text("a1\na2\na3\n", encoding="utf-8")
            agent = _DummyAgent(root)

            patch = "@@ -3,1 +3,1 @@\n-a2\n+a2_changed\n"
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertEqual(target.read_text(encoding="utf-8"), "a1\na2_changed\na3\n")

    def test_apply_patch_failure_message_preserves_multiline_context(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "demo.txt"
            target.write_text("a1\na2\na3\n", encoding="utf-8")
            agent = _DummyAgent(root)

            patch = "@@ -1,1 +1,1 @@\n-missing\n+changed\n"
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertFalse(result.get("success"))
            error_text = str(result.get("error") or "")
            self.assertIn("\nFile content:\n", error_text)

    def test_apply_patch_with_lf_patch_preserves_crlf_file_newlines(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "demo.txt"
            target.write_bytes(b"a1\r\na2\r\na3\r\n")
            agent = _DummyAgent(root)

            patch = "@@ -2,1 +2,1 @@\n-a2\n+a2_changed\n"
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertEqual(target.read_bytes(), b"a1\r\na2_changed\r\na3\r\n")


class ResponsiveChangePreviewTests(unittest.TestCase):
    _SEGMENTS = [
        {
            "old_lines": ["alpha", "beta", "gamma"],
            "new_lines": ["alpha", "BETA", "gamma"],
            "old_start_line": 1,
            "new_start_line": 1,
        }
    ]

    def test_wide_terminal_uses_side_by_side(self):
        rows = ChangePreviewFormatter.format_segments_responsive(
            self._SEGMENTS, terminal_width=160
        )
        # Side-by-side rows contain the " ││ " column separator.
        self.assertTrue(any("││" in _strip_ansi(r) for r in rows))

    def test_narrow_terminal_uses_inline(self):
        rows = ChangePreviewFormatter.format_segments_responsive(
            self._SEGMENTS, terminal_width=50
        )
        plain = [_strip_ansi(r) for r in rows]
        # Inline rows never contain the side-by-side column separator.
        self.assertFalse(any("││" in r for r in plain))
        # The deleted and inserted lines appear on separate rows.
        self.assertTrue(any(r.lstrip().startswith("- ") for r in plain))
        self.assertTrue(any(r.lstrip().startswith("+ ") for r in plain))
        joined = "\n".join(plain)
        self.assertIn("beta", joined)
        self.assertIn("BETA", joined)

    def test_unknown_width_defaults_to_side_by_side(self):
        rows = ChangePreviewFormatter.format_segments_responsive(
            self._SEGMENTS, terminal_width=0
        )
        self.assertTrue(any("││" in _strip_ansi(r) for r in rows))

    def test_format_segments_structured_emits_typed_rows(self):
        rows = ChangePreviewFormatter.format_segments_structured(self._SEGMENTS)
        self.assertTrue(rows)
        types = {r["type"] for r in rows}
        # The replace hunk yields a "change" row (old "beta" -> new "BETA").
        self.assertIn("change", types)
        change = next(r for r in rows if r["type"] == "change")
        self.assertEqual(change["oldText"], "beta")
        self.assertEqual(change["newText"], "BETA")
        self.assertIsInstance(change["oldNo"], int)
        self.assertIsInstance(change["newNo"], int)
        # Every row exposes the JSON-serializable shape the GUI relies on.
        for r in rows:
            self.assertEqual(
                set(r.keys()), {"type", "oldNo", "newNo", "oldText", "newText"}
            )


class _FakePreviewChatStateManager:
    """Minimal chat-state manager exposing the per-chat previews path used by
    the apply_patch preview sidecar (one file per chat under
    ``chats/data/<record-stem>/previews.json``)."""

    def __init__(self, cfg_dir: Path) -> None:
        self._cfg = Path(cfg_dir)

    def chat_previews_path(self, chat_id: str):
        cid = str(chat_id or "").strip()
        if not cid:
            return None
        # Deterministic per-chat record stem for the test.
        return (
            self._cfg / "chats" / "data" / f"record-{cid}" / "previews.json"
        )


class ApplyPatchPreviewSidecarTests(unittest.TestCase):
    """The change-preview rows must persist OUTSIDE the model context (a per-chat
    sidecar under ``chats/``) and be recoverable on transcript reload, keyed by
    the tool-result ``created_at`` (unique within a chat)."""

    def _agent(self, cfg_dir: Path, gui: bool):
        from cli.agent import Agent

        class _Stub:
            pass

        stub = _Stub()
        stub.workspace_config_dir = cfg_dir
        stub.active_chat_id = "chat-1"
        stub._chat_state_manager = _FakePreviewChatStateManager(cfg_dir)
        if gui:
            stub._confirm_choice_provider = lambda *a, **k: "y"
        for name in (
            "_apply_patch_preview_path",
            "_load_apply_patch_preview_store",
            "_persist_apply_patch_preview_sidecar",
            "_prune_apply_patch_preview_sidecar",
            "_replay_apply_patch_gui_diff_block",
        ):
            setattr(stub, name, getattr(Agent, name).__get__(stub, _Stub))
        return stub

    def test_rows_persist_to_sidecar_and_replay_on_reload(self):
        import io
        import contextlib

        rows = [
            {"type": "change", "oldNo": 2, "newNo": 2, "oldText": "beta", "newText": "BETA"}
        ]
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            agent = self._agent(cfg, gui=True)
            created_at = "2026-06-24 10:00:00"
            agent._persist_apply_patch_preview_sidecar(
                {"file_path": "src/x.py"},
                {"file": "/abs/src/x.py", "change_preview_rows": rows},
                created_at,
            )
            # Per-chat sidecar written under chats/data/<stem>/, rows kept out
            # of context.
            sidecar = cfg / "chats" / "data" / "record-chat-1" / "previews.json"
            self.assertTrue(sidecar.exists())

            # Reload replay finds the rows by created_at key.
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                agent._replay_apply_patch_gui_diff_block(
                    {
                        "tool": "apply_patch",
                        "args": {"file_path": "src/x.py"},
                        "created_at": created_at,
                    }
                )
            out = buf.getvalue()
            self.assertIn("\ue006", out)
            self.assertIn("\ue007", out)
            self.assertIn("BETA", out)

    def test_apply_patch_result_carries_structured_preview_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "f.py"
            target.write_text("a\nb\nc\n", encoding="utf-8")
            agent = _DummyAgent(root)
            patch = "@@ -2,1 +2,1 @@\n-b\n+B\n"
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)
            self.assertTrue(result.get("success"), result.get("error"))
            rows = result.get("change_preview_rows")
            self.assertIsInstance(rows, list)
            self.assertTrue(rows)
            self.assertTrue(any(r.get("type") == "change" for r in rows))

    def test_replay_noop_without_gui_provider(self):
        import io
        import contextlib

        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            agent = self._agent(cfg, gui=False)
            agent._persist_apply_patch_preview_sidecar(
                {"file_path": "src/x.py"},
                {
                    "file": "/abs/src/x.py",
                    "change_preview_rows": [
                        {"type": "add", "oldNo": None, "newNo": 1, "oldText": "", "newText": "z"}
                    ],
                },
                "2026-06-24 10:00:00",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                agent._replay_apply_patch_gui_diff_block(
                    {
                        "tool": "apply_patch",
                        "args": {"file_path": "src/x.py"},
                        "created_at": "2026-06-24 10:00:00",
                    }
                )
            self.assertEqual(buf.getvalue(), "")

    def test_preview_store_is_cached_until_sidecar_changes(self):
        """History builds look the preview sidecar up once per tool round; the
        store must be parsed at most once per on-disk revision (the sidecar can
        be several MB) and reloaded only when the file's mtime changes."""
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            agent = self._agent(cfg, gui=True)
            sidecar = cfg / "chats" / "data" / "record-chat-1" / "previews.json"
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(
                json.dumps({"k1": {"file": "a.py", "diffRows": []}}),
                encoding="utf-8",
            )

            # Repeated reads with an unchanged file must not re-parse it.
            with patch("json.load", wraps=json.load) as load:
                first = agent._load_apply_patch_preview_store()
                second = agent._load_apply_patch_preview_store()
            self.assertEqual(load.call_count, 1)
            self.assertEqual(first, {"k1": {"file": "a.py", "diffRows": []}})
            self.assertIs(first, second)

            # A new revision on disk (e.g. a live task persisted a fresh diff)
            # bumps the mtime, so the next read must reload from disk.
            time.sleep(0.01)
            sidecar.write_text(
                json.dumps(
                    {
                        "k1": {"file": "a.py", "diffRows": []},
                        "k2": {"file": "b.py", "diffRows": []},
                    }
                ),
                encoding="utf-8",
            )
            with patch("json.load", wraps=json.load) as load:
                third = agent._load_apply_patch_preview_store()
            self.assertEqual(load.call_count, 1)
            self.assertIn("k2", third)

            # Switching to another chat points at a different sidecar path, so
            # the old cache entry must not leak across chats.
            sidecar2 = cfg / "chats" / "data" / "record-chat-2" / "previews.json"
            sidecar2.parent.mkdir(parents=True, exist_ok=True)
            sidecar2.write_text(
                json.dumps({"z": {"file": "z.py", "diffRows": []}}),
                encoding="utf-8",
            )
            agent.active_chat_id = "chat-2"
            other = agent._load_apply_patch_preview_store()
            self.assertEqual(other, {"z": {"file": "z.py", "diffRows": []}})

    def test_prune_drops_entries_absent_from_chat(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            agent = self._agent(cfg, gui=True)
            for ca in ("2026-06-24 10:00:00", "2026-06-24 10:00:05"):
                agent._persist_apply_patch_preview_sidecar(
                    {"file_path": "a.py"},
                    {"file": "/abs/a.py", "change_preview_rows": [
                        {"type": "add", "oldNo": None, "newNo": 1, "oldText": "", "newText": "x"}
                    ]},
                    ca,
                )
            # Active conversation history only retains the first apply_patch result.
            agent.conversation_history = [
                {
                    "role": "assistant",
                    "content": "Applying patch",
                    "_tool_rounds_raw": [
                        {
                            "tool": "apply_patch",
                            "previewRef": "2026-06-24 10:00:00",
                            "args": {"file_path": "a.py"},
                            "failed": False,
                        }
                    ],
                }
            ]
            agent._prune_apply_patch_preview_sidecar()
            store = agent._load_apply_patch_preview_store()
            self.assertEqual(set(store.keys()), {"2026-06-24 10:00:00"})

    def test_prune_removes_file_when_nothing_remains(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            agent = self._agent(cfg, gui=True)
            agent._persist_apply_patch_preview_sidecar(
                {"file_path": "a.py"},
                {"file": "/abs/a.py", "change_preview_rows": [
                    {"type": "add", "oldNo": None, "newNo": 1, "oldText": "", "newText": "x"}
                ]},
                "2026-06-24 10:00:00",
            )
            agent._find_chat_by_id = lambda _cid: {"messages": []}
            agent._prune_apply_patch_preview_sidecar()
            self.assertFalse(
                (cfg / "chats" / "data" / "record-chat-1" / "previews.json").exists()
            )

    def test_multiple_patches_same_chat_keep_distinct_entries(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            agent = self._agent(cfg, gui=True)
            agent._persist_apply_patch_preview_sidecar(
                {"file_path": "a.py"},
                {"file": "/abs/a.py", "change_preview_rows": [
                    {"type": "add", "oldNo": None, "newNo": 1, "oldText": "", "newText": "one"}
                ]},
                "2026-06-24 10:00:00",
            )
            agent._persist_apply_patch_preview_sidecar(
                {"file_path": "a.py"},
                {"file": "/abs/a.py", "change_preview_rows": [
                    {"type": "add", "oldNo": None, "newNo": 2, "oldText": "", "newText": "two"}
                ]},
                "2026-06-24 10:00:05",
            )
            store = agent._load_apply_patch_preview_store()
            self.assertEqual(set(store.keys()), {"2026-06-24 10:00:00", "2026-06-24 10:00:05"})

    def test_record_history_apply_patch_attaches_raw_immediately(self):
        from cli.agent import Agent

        class _Stub(_DummyAgent):
            def __init__(self, work_directory: Path) -> None:
                super().__init__(work_directory)
                self.active_chat_id = "chat-1"
                self._chat_state_manager = _FakePreviewChatStateManager(work_directory)
                self.conversation_history = [
                    {
                        "role": "assistant",
                        "content": "{\"tool_calls\":[]}",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "apply_patch", "arguments": "{}"},
                            }
                        ],
                    }
                ]
                self._accumulated_tool_rounds = []
                self._accumulated_tool_rounds_raw = []
                self._last_tool_issuing_assistant = self.conversation_history[0]
                self.sync_calls = 0
                self.synced_raw_lengths: List[int] = []

            def _sync_active_chat_messages(self):
                self.sync_calls += 1
                issuing = self.conversation_history[0]
                raw = issuing.get("_tool_rounds_raw")
                self.synced_raw_lengths.append(len(raw) if isinstance(raw, list) else 0)

            def _format_tool_call_feedback_line(
                self,
                tool_name: str,
                args: Dict[str, Any],
                failed: bool = False,
                is_add_file: Optional[bool] = None,
                background: bool = False,
            ) -> str:
                return f"tool={tool_name} failed={failed} path={args.get('path', '')}"

            def _ui_language(self) -> str:
                return "en"

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agent = _Stub(root)
            target = root / "demo.txt"
            target.write_text("hello\n", encoding="utf-8")
            patch = "@@ -1,1 +1,1 @@\n-hello\n+hello_mod\n"
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)
            self.assertTrue(result.get("success"), result.get("error"))

            for name in (
                "_apply_patch_preview_path",
                "_load_apply_patch_preview_store",
                "_persist_apply_patch_preview_sidecar",
                "_append_tool_preview_blocks",
                "_attach_accumulated_tool_rounds",
                "_next_tool_call_id",
                "_record_model_tool_execution_history",
            ):
                setattr(agent, name, getattr(Agent, name).__get__(agent, _Stub))
            agent._extract_live_tool_round_suffix = Agent._extract_live_tool_round_suffix
            agent._extract_tool_result_output = Agent._extract_tool_result_output
            agent._is_apply_patch_add_file = Agent._is_apply_patch_add_file.__get__(agent, _Stub)

            agent._record_model_tool_execution_history(
                "apply_patch",
                {"path": str(target), "patch": patch},
                result,
                is_add_file=False,
            )

            issuing = agent.conversation_history[0]
            raw = issuing.get("_tool_rounds_raw")
            self.assertIsInstance(raw, list)
            self.assertEqual(len(raw), 1)
            self.assertEqual(raw[0].get("tool"), "apply_patch")
            self.assertTrue(raw[0].get("previewRef"))
            self.assertEqual(len(agent._accumulated_tool_rounds_raw), 1)
            self.assertEqual(len(agent._accumulated_tool_rounds), 1)
            self.assertIn(GUI_DIFF_BEGIN, agent._accumulated_tool_rounds[0])
            self.assertIn(GUI_DIFF_END, agent._accumulated_tool_rounds[0])
            self.assertGreaterEqual(agent.sync_calls, 1)
            self.assertTrue(agent.synced_raw_lengths)
            self.assertEqual(agent.synced_raw_lengths[0], 1)

    def test_attach_accumulated_tool_rounds_reattaches_stale_issuing(self):
        """Raw tool rounds must land on the live assistant message, not on a
        detached copy of it.

        A mid-batch chat reload/switch (e.g. the GUI confirm dialog for
        reject-with-supplement re-activating the session while the tool call
        is pending) re-creates the history message dicts. The
        ``_last_tool_issuing_assistant`` reference then points at the stale
        copy; attaching ``_tool_rounds_raw`` there would silently drop the
        rejected call's raw round from the persisted history (history reload
        would only show the later, successful apply_patch).
        """
        from cli.agent import Agent

        class _Stub(_DummyAgent):
            def __init__(self, work_directory: Path) -> None:
                super().__init__(work_directory)
                self.active_chat_id = "chat-1"
                self._chat_state_manager = _FakePreviewChatStateManager(work_directory)
                self.live_assistant = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-live",
                            "type": "function",
                            "function": {"name": "apply_patch", "arguments": "{}"},
                        }
                    ],
                }
                self.conversation_history = [self.live_assistant]
                # A detached copy: same content, different object. It is no
                # longer part of conversation_history after the reload.
                self.stale_assistant = dict(self.live_assistant)
                self._last_tool_issuing_assistant = self.stale_assistant
                self._accumulated_tool_rounds = []
                self._accumulated_tool_rounds_raw = [
                    {"tool": "apply_patch", "args": {}, "failed": True, "output": "x"}
                ]
                self.sync_calls = 0

            def _sync_active_chat_messages(self) -> None:
                self.sync_calls += 1

        with tempfile.TemporaryDirectory() as td:
            agent = _Stub(Path(td))
            attach = Agent._attach_accumulated_tool_rounds.__get__(agent, _Stub)
            ok = attach(clear=True)
            self.assertTrue(ok)
            live_raw = agent.live_assistant.get("_tool_rounds_raw")
            self.assertIsInstance(live_raw, list)
            self.assertEqual(len(live_raw), 1)
            self.assertEqual(live_raw[0].get("tool"), "apply_patch")
            self.assertTrue(live_raw[0].get("failed"))
            # The stale detached copy must never receive the rounds.
            self.assertNotIn("_tool_rounds_raw", agent.stale_assistant)
            # The issuing reference is re-pointed at the live message.
            self.assertIs(agent._last_tool_issuing_assistant, agent.live_assistant)
            self.assertEqual(agent.sync_calls, 1)
            self.assertEqual(agent._accumulated_tool_rounds_raw, [])


class ChatPreviewSidecarLifecycleTests(unittest.TestCase):
    """Per-chat side data lives under ``chats/data/<record-stem>/``, is deleted
    wholesale with its chat, and orphans are cleaned up at startup."""

    def _manager(self, cfg_dir: Path):
        from cli.managers.chat_state_manager import ChatStateManager

        class _Stub:
            pass

        agent = _Stub()
        agent.workspace_config_dir = Path(cfg_dir)
        mgr = ChatStateManager(agent, "chats.json")
        return mgr

    def test_delete_chat_data_removes_dir(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            mgr = self._manager(cfg)
            data_dir = cfg / "chats" / "data" / "abc"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "previews.json").write_text("{}", encoding="utf-8")
            (data_dir / "img_x.png").write_bytes(b"\x89PNG")
            mgr.delete_chat_data("abc.json")
            self.assertFalse(data_dir.exists())

    def test_cleanup_orphan_data(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            mgr = self._manager(cfg)
            records = cfg / "chats"
            data_root = records / "data"
            data_root.mkdir(parents=True, exist_ok=True)
            # Orphan: no sibling record.
            orphan = data_root / "gone"
            orphan.mkdir()
            (orphan / "previews.json").write_text("{}", encoding="utf-8")
            # Live: has sibling record.
            (records / "live.json").write_text("{}", encoding="utf-8")
            live_dir = data_root / "live"
            live_dir.mkdir()
            (live_dir / "previews.json").write_text("{}", encoding="utf-8")
            mgr.cleanup_orphan_chat_data()
            self.assertFalse(orphan.exists())
            self.assertTrue(live_dir.exists())


class ChangePreviewHighlightTests(unittest.TestCase):
    """The diff preview applies syntax highlighting to code text, and changed
    lines keep a subtle background tint that re-arms across syntax-color resets
    so foreground colors stay readable."""

    def setUp(self):
        import cli.core.console_utils as cu
        import cli.core.syntax_highlighter as sh

        self._cu_orig = cu._stdout_color_enabled
        self._sh_orig = sh._stdout_color_enabled
        cu._stdout_color_enabled = lambda: True
        sh._stdout_color_enabled = lambda: True

    def tearDown(self):
        import cli.core.console_utils as cu
        import cli.core.syntax_highlighter as sh

        cu._stdout_color_enabled = self._cu_orig
        sh._stdout_color_enabled = self._sh_orig

    def test_language_from_path(self):
        from cli.core.change_preview_formatter import ChangePreviewFormatter as F

        self.assertEqual(F.language_from_path("a/b/foo.py"), "python")
        self.assertEqual(F.language_from_path("foo.TSX"), "typescript")
        self.assertEqual(F.language_from_path("Dockerfile"), "dockerfile")
        self.assertIsNone(F.language_from_path("foo.unknownext"))
        self.assertIsNone(F.language_from_path(""))

    def test_side_by_side_highlights_and_rearms_bg(self):
        from cli.core.change_preview_formatter import ChangePreviewFormatter as F

        segs = [{
            "old_lines": ["def main():"],
            "new_lines": ["def maine():"],
            "old_start_line": 1,
            "new_start_line": 1,
        }]
        out = "\n".join(F.format_segments_responsive(segs, terminal_width=120, code_language="python"))
        # Keyword color applied (purple 198;120;221) and del/add bg tints present.
        self.assertIn("38;2;198;120;221", out)
        self.assertIn(F.ANSI_BG_DEL, out)
        self.assertIn(F.ANSI_BG_ADD, out)
        # The bg is re-armed right after a reset so it spans the cell.
        self.assertIn(F.ANSI_RESET + F.ANSI_BG_DEL, out)

    def test_no_code_language_leaves_text_unhighlighted(self):
        from cli.core.change_preview_formatter import ChangePreviewFormatter as F

        segs = [{
            "old_lines": ["def main():"],
            "new_lines": ["def maine():"],
            "old_start_line": 1,
            "new_start_line": 1,
        }]
        out = "\n".join(F.format_segments_responsive(segs, terminal_width=120))
        self.assertNotIn("38;2;198;120;221", out)

    def test_highlight_continues_across_wrap_boundary(self):
        from cli.core.change_preview_formatter import ChangePreviewFormatter as F

        long_str = '    print("' + ("alpha beta gamma " * 8) + '")'
        segs = [{
            "old_lines": [long_str],
            "new_lines": [long_str.replace("alpha", "ALPHA")],
            "old_start_line": 1,
            "new_start_line": 1,
        }]
        lines = F.format_segments_responsive(segs, terminal_width=50, code_language="python")
        # The string spans several wrapped continuation rows; each continuation
        # row (no line-number, just the "│" gutter) must re-arm the green string
        # color rather than dropping back to default.
        green = "38;2;152;195;121"
        cont_rows = [ln for ln in lines if ln.lstrip().startswith("\x1b[90m") and "│" in ln]
        # At least one continuation row beyond the first should carry the color.
        colored_cont = [ln for ln in lines if green in ln]
        self.assertGreaterEqual(len(colored_cont), 3)

    def test_ansi_slicer_preserves_color(self):
        from cli.core.change_preview_formatter import ChangePreviewFormatter as F

        # "<green>aaaa...<reset>" wider than the width must split into chunks
        # that each re-arm the green and end with a reset.
        green = "\x1b[38;2;1;2;3m"
        reset = F.ANSI_RESET
        ansi = f"{green}{'a' * 20}{reset}"
        chunks = F._slice_ansi_by_display_width(ansi, 8)
        self.assertGreater(len(chunks), 1)
        for ch in chunks:
            self.assertIn(green, ch)
            self.assertTrue(ch.endswith(reset))

    def test_fragments_compose_bg_with_syntax_style(self):
        from cli.core.change_preview_formatter import ChangePreviewFormatter as F

        segs = [{
            "old_lines": ["x = 1"],
            "new_lines": ["x = 2"],
            "old_start_line": 1,
            "new_start_line": 1,
        }]
        frags = F.format_segments_responsive_fragments(segs, terminal_width=120, code_language="python")
        # A changed-line fragment carries the del/add bg composed with a fg style.
        self.assertTrue(any(F.PT_BG_DEL in style for style, _ in frags))
        self.assertTrue(any(F.PT_BG_ADD in style for style, _ in frags))


class _PlanModeDummyAgent:
    def __init__(self, work_directory: Path, plan_mode: bool = False,
                 temp_dir: Optional[Path] = None) -> None:
        self.work_directory = work_directory
        self.workspace_root = work_directory
        self.workspace_config_dir = work_directory
        self.execution_policy = "confirmation"
        self._plan_mode_sticky = plan_mode
        self._ai_created_path_keys = set()
        self.prompt_calls = 0
        self.ai_workspace_temp_dir = temp_dir

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
        from cli.core.change_preview_formatter import ChangePreviewFormatter
        code_language = ChangePreviewFormatter.language_from_path(file_path)
        return ChangePreviewFormatter.format_side_by_side_segments(
            segments, code_language=code_language
        )

    def _prompt_confirm_yes_no_maybe_always(self, _message: str, offer_always: bool = False, kind: str = "", **_kwargs: object) -> bool:
        self.prompt_calls += 1
        return True

    def _ephemeral_path_key(self, resolved: Path) -> str:
        return str(resolved)

    def _reload_skills_if_workspace_skill_changed(self, _paths: List[Path]) -> None:
        return None


class ApplyPatchPlanModeGuardTests(unittest.TestCase):
    def test_plan_mode_blocks_workspace_file_modification(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "existing.py"
            target.write_text("x = 1\n", encoding="utf-8")
            agent = _PlanModeDummyAgent(root, plan_mode=True)

            patch = "@@ -1 +1 @@\n-x = 1\n+x = 2\n"
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertFalse(result.get("success"))
            self.assertIn("Plan mode", result.get("error", ""))
            self.assertIn("under the workspace root", result.get("error", ""))

    def test_plan_mode_blocks_new_file_under_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "new_file.py"
            agent = _PlanModeDummyAgent(root, plan_mode=True)

            patch = (
                "*** Begin Patch\n"
                "*** Add File: new_file.py\n"
                "+x = 1\n"
                "*** End Patch\n"
            )
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertFalse(result.get("success"))
            self.assertIn("Plan mode", result.get("error", ""))

    def test_plan_mode_allows_file_outside_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside_dir = Path(tempfile.mkdtemp())
            try:
                target = outside_dir / "plan_notes.md"
                agent = _PlanModeDummyAgent(root, plan_mode=True)

                patch = (
                    "*** Begin Patch\n"
                    "*** Add File: plan_notes.md\n"
                    "+# Plan Notes\n"
                    "+## Phase 1\n"
                    "*** End Patch\n"
                )
                result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

                self.assertTrue(result.get("success"), result.get("error"))
                self.assertTrue(target.exists())
            finally:
                import shutil
                shutil.rmtree(str(outside_dir), ignore_errors=True)

    def test_non_plan_mode_allows_workspace_file_modification(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "existing.py"
            target.write_text("x = 1\n", encoding="utf-8")
            agent = _PlanModeDummyAgent(root, plan_mode=False)

            patch = "@@ -1 +1 @@\n-x = 1\n+x = 2\n"
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertEqual(target.read_text(encoding="utf-8"), "x = 2\n")

    def test_plan_mode_allows_temp_dir_writes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            temp_dir = root / "temp"
            temp_dir.mkdir(parents=True, exist_ok=True)
            target = temp_dir / "plan_notes.md"
            agent = _PlanModeDummyAgent(root, plan_mode=True, temp_dir=temp_dir)

            patch = (
                "*** Begin Patch\n"
                "*** Add File: plan_notes.md\n"
                "+# Plan Notes\n"
                "+## Implementation Plan\n"
                "*** End Patch\n"
            )
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertTrue(target.exists())

    def test_plan_mode_blocks_file_nearby_but_not_under_temp_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            temp_dir = root / "temp"
            temp_dir.mkdir(parents=True, exist_ok=True)
            # File is at root level, not under temp_dir
            target = root / "src" / "main.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("print('hello')\n", encoding="utf-8")
            agent = _PlanModeDummyAgent(root, plan_mode=True, temp_dir=temp_dir)

            patch = "@@ -1 +1 @@\n-print('hello')\n+print('world')\n"
            result = action_apply_unified_patch(agent, str(target), patch, confirmed=False)

            self.assertFalse(result.get("success"))
            self.assertIn("Plan mode", result.get("error", ""))
            self.assertIn(str(temp_dir), result.get("error", ""))


if __name__ == "__main__":
    unittest.main()
