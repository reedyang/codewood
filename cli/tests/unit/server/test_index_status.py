import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

from cli.server.serve_app import ServeApp


class _FakeIndex:
    def status(self):
        return {
            "files_total": 123,
            "refresh_phase": "indexing",
            "refresh_progress_total": 10,
            "refresh_progress_done": 4,
            "refresh_progress_percent": 40,
        }


class _FakeIndexManager:
    """Manager-shaped fake: aggregated ``status()`` plus per-workspace
    ``status_for_storage()``, matching ProjectContextIndexManager."""

    def status(self):
        return {
            "files_total": 321,
            "refresh_phase": "indexing",
            "refresh_progress_total": 10,
            "refresh_progress_done": 4,
            "refresh_progress_percent": 40,
        }

    def status_for_storage(self, storage_dir: Optional[Path]):
        key = str(storage_dir or "")
        if "alpha" in key:
            return {"files_total": 123, "refresh_phase": "", "refresh_progress_percent": 0}
        if "beta" in key:
            return {
                "files_total": 198,
                "refresh_phase": "indexing",
                "refresh_progress_total": 10,
                "refresh_progress_done": 4,
                "refresh_progress_percent": 40,
            }
        return None


class _FakeAgent:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.workspace_name = "Demo"
        self.workspace_id = "workspace-1"
        self._project_context_index = _FakeIndex()

    def _workspace_root_path(self, entry: Dict[str, Any]) -> Path:
        return Path(str(entry.get("root") or ""))

    def _workspace_storage_path(self, entry: Dict[str, Any]) -> Path:
        return Path(str(entry.get("root") or "")) / ".codewood"


class _FakeAgentManager(_FakeAgent):
    def __init__(self, config_dir: Path) -> None:
        super().__init__(config_dir)
        self._project_context_index = _FakeIndexManager()
        self._workspaces_state = {
            "workspaces": {
                "ws-alpha": {
                    "id": "ws-alpha",
                    "name": "Alpha",
                    "kind": "custom",
                    "root": "D:/alpha",
                },
                "ws-beta": {
                    "id": "ws-beta",
                    "name": "Beta",
                    "kind": "custom",
                    "root": "D:/beta",
                },
            }
        }


def _stub_for(agent: Any) -> Any:
    class _Stub:
        pass

    stub = _Stub()
    stub.agent = agent
    stub.broadcaster = None
    stub._mcp_reconnect_threads = {}
    stub._mcp_reconnect_lock = threading.Lock()
    stub.index_status = getattr(ServeApp, "index_status").__get__(stub, _Stub)
    return stub


def _app(cfg_dir: Path) -> ServeApp:
    return _stub_for(_FakeAgent(cfg_dir))  # type: ignore[return-value]


class ServeAppIndexStatusTests(unittest.TestCase):
    def test_index_status_includes_progress_percent(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _app(Path(tmp))
            payload = app.index_status()
            self.assertFalse(payload["hidden"])
            self.assertEqual(payload["refresh_progress_percent"], 40)
            self.assertEqual(payload["refresh_progress_done"], 4)

    def test_index_status_aggregates_every_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = _stub_for(_FakeAgentManager(Path(tmp))).index_status()
            self.assertFalse(payload["hidden"])
            self.assertEqual(payload["files_total"], 321)
            self.assertEqual(len(payload["workspaces"]), 2)
            by_id = {w["id"]: w for w in payload["workspaces"]}
            self.assertEqual(by_id["ws-alpha"]["files_total"], 123)
            self.assertEqual(by_id["ws-beta"]["files_total"], 198)
            self.assertEqual(by_id["ws-beta"]["refresh_phase"], "indexing")


if __name__ == "__main__":
    unittest.main()
