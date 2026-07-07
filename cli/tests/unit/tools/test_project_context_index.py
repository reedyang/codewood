import sqlite3
import tempfile
import unittest
from pathlib import Path

from cli.agent import Agent
from cli.tools.project_context_index import (
    ProjectContextIndex,
    _CallEdge,
    _FileEntry,
    search_workspace_files,
)


class _DummyProjectContextIndex:
    def __init__(self) -> None:
        self.calls = []

    def bind_workspace(self, workspace_root: Path, storage_dir: Path | None = None) -> None:
        self.calls.append((Path(workspace_root), Path(storage_dir) if storage_dir is not None else None))


class ProjectContextIndexTests(unittest.TestCase):
    def test_status_reports_progress_percent_for_all_phases(self):
        with tempfile.TemporaryDirectory() as td_workspace, tempfile.TemporaryDirectory() as td_storage:
            index = ProjectContextIndex(
                workspace_root=Path(td_workspace), storage_dir=Path(td_storage)
            )
            index._index_expected_total = 200

            index._refresh_progress_phase = "scanning"
            index._refresh_progress_done = 50
            index._refresh_progress_total = 0
            self.assertEqual(index.status()["refresh_progress_percent"], 25)

            index._refresh_progress_phase = "scanning"
            index._refresh_progress_done = 200
            index._refresh_progress_total = 0
            self.assertEqual(index.status()["refresh_progress_percent"], 100)

            index._refresh_progress_phase = "indexing"
            index._refresh_progress_done = 3
            index._refresh_progress_total = 8
            self.assertEqual(index.status()["refresh_progress_percent"], 37)

            index._refresh_progress_phase = "saving"
            index._refresh_progress_done = 0
            index._refresh_progress_total = 0
            self.assertEqual(index.status()["refresh_progress_percent"], 100)

            index._refresh_progress_phase = "saving"
            index._refresh_progress_done = 3
            index._refresh_progress_total = 10
            self.assertEqual(index.status()["refresh_progress_percent"], 30)

            index._refresh_progress_phase = ""
            self.assertEqual(index.status()["refresh_progress_percent"], 0)

    def test_refresh_writes_index_file_even_when_workspace_has_no_code_files(self):
        with tempfile.TemporaryDirectory() as td_workspace, tempfile.TemporaryDirectory() as td_storage:
            workspace = Path(td_workspace)
            storage = Path(td_storage)

            index = ProjectContextIndex(workspace_root=workspace, storage_dir=storage)
            self.assertFalse(index.index_path.exists())
            self.assertTrue(str(index.index_path).endswith(".db"))

            result = index.refresh_index(force=False)

            self.assertTrue(result["success"])
            self.assertTrue(index.index_path.exists())

            conn = sqlite3.connect(str(index.index_path))
            try:
                meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
                file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(meta["workspace_root"], str(workspace.resolve()))
            self.assertEqual(file_count, 0)
            self.assertIsInstance(float(meta["last_index_at"]), float)

    def test_bind_project_index_workspace_uses_workspace_root(self):
        with tempfile.TemporaryDirectory() as td_workspace:
            workspace_root = Path(td_workspace) / "workspace_root"
            work_directory = Path(td_workspace) / "current_work_dir"
            workspace_config_dir = workspace_root / ".codewood"
            work_directory.mkdir(parents=True)
            workspace_root.mkdir(parents=True, exist_ok=True)

            dummy = type("DummyAgent", (), {})()
            dummy.workspace_root = workspace_root
            dummy.work_directory = work_directory
            dummy.workspace_config_dir = workspace_config_dir
            dummy._project_context_index = _DummyProjectContextIndex()

            Agent._bind_project_index_workspace(dummy)

            self.assertEqual(len(dummy._project_context_index.calls), 1)
            bound_root, bound_storage = dummy._project_context_index.calls[0]
            self.assertEqual(bound_root.name, workspace_root.name)
            self.assertNotEqual(bound_root.name, work_directory.name)
            self.assertEqual(bound_storage.name, "indexes")
            self.assertEqual(bound_storage.parent.name, workspace_config_dir.name)

    def test_sqlite_roundtrip_persists_symbols_imports_and_calls(self):
        with tempfile.TemporaryDirectory() as td_workspace, tempfile.TemporaryDirectory() as td_storage:
            workspace = Path(td_workspace)
            storage = Path(td_storage)
            (workspace / "mod.py").write_text(
                "import os\n"
                "def helper(x):\n"
                "    return x + 1\n"
                "def main():\n"
                "    helper(1)\n"
                "    os.getcwd()\n",
                encoding="utf-8",
            )

            index = ProjectContextIndex(workspace_root=workspace, storage_dir=storage)
            res = index.refresh_index(force=True)
            self.assertTrue(res["success"])
            self.assertEqual(res["files_total"], 1)

            # A fresh instance must reload everything from SQLite.
            reloaded = ProjectContextIndex(workspace_root=workspace, storage_dir=storage)
            entry = reloaded.files["mod.py"]
            self.assertIn("helper", entry.symbols)
            self.assertIn("main", entry.symbols)

            cg = reloaded.call_graph("main", direction="callees", auto_refresh=False)
            callee_names = {c["callee"] for c in cg.get("callees", [])}
            self.assertIn("helper", callee_names)

    def test_checkpoint_batch_roundtrip_is_loadable(self):
        with tempfile.TemporaryDirectory() as td_workspace, tempfile.TemporaryDirectory() as td_storage:
            workspace = Path(td_workspace)
            storage = Path(td_storage)
            index = ProjectContextIndex(workspace_root=workspace, storage_dir=storage)
            entry = _FileEntry(
                path="pkg/mod.py",
                mtime_ns=123,
                size=456,
                symbols=["main"],
                imports=["os"],
                tokens=["pkg", "mod", "main"],
                calls=[_CallEdge(caller="main", callee="helper")],
            )

            index._save_checkpoint_batch({"pkg/mod.py": entry}, checkpoint_ts=42.0)

            reloaded = ProjectContextIndex(workspace_root=workspace, storage_dir=storage)
            saved = reloaded.files["pkg/mod.py"]
            self.assertEqual(saved.symbols, ["main"])
            self.assertEqual(saved.imports, ["os"])
            self.assertEqual(saved.tokens, [])
            cg = reloaded.call_graph("main", direction="callees", auto_refresh=False)
            self.assertEqual(cg["callees"][0]["callee"], "helper")

    def test_refresh_resumes_from_checkpointed_files_without_reparsing_them(self):
        with tempfile.TemporaryDirectory() as td_workspace, tempfile.TemporaryDirectory() as td_storage:
            workspace = Path(td_workspace)
            storage = Path(td_storage)
            first = workspace / "first.py"
            second = workspace / "second.py"
            first.write_text("def first():\n    return 1\n", encoding="utf-8")
            second.write_text("def second():\n    return 2\n", encoding="utf-8")

            seed = ProjectContextIndex(workspace_root=workspace, storage_dir=storage)
            first_stat = first.stat()
            first_entry = seed._parse_file(
                first,
                "first.py",
                int(getattr(first_stat, "st_mtime_ns", int(first_stat.st_mtime * 1e9))),
                int(first_stat.st_size),
            )
            seed._save_checkpoint_batch({"first.py": first_entry}, checkpoint_ts=1.0)

            resumed = ProjectContextIndex(workspace_root=workspace, storage_dir=storage)
            parsed_rels = []
            original_parse = resumed._parse_file

            def _tracked_parse(p, rel, st_mtime_ns, st_size):
                parsed_rels.append(rel)
                return original_parse(p, rel, st_mtime_ns, st_size)

            resumed._parse_file = _tracked_parse  # type: ignore[method-assign]
            result = resumed.refresh_index(force=False)

            self.assertTrue(result["success"])
            self.assertEqual(parsed_rels, ["second.py"])
            self.assertEqual(sorted(resumed.files.keys()), ["first.py", "second.py"])

    def test_refresh_short_circuits_when_existing_index_is_fully_unchanged(self):
        with tempfile.TemporaryDirectory() as td_workspace, tempfile.TemporaryDirectory() as td_storage:
            workspace = Path(td_workspace)
            storage = Path(td_storage)
            mod = workspace / "mod.py"
            mod.write_text("def mod():\n    return 1\n", encoding="utf-8")

            seeded = ProjectContextIndex(workspace_root=workspace, storage_dir=storage)
            seeded.refresh_index(force=True)

            index = ProjectContextIndex(workspace_root=workspace, storage_dir=storage)
            parse_calls = []
            save_calls = []
            original_parse = index._parse_file
            original_save = index._save

            def _tracked_parse(*args, **kwargs):
                parse_calls.append(True)
                return original_parse(*args, **kwargs)

            def _tracked_save():
                save_calls.append(True)
                return original_save()

            index._parse_file = _tracked_parse  # type: ignore[method-assign]
            index._save = _tracked_save  # type: ignore[method-assign]
            result = index.refresh_index(force=False)

            self.assertTrue(result["success"])
            self.assertEqual(parse_calls, [])
            self.assertEqual(save_calls, [])

    def test_final_save_removes_deleted_files_after_checkpoint_resume(self):
        with tempfile.TemporaryDirectory() as td_workspace, tempfile.TemporaryDirectory() as td_storage:
            workspace = Path(td_workspace)
            storage = Path(td_storage)
            doomed = workspace / "doomed.py"
            doomed.write_text("def doomed():\n    return 0\n", encoding="utf-8")

            index = ProjectContextIndex(workspace_root=workspace, storage_dir=storage)
            index.refresh_index(force=True)
            self.assertIn("doomed.py", index.files)

            doomed.unlink()
            refreshed = ProjectContextIndex(workspace_root=workspace, storage_dir=storage)
            result = refreshed.refresh_index(force=False)

            self.assertTrue(result["success"])
            self.assertNotIn("doomed.py", refreshed.files)

    def test_refresh_keeps_saving_phase_while_running_full_save(self):
        with tempfile.TemporaryDirectory() as td_workspace, tempfile.TemporaryDirectory() as td_storage:
            workspace = Path(td_workspace)
            storage = Path(td_storage)
            (workspace / "save_me.py").write_text("def save_me():\n    return 1\n", encoding="utf-8")

            index = ProjectContextIndex(workspace_root=workspace, storage_dir=storage)
            phases_seen = []
            original_save = index._save

            def _wrapped_save():
                phases_seen.append(index._refresh_progress_phase)
                original_save()

            index._save = _wrapped_save  # type: ignore[method-assign]
            result = index.refresh_index(force=True)

            self.assertTrue(result["success"])
            self.assertIn("saving", phases_seen)

    def test_iter_code_files_reports_final_progress_for_small_workspaces(self):
        with tempfile.TemporaryDirectory() as td_workspace, tempfile.TemporaryDirectory() as td_storage:
            workspace = Path(td_workspace)
            storage = Path(td_storage)
            (workspace / "one.py").write_text("x = 1\n", encoding="utf-8")
            (workspace / "two.py").write_text("y = 2\n", encoding="utf-8")

            index = ProjectContextIndex(workspace_root=workspace, storage_dir=storage)
            progress = []

            files, timed_out = index._iter_code_files(progress_cb=lambda count: progress.append(count))

            self.assertFalse(timed_out)
            self.assertEqual(len(files), 2)
            self.assertEqual(progress[-1], 2)

    def test_call_graph_callers_and_callees(self):
        with tempfile.TemporaryDirectory() as td_workspace, tempfile.TemporaryDirectory() as td_storage:
            workspace = Path(td_workspace)
            storage = Path(td_storage)
            (workspace / "a.py").write_text(
                "def helper(x):\n"
                "    return x\n"
                "def main():\n"
                "    helper(1)\n"
                "    other()\n",
                encoding="utf-8",
            )

            index = ProjectContextIndex(workspace_root=workspace, storage_dir=storage)
            index.refresh_index(force=True)

            callers = index.call_graph("helper", direction="callers", auto_refresh=False)
            self.assertTrue(callers["success"])
            self.assertEqual(callers["callers_total"], 1)
            self.assertEqual(callers["callers"][0]["caller"], "main")

            callees = index.call_graph("main", direction="callees", auto_refresh=False)
            self.assertTrue(callees["success"])
            callee_names = {c["callee"] for c in callees["callees"]}
            self.assertIn("helper", callee_names)
            self.assertIn("other", callee_names)

    def test_call_graph_empty_symbol_fails(self):
        with tempfile.TemporaryDirectory() as td_workspace, tempfile.TemporaryDirectory() as td_storage:
            index = ProjectContextIndex(
                workspace_root=Path(td_workspace), storage_dir=Path(td_storage)
            )
            res = index.call_graph("", auto_refresh=False)
            self.assertFalse(res["success"])


class SearchWorkspaceFilesTests(unittest.TestCase):
    """Filename search backing the ``@`` quick file-reference feature."""

    def _make_tree(self, root: Path) -> None:
        (root / "src" / "components").mkdir(parents=True)
        (root / "src" / "utils").mkdir(parents=True)
        (root / "node_modules" / "pkg").mkdir(parents=True)
        (root / ".git").mkdir(parents=True)
        (root / "src" / "components" / "ChatView.tsx").write_text("x", encoding="utf-8")
        (root / "src" / "utils" / "tokens.ts").write_text("x", encoding="utf-8")
        (root / "src" / "main.py").write_text("x", encoding="utf-8")
        (root / "README.md").write_text("x", encoding="utf-8")
        (root / ".hidden").write_text("x", encoding="utf-8")
        (root / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
        (root / ".git" / "config").write_text("x", encoding="utf-8")

    def test_matches_by_basename_and_excludes_noise_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_tree(root)
            results = search_workspace_files(root, "chatview", 10)
            self.assertIn("src/components/ChatView.tsx", results)
            # Excluded directories and dotfiles never surface.
            joined = "\n".join(results)
            self.assertNotIn("node_modules", joined)
            self.assertNotIn(".git", joined)

    def test_empty_query_returns_candidates_capped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_tree(root)
            results = search_workspace_files(root, "", 2)
            self.assertEqual(len(results), 2)

    def test_dotfiles_are_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_tree(root)
            results = search_workspace_files(root, "hidden", 10)
            self.assertEqual(results, [])

    def test_basename_prefix_ranks_above_substring(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "tokens.ts").write_text("x", encoding="utf-8")
            (root / "my_tokens_helper.ts").write_text("x", encoding="utf-8")
            results = search_workspace_files(root, "tokens", 10)
            self.assertEqual(results[0], "tokens.ts")

    def test_subsequence_fuzzy_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ChatView.tsx").write_text("x", encoding="utf-8")
            # "cvt" is a subsequence of "chatview.tsx".
            results = search_workspace_files(root, "cvt", 10)
            self.assertIn("ChatView.tsx", results)
