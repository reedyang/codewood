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
        self.list_resources_calls = 0
        self.list_resource_templates_calls = 0
        self.read_resource_calls = 0

    def list_resources(self, timeout_s=8.0):
        self.list_resources_calls += 1
        return [
            {"uri": "file://a.md", "name": "a.md", "description": "resource A"},
            {"uri": "file://b.md", "name": "b.md"},
        ]

    def list_resource_templates(self, timeout_s=8.0):
        self.list_resource_templates_calls += 1
        return [
            {"uriTemplate": "file://{name}.md", "name": "file-template"},
        ]

    def read_resource(self, uri, timeout_s=20.0):
        self.read_resource_calls += 1
        return {
            "contents": [
                {"uri": str(uri), "mimeType": "text/plain", "text": "hello"},
            ]
        }

    def list_tools(self, timeout_s=8.0):
        return []

    def _shutdown_unlocked(self):
        return None


class McpResourcesTests(unittest.TestCase):
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

    def test_list_resources_live_then_cache(self):
        resources, from_cache = self.manager.list_resources("fake", use_cache=False)
        self.assertFalse(from_cache)
        self.assertEqual(len(resources), 2)
        self.assertEqual(self.fake_client.list_resources_calls, 1)

        resources_cached, from_cache_cached = self.manager.list_resources("fake", use_cache=True)
        self.assertTrue(from_cache_cached)
        self.assertEqual(len(resources_cached), 2)
        self.assertEqual(self.fake_client.list_resources_calls, 1)

    def test_list_resources_cache_miss_raises(self):
        with self.assertRaises(self.McpError):
            self.manager.list_resources("fake", use_cache=True)

    def test_read_resource_validates_uri_and_returns_payload(self):
        result = self.manager.read_resource("fake", "file://a.md")
        self.assertIn("contents", result)
        self.assertEqual(self.fake_client.read_resource_calls, 1)

        with self.assertRaises(self.McpError):
            self.manager.read_resource("fake", "")

    def test_list_resource_templates_live_then_cache(self):
        templates, from_cache = self.manager.list_resource_templates("fake", use_cache=False)
        self.assertFalse(from_cache)
        self.assertEqual(len(templates), 1)
        self.assertEqual(self.fake_client.list_resource_templates_calls, 1)

        templates_cached, from_cache_cached = self.manager.list_resource_templates("fake", use_cache=True)
        self.assertTrue(from_cache_cached)
        self.assertEqual(len(templates_cached), 1)
        self.assertEqual(self.fake_client.list_resource_templates_calls, 1)

    def test_cached_resources_for_prompt_contains_entries(self):
        self.manager.list_resources("fake", use_cache=False)
        prompt_text = self.manager.cached_resources_for_prompt()
        self.assertIn("fake", prompt_text)
        self.assertIn("a.md", prompt_text)


if __name__ == "__main__":
    unittest.main()



