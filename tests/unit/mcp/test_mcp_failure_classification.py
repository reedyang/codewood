import importlib
import logging
import tempfile
import unittest
from pathlib import Path
from src.config.app_info import get_app_slug_snake

def _load_mcp_manager_module():
    return importlib.import_module("src.integrations.mcp.manager")


class McpFailureClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mcp_module = _load_mcp_manager_module()
        cls.McpManager = cls.mcp_module.McpManager

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.manager = self.McpManager(
            config_dir=self.config_dir,
            mcp_config={
                "mcpServers": {
                    "fake_stdio": {"command": "python", "args": ["-m", "fake_server"]},
                    "fake_url": {"url": "http://127.0.0.1:18765/mcp", "headers": {}},
                }
            },
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

    def test_classify_failure_32601_as_unsupported(self):
        ft, suggestion = self.manager._classify_failure(
            "fake_url",
            "{'code': -32601, 'message': 'Method not found'}",
        )
        self.assertEqual(ft, "unsupported")
        self.assertIn("error.code", suggestion)

    def test_classify_failure_unknown_tool_as_unsupported(self):
        ft, suggestion = self.manager._classify_failure(
            "fake_stdio",
            "Unknown tool: missing_tool",
        )
        self.assertEqual(ft, "unsupported")
        self.assertIn("tool/method names", suggestion)

    def test_classify_failure_prefers_error_code_over_keywords(self):
        ft, suggestion = self.manager._classify_failure(
            "fake_stdio",
            "WinError 2 and {'code': -32601, 'message': 'Method not found'}",
        )
        self.assertEqual(ft, "unsupported")
        self.assertIn("error.code", suggestion)

    def test_classify_failure_missing_binary_as_missing_dependency(self):
        ft, suggestion = self.manager._classify_failure(
            "fake_stdio",
            "WinError 2: The system cannot find the file specified",
        )
        self.assertEqual(ft, "missing_dependency")
        self.assertIn("Executable `python` was not found", suggestion)


if __name__ == "__main__":
    unittest.main()



