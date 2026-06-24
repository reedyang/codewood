import re
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from cli.tools.apply_patch import action_apply_unified_patch
from cli.core.change_preview_formatter import ChangePreviewFormatter


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
    ) -> List[str]:
        self.preview_segments_calls.append(segments)
        return ChangePreviewFormatter.format_side_by_side_segments(segments)

    def _prompt_confirm_yes_no_maybe_always(self, _message: str, offer_always: bool = False, kind: str = "", **_kwargs: object) -> bool:
        self.prompt_calls += 1
        return True

    def _ephemeral_path_key(self, resolved: Path) -> str:
        return str(resolved)

    def _reload_skills_if_workspace_skill_changed(self, _paths: List[Path]) -> None:
        return None


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class ApplyPatchPreviewTests(unittest.TestCase):
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
    the apply_patch preview sidecar (one file per chat under ``chats/``)."""

    def __init__(self, cfg_dir: Path) -> None:
        self._cfg = Path(cfg_dir)

    def chat_previews_path(self, chat_id: str):
        cid = str(chat_id or "").strip()
        if not cid:
            return None
        # Deterministic per-chat record stem for the test.
        return self._cfg / "chats" / f"record-{cid}.previews.json"


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
            # Per-chat sidecar written under chats/, rows kept out of context.
            sidecar = cfg / "chats" / "record-chat-1.previews.json"
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
            # Active chat only retains the first apply_patch result.
            agent._find_chat_by_id = lambda _cid: {
                "messages": [
                    {
                        "role": "assistant",
                        "content": '[MODEL_TOOL_RESULT]{"tool": "apply_patch", "created_at": "2026-06-24 10:00:00"}',
                    }
                ]
            }
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
            self.assertFalse((cfg / "chats" / "record-chat-1.previews.json").exists())

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


class ChatPreviewSidecarLifecycleTests(unittest.TestCase):
    """Per-chat preview files live under chats/, are deleted with their chat,
    and orphans are cleaned up at startup."""

    def _manager(self, cfg_dir: Path):
        from cli.managers.chat_state_manager import ChatStateManager

        class _Stub:
            pass

        agent = _Stub()
        agent.workspace_config_dir = Path(cfg_dir)
        mgr = ChatStateManager(agent, "chats.json")
        return mgr

    def test_delete_chat_previews_removes_file(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            mgr = self._manager(cfg)
            records = cfg / "chats"
            records.mkdir(parents=True, exist_ok=True)
            preview = records / "abc.previews.json"
            preview.write_text("{}", encoding="utf-8")
            mgr.delete_chat_previews("abc.json")
            self.assertFalse(preview.exists())

    def test_cleanup_orphan_previews(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            mgr = self._manager(cfg)
            records = cfg / "chats"
            records.mkdir(parents=True, exist_ok=True)
            # Orphan: no sibling record.
            orphan = records / "gone.previews.json"
            orphan.write_text("{}", encoding="utf-8")
            # Live: has sibling record.
            (records / "live.json").write_text("{}", encoding="utf-8")
            live_preview = records / "live.previews.json"
            live_preview.write_text("{}", encoding="utf-8")
            mgr.cleanup_orphan_chat_previews()
            self.assertFalse(orphan.exists())
            self.assertTrue(live_preview.exists())


if __name__ == "__main__":
    unittest.main()

