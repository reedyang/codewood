import importlib
import logging
import tempfile
import unittest
from pathlib import Path
from src.config.app_info import get_app_slug_snake

def _load_mcp_manager_module():
    return importlib.import_module("src.integrations.mcp.manager")


class _FakeClient:
    def __init__(self):
        self.batch_calls = 0

    def call_tools_batch(self, calls, timeout_s=30.0, allow_partial_failure=False):
        self.batch_calls += 1
        out = []
        for c in calls:
            if c.get("tool") == "boom":
                if allow_partial_failure:
                    out.append({"ok": False, "error": {"code": -32000, "message": "boom"}})
                else:
                    raise RuntimeError("boom")
            else:
                payload = {"content": [{"type": "text", "text": f"{c.get('tool')}:{c.get('arguments', {}).get('message', '')}"}]}
                out.append({"ok": True, "result": payload} if allow_partial_failure else payload)
        return out

    def list_tools(self, timeout_s=8.0):
        return []

    def _shutdown_unlocked(self):
        return None


class McpBatchJsonRpcTests(unittest.TestCase):
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
        self.client = _FakeClient()
        self.manager._clients["fake"] = self.client
        self.manager._tools_cache["fake"] = {
            "tools": [
                {
                    "name": "echo",
                    "inputSchema": {
                        "type": "object",
                        "required": ["message"],
                        "properties": {"message": {"type": "string"}},
                    },
                }
            ],
            "ts": 0,
            "source": "test",
        }

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

    def test_call_tools_batch_success(self):
        result = self.manager.call_tools_batch(
            "fake",
            [
                {"tool": "echo", "arguments": {"message": "a"}},
                {"tool": "echo", "arguments": {"message": "b"}},
            ],
        )
        self.assertEqual(self.client.batch_calls, 1)
        self.assertEqual(len(result), 2)
        self.assertIn("echo:a", str(result[0].get("content", [{}])[0].get("text", "")))

    def test_call_tools_batch_local_schema_validation(self):
        with self.assertRaises(self.McpError):
            self.manager.call_tools_batch(
                "fake",
                [{"tool": "echo", "arguments": {"message": 1}}],
            )
        self.assertEqual(self.client.batch_calls, 0)

    def test_call_tools_batch_partial_failure_tolerant(self):
        result = self.manager.call_tools_batch(
            "fake",
            [
                {"tool": "echo", "arguments": {"message": "ok"}},
                {"tool": "boom", "arguments": {}},
            ],
            allow_partial_failure=True,
        )
        self.assertEqual(self.client.batch_calls, 1)
        self.assertEqual(len(result), 2)
        self.assertTrue(result[0].get("ok"))
        self.assertIn("echo:ok", str(result[0].get("result", {}).get("content", [{}])[0].get("text", "")))
        self.assertFalse(result[1].get("ok"))
        self.assertIn("boom", str(result[1].get("error", {})))


if __name__ == "__main__":
    unittest.main()



