import json
import tempfile
import unittest
from pathlib import Path

from src.agent import Agent
from src.tools.project_context_index import (
    ProjectContextIndex,
    search_workspace_files,
)


class _DummyProjectContextIndex:
    def __init__(self) -> None:
        self.calls = []

    def bind_workspace(self, workspace_root: Path, storage_dir: Path | None = None) -> None:
        self.calls.append((Path(workspace_root), Path(storage_dir) if storage_dir is not None else None))


class ProjectContextIndexTests(unittest.TestCase):
    def test_refresh_writes_index_file_even_when_workspace_has_no_code_files(self):
        with tempfile.TemporaryDirectory() as td_workspace, tempfile.TemporaryDirectory() as td_storage:
            workspace = Path(td_workspace)
            storage = Path(td_storage)

            index = ProjectContextIndex(workspace_root=workspace, storage_dir=storage)
            self.assertFalse(index.index_path.exists())

            result = index.refresh_index(force=False)

            self.assertTrue(result["success"])
            self.assertTrue(index.index_path.exists())

            payload = json.loads(index.index_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["workspace_root"], str(workspace.resolve()))
            self.assertEqual(payload["files"], {})
            self.assertIsInstance(payload["last_index_at"], float)

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
