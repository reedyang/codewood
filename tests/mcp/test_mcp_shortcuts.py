import sys
import types
import unittest


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from agent.smart_shell_agent import SmartShellAgent
from agent.builtin_slash_commands import WINDOWS_SLASH_BUILTIN_COMMANDS


class McpShortcutCommandTests(unittest.TestCase):
    def setUp(self):
        # Bypass heavy __init__; parser method is pure.
        self.agent = SmartShellAgent.__new__(SmartShellAgent)

    def test_parse_all_valid_mcp_shortcuts(self):
        cases = {
            "mcp reload-config": ("mcp_reload_config", {}),
            "mcp status": ("mcp_status", {}),
            "mcp status-refresh": ("mcp_status_refresh", {}),
            "mcp reconnect windbg": ("mcp_reconnect", {"server": "windbg"}),
            "mcp server-info playwright": ("mcp_server_info", {"server": "playwright"}),
            "mcp list-tools playwright": ("mcp_list_tools", {"server": "playwright"}),
            "mcp list-resources playwright": ("mcp_list_resources", {"server": "playwright"}),
            "mcp list-resource-templates playwright": ("mcp_list_resource_templates", {"server": "playwright"}),
            "mcp list-prompts playwright": ("mcp_list_prompts", {"server": "playwright"}),
            "mcp list-disabled-tools": ("mcp_list_disabled_tools", {}),
            "mcp list-disabled-tools playwright": ("mcp_list_disabled_tools", {"server": "playwright"}),
            "mcp disable-tools playwright browser_click,browser_type": (
                "mcp_disable_tools",
                {"server": "playwright", "tools": ["browser_click", "browser_type"]},
            ),
            "mcp enable-tools playwright browser_click,browser_type": (
                "mcp_enable_tools",
                {"server": "playwright", "tools": ["browser_click", "browser_type"]},
            ),
        }
        for cmd, (expected_tool, expected_args) in cases.items():
            tool, args, err = self.agent._parse_mcp_shortcut_command(cmd)
            self.assertEqual(tool, expected_tool, cmd)
            self.assertEqual(args, expected_args, cmd)
            self.assertIsNone(err, cmd)

    def test_parse_missing_required_args_returns_usage(self):
        bad_cases = [
            "mcp reconnect",
            "mcp server-info",
            "mcp list-tools",
            "mcp list-resources",
            "mcp list-resource-templates",
            "mcp list-prompts",
            "mcp disable-tools",
            "mcp enable-tools",
            "mcp list-disabled-tools a b",
        ]
        for cmd in bad_cases:
            tool, _, err = self.agent._parse_mcp_shortcut_command(cmd)
            self.assertIsNone(tool, cmd)
            self.assertIsInstance(err, str, cmd)
            self.assertTrue(err.startswith("用法:"), f"{cmd} => {err}")

    def test_completion_contains_all_mcp_shortcuts(self):
        expected = [
            "/mcp ",
            "/mcp reload-config",
            "/mcp status",
            "/mcp status-refresh",
            "/mcp reconnect ",
            "/mcp server-info ",
            "/mcp list-tools ",
            "/mcp list-resources ",
            "/mcp list-resource-templates ",
            "/mcp list-prompts ",
            "/mcp list-disabled-tools",
            "/mcp disable-tools ",
            "/mcp enable-tools ",
        ]
        for item in expected:
            self.assertIn(item, WINDOWS_SLASH_BUILTIN_COMMANDS)


if __name__ == "__main__":
    unittest.main()
