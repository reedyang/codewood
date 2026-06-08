import sys
import types
import unittest


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.agent import Agent


class TaskControlToolTests(unittest.TestCase):
    def setUp(self):
        # Bypass heavy init; these tool branches are pure.
        self.agent = Agent.__new__(Agent)
        self.agent.skills = []

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

    def test_task_changed_tool_is_no_longer_registered(self):
        result = self.agent.execute_tool_call(
            "task_changed",
            {"new_task": "Generate release notes", "reason": "user changed request"},
        )
        self.assertFalse(result.get("success", True))

    def test_done_tool_is_no_longer_registered(self):
        result = self.agent.execute_tool_call("done", {})
        self.assertFalse(result.get("success", True))

    def test_cancel_detection_does_not_scan_success_output_text(self):
        result = {
            "success": True,
            "output": "This is file content and contains the keyword: user cancelled, but this is not a cancellation.",
        }
        self.assertFalse(self.agent._result_indicates_user_cancelled(result))

    def test_cancel_detection_uses_error_message_on_failure(self):
        result = {"success": False, "error": "The operation was cancelled by the user"}
        self.assertTrue(self.agent._result_indicates_user_cancelled(result))


if __name__ == "__main__":
    unittest.main()
