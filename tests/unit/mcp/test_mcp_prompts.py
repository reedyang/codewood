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
        self.initialized = False
        self.initialize_calls = 0
        self.initialize_instructions = ""
        self.list_tools_calls = 0
        self.list_prompts_calls = 0
        self.get_prompt_calls = 0

    def initialize(self, timeout_s=8.0):
        self.initialize_calls += 1
        self.initialized = True
        self.initialize_instructions = (
            "CodeGraph MCP instructions\n"
            "Use codegraph_explore first\n"
            "Prefer callers and impact before editing"
        )
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "fake-mcp-server", "version": "1.0.0"},
            "capabilities": {"tools": {}},
            "instructions": self.initialize_instructions,
        }

    def list_tools(self, timeout_s=8.0):
        if not self.initialized:
            self.initialize(timeout_s=timeout_s)
        self.list_tools_calls += 1
        return []

    def list_prompts(self, timeout_s=8.0):
        if not self.initialized:
            self.initialize(timeout_s=timeout_s)
        self.list_prompts_calls += 1
        return [{"name": "summarize_text", "description": "Summarize text"}]

    def get_prompt(self, prompt_name, arguments, timeout_s=20.0):
        self.get_prompt_calls += 1
        return {
            "messages": [
                {"role": "user", "content": {"type": "text", "text": f"{prompt_name}:{arguments.get('text', '')}"}}
            ]
        }

    def _shutdown_unlocked(self):
        self.initialized = False
        return None


class McpPromptsTests(unittest.TestCase):
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

    def test_list_prompts_live_then_cache(self):
        prompts, from_cache = self.manager.list_prompts("fake", use_cache=False)
        self.assertFalse(from_cache)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(self.fake_client.list_prompts_calls, 1)

        prompts_cached, from_cache_cached = self.manager.list_prompts("fake", use_cache=True)
        self.assertTrue(from_cache_cached)
        self.assertEqual(len(prompts_cached), 1)
        self.assertEqual(self.fake_client.list_prompts_calls, 1)

    def test_list_prompts_cache_miss_raises(self):
        with self.assertRaises(self.McpError):
            self.manager.list_prompts("fake", use_cache=True)

    def test_reconnect_warms_prompts_cache(self):
        def _fake_client_factory(server):
            self.manager._clients[server] = self.fake_client
            return self.fake_client

        self.manager._client = _fake_client_factory

        tools = self.manager.reconnect_server("fake", timeout_s=1.0)

        self.assertEqual(tools, [])
        self.assertEqual(self.fake_client.initialize_calls, 1)
        self.assertEqual(self.fake_client.list_prompts_calls, 1)
        self.assertIn("fake", self.manager._prompts_cache)

        prompts, from_cache = self.manager.list_prompts("fake", use_cache=True)
        self.assertTrue(from_cache)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(self.fake_client.list_prompts_calls, 1)
        instructions_text = self.manager.cached_initialize_instructions_for_prompt()
        self.assertIn("fake", instructions_text)
        self.assertIn("Use codegraph_explore first", instructions_text)
        self.assertIn("Prefer callers and impact before editing", instructions_text)

    def test_get_prompt_validates_and_returns_payload(self):
        result = self.manager.get_prompt("fake", "summarize_text", {"text": "abc"})
        self.assertIn("messages", result)
        self.assertEqual(self.fake_client.get_prompt_calls, 1)
        with self.assertRaises(self.McpError):
            self.manager.get_prompt("fake", "", {"text": "x"})
        with self.assertRaises(self.McpError):
            self.manager.get_prompt("fake", "summarize_text", "bad-args")  # type: ignore[arg-type]

    def test_cached_prompts_for_prompt_contains_entries(self):
        self.manager.list_prompts("fake", use_cache=False)
        prompt_text = self.manager.cached_prompts_for_prompt()
        self.assertIn("fake", prompt_text)
        self.assertIn("summarize_text", prompt_text)

    def test_cached_initialize_instructions_for_prompt_empty_when_no_initialized_clients(self):
        self.fake_client.initialized = False
        self.fake_client.initialize_instructions = ""
        self.manager._clients["fake"] = self.fake_client
        text = self.manager.cached_initialize_instructions_for_prompt()
        self.assertIn("No cached MCP initialize instructions yet", text)


if __name__ == "__main__":
    unittest.main()



