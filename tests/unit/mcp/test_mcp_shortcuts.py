import sys
import types
import unittest
from pathlib import Path


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.agent import Agent
from src.completion.builtin_slash_commands import SLASH_BUILTIN_COMMANDS


class McpShortcutCommandTests(unittest.TestCase):
    def setUp(self):
        # Bypass heavy __init__; parser method is pure.
        self.agent = Agent.__new__(Agent)

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
        # The parser now returns a deferred-i18n payload (dict with
        # ``key``/``kwargs``) instead of an already-translated string so
        # the controller can resolve the message in the caller's
        # locale. Resolve it through the same helper the runtime uses
        # so we still assert on user-visible text.
        from src.controllers.mcp_shortcut_controller import format_mcp_shortcut_error

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
            self.assertIsNotNone(err, cmd)
            rendered = format_mcp_shortcut_error(self.agent, err)
            self.assertIsInstance(rendered, str, cmd)
            self.assertTrue(rendered.startswith("Usage:"), f"{cmd} => {rendered}")

    def test_completion_contains_all_mcp_shortcuts(self):
        expected = [
            "/mcp/",
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
            self.assertIn(item, SLASH_BUILTIN_COMMANDS)

    def test_completion_contains_memory_commands(self):
        expected = [
            "/memory ",
            "/memory status",
            "/memory stats",
            "/memory list",
            "/memory search ",
            "/memory remember ",
            "/memory delete ",
        ]
        for item in expected:
            self.assertIn(item, SLASH_BUILTIN_COMMANDS)

    def test_completion_contains_model_commands(self):
        expected = [
            "/model",
        ]
        for item in expected:
            self.assertIn(item, SLASH_BUILTIN_COMMANDS)

    def test_completion_contains_compact_command(self):
        self.assertIn("/compact", SLASH_BUILTIN_COMMANDS)


if __name__ == "__main__":
    unittest.main()

