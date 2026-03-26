import importlib.util
import logging
import tempfile
import unittest
from pathlib import Path


def _load_mcp_manager_module():
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "agent" / "mcp_manager.py"
    spec = importlib.util.spec_from_file_location("smart_shell_mcp_manager_sampling", str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load mcp_manager module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeClient:
    def __init__(self):
        self.sampling_calls = 0

    def sampling_create_message(self, params, timeout_s=30.0):
        self.sampling_calls += 1
        messages = params.get("messages", []) if isinstance(params, dict) else []
        text = ""
        if isinstance(messages, list) and messages and isinstance(messages[-1], dict):
            content = messages[-1].get("content", {})
            if isinstance(content, dict):
                text = str(content.get("text", ""))
        return {"role": "assistant", "content": {"type": "text", "text": f"sample:{text}"}, "stopReason": "endTurn"}

    def list_tools(self, timeout_s=8.0):
        return []

    def _shutdown_unlocked(self):
        return None


class McpSamplingTests(unittest.TestCase):
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

    def test_sampling_create_message_validates_and_returns_payload(self):
        result = self.manager.sampling_create_message(
            "fake",
            {"messages": [{"role": "user", "content": {"type": "text", "text": "hello"}}], "maxTokens": 32},
        )
        self.assertIn("content", result)
        self.assertEqual(self.fake_client.sampling_calls, 1)
        self.assertIn("sample:hello", str(result.get("content", {}).get("text", "")))
        with self.assertRaises(self.McpError):
            self.manager.sampling_create_message("fake", "bad-params")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
