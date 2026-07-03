import importlib
import logging
import tempfile
import unittest
from pathlib import Path

from cli.config.app_info import get_app_slug_snake


def _load_mcp_manager_module():
    return importlib.import_module("cli.integrations.mcp.manager")


class _FakeClient:
    def __init__(self) -> None:
        self.list_tools_calls = 0

    def list_tools(self, timeout_s=8.0):
        _ = timeout_s
        self.list_tools_calls += 1
        return [{"name": "browser_click"}]

    def _shutdown_unlocked(self):
        return None


class McpCacheOnlySettingsTests(unittest.TestCase):
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

    def test_list_tools_with_disabled_cache_miss_does_not_connect(self):
        with self.assertRaises(self.McpError):
            self.manager.list_tools_with_disabled("fake", use_cache=True)
        self.assertEqual(self.fake_client.list_tools_calls, 0)


if __name__ == "__main__":
    unittest.main()
