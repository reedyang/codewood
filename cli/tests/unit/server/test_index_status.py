import tempfile
import threading
import unittest
from pathlib import Path

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


class _FakeAgent:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.workspace_name = "Demo"
        self.workspace_id = "workspace-1"
        self._project_context_index = _FakeIndex()


def _app(cfg_dir: Path) -> ServeApp:
    class _Stub:
        pass

    stub = _Stub()
    stub.agent = _FakeAgent(cfg_dir)
    stub.broadcaster = None
    stub._mcp_reconnect_threads = {}
    stub._mcp_reconnect_lock = threading.Lock()
    stub.index_status = getattr(ServeApp, "index_status").__get__(stub, _Stub)
    return stub  # type: ignore[return-value]


class ServeAppIndexStatusTests(unittest.TestCase):
    def test_index_status_includes_progress_percent(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _app(Path(tmp))
            payload = app.index_status()
            self.assertFalse(payload["hidden"])
            self.assertEqual(payload["refresh_progress_percent"], 40)
            self.assertEqual(payload["refresh_progress_done"], 4)


if __name__ == "__main__":
    unittest.main()
