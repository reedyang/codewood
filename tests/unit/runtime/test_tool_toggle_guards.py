import sys
import types
import unittest


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.agent import Agent


class _FakeMcpManager:
    def __init__(self):
        self.mcp_config = {"mcpServers": {"playwright": {}}}

    def get_status(self, log_limit=20):
        _ = log_limit
        return {
            "all_loaded": True,
            "loading_count": 0,
            "servers": {
                "playwright": {
                    "state": "success",
                    "source": "url",
                    "tool_count": 1,
                }
            },
        }

    def list_disabled_tools(self, server=None):
        key = str(server or "playwright")
        return {key: []}

    def list_tools_with_disabled(self, server, timeout_s=8.0, use_cache=True):
        _ = (server, timeout_s, use_cache)
        return ([{"name": "browser_click", "display_name": "browser_click"}], False)

    def list_resources(self, server, timeout_s=8.0, use_cache=True):
        _ = (server, timeout_s, use_cache)
        return ([], False)

    def list_resource_templates(self, server, timeout_s=8.0, use_cache=True):
        _ = (server, timeout_s, use_cache)
        return ([], False)

    def list_prompts(self, server, timeout_s=8.0, use_cache=True):
        _ = (server, timeout_s, use_cache)
        return ([], False)


class ToolToggleGuardsTests(unittest.TestCase):
    def setUp(self):
        self.agent = Agent.__new__(Agent)
        self.agent.skills = []
        self.agent.mcp_tools_enabled = False
        self.agent.mcp_manager = _FakeMcpManager()
        self.agent.system_prompt = ""
        self.agent._compose_system_prompt_snapshot = lambda include_tools=False: ""
        self.agent._mcp_pending_user_input = {}

    def test_mcp_management_tools_still_run_when_hidden_from_model(self):
        result = self.agent.execute_tool_call("mcp_server_info", {"server": "playwright"})
        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("server"), "playwright")
        self.assertIn("info", result)
        self.assertIn("status", result["info"])


if __name__ == "__main__":
    unittest.main()
