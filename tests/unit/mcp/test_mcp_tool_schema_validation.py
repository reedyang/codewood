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
        self.call_count = 0

    def call_tool(self, tool_name, arguments, timeout_s=20.0):
        self.call_count += 1
        return {"ok": True, "tool": tool_name, "arguments": arguments}

    def list_tools(self, timeout_s=8.0):
        return []

    def _shutdown_unlocked(self):
        return None


class ToolSchemaValidationTests(unittest.TestCase):
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
                    "name": "sum",
                    "inputSchema": {
                        "type": "object",
                        "required": ["a", "b"],
                        "properties": {
                            "a": {"type": "number"},
                            "b": {"type": "number"},
                            "label": {"type": "string"},
                        },
                        "additionalProperties": False,
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

    def test_call_tool_valid_arguments_pass(self):
        result = self.manager.call_tool("fake", "sum", {"a": 1, "b": 2, "label": "x"})
        self.assertTrue(result.get("ok"))
        self.assertEqual(self.client.call_count, 1)

    def test_call_tool_missing_required_is_blocked_locally(self):
        with self.assertRaises(self.McpError):
            self.manager.call_tool("fake", "sum", {"a": 1})
        self.assertEqual(self.client.call_count, 0)

    def test_call_tool_wrong_type_is_blocked_locally(self):
        with self.assertRaises(self.McpError):
            self.manager.call_tool("fake", "sum", {"a": "1", "b": 2})
        self.assertEqual(self.client.call_count, 0)

    def test_call_tool_additional_property_is_blocked_locally(self):
        with self.assertRaises(self.McpError):
            self.manager.call_tool("fake", "sum", {"a": 1, "b": 2, "x": 9})
        self.assertEqual(self.client.call_count, 0)


if __name__ == "__main__":
    unittest.main()



