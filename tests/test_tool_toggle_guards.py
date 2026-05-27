import sys
import types
import unittest


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.smart_shell_agent import SmartShellAgent


class ToolToggleGuardsTests(unittest.TestCase):
    def setUp(self):
        self.agent = SmartShellAgent.__new__(SmartShellAgent)
        self.agent.skills = []

    def test_mcp_management_tools_blocked_by_default(self):
        self.agent.mcp_tools_enabled = False
        result = self.agent.execute_tool_call("mcp_server_info", {"server": "playwright"})
        self.assertFalse(result.get("success", True))
        self.assertIn("mcp_tools_enabled", str(result.get("error", "")))

    def test_knowledge_maintenance_tools_blocked_by_default(self):
        self.agent.knowledge_tools_enabled = False
        result = self.agent.execute_tool_call("knowledge_sync", {})
        self.assertFalse(result.get("success", True))
        self.assertIn("knowledge_tools_enabled", str(result.get("error", "")))


if __name__ == "__main__":
    unittest.main()
