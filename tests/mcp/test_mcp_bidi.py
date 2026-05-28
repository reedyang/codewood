import importlib.util
import logging
import tempfile
import unittest
from pathlib import Path
from src.config.app_info import get_app_slug_snake

def _load_mcp_manager_module():
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "agent" / "mcp_manager.py"
    spec = importlib.util.spec_from_file_location(f"{get_app_slug_snake()}_mcp_manager_bidi", str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load mcp_manager module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class McpBidiUnitTests(unittest.TestCase):
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

    def tearDown(self):
        logger = logging.getLogger(f"{get_app_slug_snake()}.mcp")
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

    def test_default_sampling_handler_returns_content(self):
        result = self.manager._dispatch_server_request_to_client(
            "fake",
            "sampling/createMessage",
            {
                "messages": [
                    {"role": "user", "content": {"type": "text", "text": "hello"}},
                ],
                "maxTokens": 12,
            },
        )
        self.assertIn("content", result)
        self.assertIn("client-sampled", str(result.get("content", {}).get("text", "")))
        self.assertIn("hello", str(result.get("content", {}).get("text", "")))

    def test_register_custom_handler(self):
        def _handler(server: str, params):
            return {"ok": f"{server}:{params.get('x', '')}"}

        self.manager.register_client_method_handler("foo/bar", _handler)
        result = self.manager._dispatch_server_request_to_client("s1", "foo/bar", {"x": "y"})
        self.assertEqual(result.get("ok"), "s1:y")

    def test_default_elicitation_handler_returns_accept_values(self):
        result = self.manager._dispatch_server_request_to_client(
            "fake",
            "elicitation/create",
            {
                "title": "Need input",
                "requestedSchema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
                },
            },
        )
        self.assertEqual(result.get("action"), "accept")
        self.assertIn("name", result.get("content", {}))
        self.assertIn("age", result.get("content", {}))

    def test_unknown_method_raises(self):
        with self.assertRaises(self.McpError):
            self.manager._dispatch_server_request_to_client("fake", "unknown/method", {})


if __name__ == "__main__":
    unittest.main()
