import json
import tempfile
import unittest
from pathlib import Path

from src.agent import Agent
from src.tools.project_context_index import ProjectContextIndex


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
            workspace_config_dir = workspace_root / ".smartshell"
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
            self.assertEqual(bound_storage.name, "project_context_db")
            self.assertEqual(bound_storage.parent.name, workspace_config_dir.name)
