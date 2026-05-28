import sys
import types
import unittest


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.completion.slash_dynamic_completions import (
    build_mcp_scoped_groups,
    build_mcp_server_commands,
    build_slash_dynamic_rules,
)
from src.completion.builtin_slash_commands import slash_builtin_completions
from src.agent import Agent


class _FakeMcpManager:
    def __init__(self):
        self.mcp_config = {
            "mcpServers": {
                "playwright": {},
                "figma": {},
            }
        }
        self._status = {"playwright": {"state": "success"}}
        self._clients = {"playwright": object()}
        self._tools_cache = {
            "playwright": {
                "tools": [
                    {"name": "browser_click"},
                    {"name": "browser_type"},
                ]
            }
        }
        self._prompts_cache = {
            "playwright": {
                "prompts": [
                    {"name": "summarize_page"},
                ]
            }
        }

    def list_tools(self, server: str, timeout_s: float = 8.0, use_cache: bool = False):
        if server == "playwright":
            return ([{"name": "browser_click"}], None)
        return ([], None)

    def list_prompts(self, server: str, timeout_s: float = 8.0, use_cache: bool = False):
        if server == "playwright":
            return ([{"name": "summarize_page"}], None)
        return ([], None)

    def get_prompt(self, server: str, name: str, args: dict, timeout_s: float = 20.0):
        return {"description": f"{server}:{name}"}


class McpReferenceFormatTests(unittest.TestCase):
    def setUp(self):
        self.agent = Agent.__new__(Agent)
        self.agent.mcp_manager = _FakeMcpManager()

    def test_mcp_server_commands_use_mcp_prefix(self):
        commands = build_mcp_server_commands(self.agent.mcp_manager.mcp_config)
        self.assertIn("/mcp/playwright/", commands)
        self.assertIn("/mcp/figma/", commands)
        self.assertNotIn("/playwright/", commands)

    def test_mcp_scoped_groups_use_mcp_prefix(self):
        groups = build_mcp_scoped_groups(self.agent.mcp_manager)
        by_trigger = {trigger: candidates for trigger, candidates in groups}
        self.assertIn("/mcp/playwright/", by_trigger)
        candidates = by_trigger["/mcp/playwright/"]
        self.assertIn("/mcp/playwright/browser_click", candidates)
        self.assertIn("/mcp/playwright/summarize_page", candidates)

    def test_extract_forced_mcp_reference_supports_new_format(self):
        parsed = self.agent._extract_forced_mcp_reference("/mcp/playwright/browser_click 执行这个工具")
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed["entries"]), 1)
        self.assertEqual(parsed["entries"][0]["server"], "playwright")
        self.assertEqual(parsed["entries"][0]["name"], "browser_click")
        self.assertEqual(parsed["entries"][0]["kind"], "tool")
        self.assertEqual(parsed["rest"], "执行这个工具")

    def test_extract_forced_mcp_reference_rejects_old_format(self):
        parsed = self.agent._extract_forced_mcp_reference("/playwright/browser_click 执行这个工具")
        self.assertIsNone(parsed)

    def test_mcp_server_candidates_are_second_level_dynamic(self):
        self.agent._workspaces_state = {}
        self.agent._get_slash_mcp_scoped_groups = lambda: []
        self.agent._get_configured_model_selectors = lambda: []
        self.agent._get_slash_skill_target_commands = lambda: []
        rules = build_slash_dynamic_rules(
            workspaces_state=self.agent._workspaces_state,
            mcp_config=self.agent.mcp_manager.mcp_config,
            mcp_scoped_groups_provider=self.agent._get_slash_mcp_scoped_groups,
            model_selectors_provider=self.agent._get_configured_model_selectors,
            skill_targets_provider=self.agent._get_slash_skill_target_commands,
            mcp_root_server_commands_provider=self.agent._get_slash_connected_mcp_server_commands,
        )
        mcp_rules = [r for r in rules if r.get("trigger") == "/mcp/"]
        self.assertEqual(len(mcp_rules), 1)
        candidates = mcp_rules[0].get("candidates", [])
        self.assertIn("/mcp/playwright/", candidates)
        self.assertNotIn("/mcp/figma/", candidates)

    def test_first_level_mcp_server_candidates_are_not_injected(self):
        first_layer = self.agent._get_slash_mcp_server_commands()
        self.assertEqual(first_layer, [])

    def test_root_level_completion_does_not_show_mcp_server_items(self):
        delayed_groups = [("/mcp/", ["/mcp/playwright/"])]
        root_out = slash_builtin_completions(
            "/",
            dynamic_commands=[],
            delayed_dynamic_groups=delayed_groups,
        )
        self.assertIn("/mcp/", root_out)
        self.assertNotIn("/mcp/playwright/", root_out)

        mcp_out = slash_builtin_completions(
            "/mcp/",
            dynamic_commands=[],
            delayed_dynamic_groups=delayed_groups,
        )
        self.assertIn("/mcp/playwright/", mcp_out)
        self.assertNotIn("/mcp/figma/", mcp_out)

    def test_connected_mcp_server_commands_include_only_connected_servers(self):
        commands = self.agent._get_slash_connected_mcp_server_commands()
        self.assertIn("/mcp/playwright/", commands)
        self.assertNotIn("/mcp/figma/", commands)


if __name__ == "__main__":
    unittest.main()

