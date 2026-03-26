import importlib.util
import logging
import tempfile
import unittest
from pathlib import Path


def _load_mcp_manager_module():
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "agent" / "mcp_manager.py"
    spec = importlib.util.spec_from_file_location("smart_shell_mcp_manager_completion", str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load mcp_manager module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeClient:
    def __init__(self):
        self.completion_calls = 0

    def completion_complete(self, params, timeout_s=20.0):
        self.completion_calls += 1
        argument = params.get("argument", {}) if isinstance(params, dict) else {}
        value = str(argument.get("value", "")) if isinstance(argument, dict) else ""
        return {"completion": {"values": [f"{value}-1", f"{value}-2"], "total": 2, "hasMore": False}}

    def list_tools(self, timeout_s=8.0):
        return []

    def _shutdown_unlocked(self):
        return None


class McpCompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mcp_module = _load_mcp_manager_module()
        cls.McpManager = cls.mcp_module.McpManager
        cls.McpError = cls.mcp_module.McpError

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.manager = self.McpManager(
            config_dir=self.config_dir,
            mcp_config={"mcpServers": {"fake": {"command": "python", "args": []}}},
        )
        self.fake_client = _FakeClient()
        self.manager._clients["fake"] = self.fake_client

    def tearDown(self):
        logger = logging.getLogger("smart_shell.mcp")
        for handler in list(logger.handlers):
            try:
                handler.close()
            except Exception:
                pass
            try:
                logger.removeHandler(handler)
            except Exception:
                pass
        self.temp_dir.cleanup()

    def test_completion_complete_validates_and_returns_payload(self):
        result = self.manager.completion_complete(
            "fake",
            {"ref": {"name": "summarize_text"}, "argument": {"name": "text", "value": "hel"}},
        )
        completion = result.get("completion", {})
        self.assertEqual(self.fake_client.completion_calls, 1)
        self.assertIn("values", completion)
        self.assertIn("hel-1", completion.get("values", []))
        with self.assertRaises(self.McpError):
            self.manager.completion_complete("fake", "bad-params")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
