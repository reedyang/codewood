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

    def test_cancel_detection_does_not_scan_success_output_text(self):
        result = {
            "success": True,
            "output": "这里是文件内容，包含关键词：用户取消，但这不是取消操作。",
        }
        self.assertFalse(self.agent._result_indicates_user_cancelled(result))

    def test_cancel_detection_uses_error_message_on_failure(self):
        result = {"success": False, "error": "用户取消了操作"}
        self.assertTrue(self.agent._result_indicates_user_cancelled(result))

    def test_start_chat_task_refreshes_context_usage_snapshot(self):
        class _FakeChatStateManager:
            def start_task(
                self,
                chat_id,
                root_user_input,
                domains,
                classifier=None,
                switched_from_task_id="",
            ):
                _ = (chat_id, root_user_input, domains, classifier, switched_from_task_id)
                return "task-2"

        class _FakeSessionMemoryService:
            def __init__(self):
                self.calls = []

            def schedule_context_usage_refresh_async(self, user_input_hint="", context_hint=""):
                self.calls.append(
                    {
                        "user_input_hint": str(user_input_hint or ""),
                        "context_hint": str(context_hint or ""),
                    }
                )
                return True

        refresh_calls = []
        self.agent.active_chat_id = "chat-1"
        self.agent._chat_state_manager = _FakeChatStateManager()
        self.agent._active_runtime_task_id = ""
        self.agent._active_runtime_task_domains = []
        self.agent.session_memory_service = _FakeSessionMemoryService()
        self.agent._refresh_status_context_usage_snapshot = (
            lambda user_input_hint="", context_hint="": refresh_calls.append(
                {
                    "user_input_hint": str(user_input_hint or ""),
                    "context_hint": str(context_hint or ""),
                }
            )
        )

        task_id = self.agent._start_chat_task(
            root_user_input="new task input",
            domains=["data_analysis"],
            classifier={"primary_domain": "data_analysis", "domains": ["data_analysis"]},
        )

        self.assertEqual(task_id, "task-2")
        self.assertEqual(self.agent._active_runtime_task_id, "task-2")
        self.assertEqual(self.agent._active_runtime_task_domains, ["data_analysis"])
        self.assertEqual(len(refresh_calls), 1)
        self.assertEqual(refresh_calls[0]["context_hint"], "task started")
        self.assertEqual(len(self.agent.session_memory_service.calls), 1)
        self.assertEqual(self.agent.session_memory_service.calls[0]["context_hint"], "task started")

    def test_clear_active_chat_context_resets_usage_and_persists(self):
        class _FakeChatStateManager:
            def __init__(self):
                self.cleared = []

            def clear_chat_context_and_tasks(self, chat_id):
                self.cleared.append(str(chat_id or ""))
                return True

        persisted = {"n": 0}
        self.agent.active_chat_id = "chat-1"
        self.agent.conversation_history = [{"role": "user", "content": "x"}]
        self.agent.operation_results = [{"ok": True}]
        self.agent._last_auto_removed_ephemeral = {"a": 1}
        self.agent._session_summary_llm = "abc"
        self.agent._session_summary_rolling = "def"
        self.agent._last_llm_summary_pair_count = 3
        self.agent._active_runtime_task_id = "task-1"
        self.agent._active_runtime_task_domains = ["software_development"]
        self.agent._last_context_usage_percent = 34
        self.agent._last_context_input_tokens = 1234
        self.agent._chat_state_manager = _FakeChatStateManager()
        self.agent._persist_active_chat_usage_snapshot = lambda: persisted.__setitem__("n", persisted["n"] + 1)

        self.agent._clear_active_chat_context_and_tasks()

        self.assertEqual(self.agent.conversation_history, [])
        self.assertEqual(self.agent.operation_results, [])
        self.assertEqual(self.agent._active_runtime_task_id, "")
        self.assertEqual(self.agent._active_runtime_task_domains, [])
        self.assertEqual(self.agent._last_context_usage_percent, 0)
        self.assertEqual(self.agent._last_context_input_tokens, 0)
        self.assertEqual(self.agent._chat_state_manager.cleared, ["chat-1"])
        self.assertEqual(persisted["n"], 1)


if __name__ == "__main__":
    unittest.main()
