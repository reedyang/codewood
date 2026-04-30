import sys
import types
import unittest


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.smart_shell_agent import SmartShellAgent


class TaskControlToolTests(unittest.TestCase):
    def setUp(self):
        # Bypass heavy init; these tool branches are pure.
        self.agent = SmartShellAgent.__new__(SmartShellAgent)

    def test_ask_more_info_returns_need_user_input_payload(self):
        result = self.agent.execute_tool_call(
            "ask_more_info",
            {"question": "Please provide project name", "expected_fields": ["project_name"]},
        )
        self.assertTrue(result.get("success"))
        self.assertTrue(result.get("needs_user_input"))
        self.assertEqual(result.get("input_type"), "supplement")
        self.assertEqual(result.get("question"), "Please provide project name")
        self.assertEqual(result.get("expected_fields"), ["project_name"])

    def test_task_changed_requires_new_task(self):
        result = self.agent.execute_tool_call("task_changed", {"reason": "irrelevant"})
        self.assertFalse(result.get("success", True))
        self.assertIn("new_task", str(result.get("error", "")))

    def test_task_changed_switch_payload(self):
        result = self.agent.execute_tool_call(
            "task_changed",
            {"new_task": "Generate release notes", "reason": "user changed request"},
        )
        self.assertTrue(result.get("success"))
        self.assertTrue(result.get("task_changed"))
        self.assertEqual(result.get("new_task"), "Generate release notes")
        self.assertEqual(result.get("reason"), "user changed request")


if __name__ == "__main__":
    unittest.main()
