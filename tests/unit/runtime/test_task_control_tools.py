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

    def test_done_accepts_reviewed_files_metadata(self):
        result = self.agent.execute_tool_call(
            "done",
            {"reviewed_files": ["src/a.py", " docs/b.md ", ""]},
        )
        self.assertTrue(result.get("success"))
        self.assertTrue(result.get("finished"))
        self.assertEqual(result.get("reviewed_files"), ["src/a.py", "docs/b.md"])

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
                switched_from_task_id="",
            ):
                _ = (chat_id, root_user_input, switched_from_task_id)
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
        )

        self.assertEqual(task_id, "task-2")
        self.assertEqual(self.agent._active_runtime_task_id, "task-2")
        self.assertEqual(len(refresh_calls), 1)
        self.assertEqual(refresh_calls[0]["context_hint"], "task started")
        self.assertEqual(len(self.agent.session_memory_service.calls), 1)
        self.assertEqual(self.agent.session_memory_service.calls[0]["context_hint"], "task started")

    def test_start_chat_task_clears_active_task_id_when_creation_fails(self):
        class _FakeChatStateManager:
            def start_task(
                self,
                chat_id,
                root_user_input,
                switched_from_task_id="",
            ):
                _ = (chat_id, root_user_input, switched_from_task_id)
                return ""

        class _FakeSessionMemoryService:
            def schedule_context_usage_refresh_async(self, user_input_hint="", context_hint=""):
                _ = (user_input_hint, context_hint)
                return True

        self.agent.active_chat_id = "chat-1"
        self.agent._chat_state_manager = _FakeChatStateManager()
        self.agent._active_runtime_task_id = "task-old"
        self.agent.session_memory_service = _FakeSessionMemoryService()
        self.agent._refresh_status_context_usage_snapshot = lambda user_input_hint="", context_hint="": None

        task_id = self.agent._start_chat_task(
            root_user_input="new task input",
        )

        self.assertEqual(task_id, "")
        self.assertEqual(self.agent._active_runtime_task_id, "")

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
        self.agent._last_context_usage_percent = 34
        self.agent._last_context_input_tokens = 1234
        self.agent._chat_state_manager = _FakeChatStateManager()
        self.agent._persist_active_chat_usage_snapshot = lambda: persisted.__setitem__("n", persisted["n"] + 1)

        self.agent._clear_active_chat_context_and_tasks()

        self.assertEqual(self.agent.conversation_history, [])
        self.assertEqual(self.agent.operation_results, [])
        self.assertEqual(self.agent._active_runtime_task_id, "")
        self.assertEqual(self.agent._last_context_usage_percent, 0)
        self.assertEqual(self.agent._last_context_input_tokens, 0)
        self.assertEqual(self.agent._chat_state_manager.cleared, ["chat-1"])
        self.assertEqual(persisted["n"], 1)

    def test_close_chat_task_cancelled_marks_unanswered_user_messages(self):
        class _FakeChatStateManager:
            def __init__(self):
                self.calls = []

            def close_task(self, chat_id, task_id, status):
                self.calls.append((str(chat_id or ""), str(task_id or ""), str(status or "")))
                return True

        class _FakeSessionMemoryService:
            def __init__(self):
                self.marked = []

            def mark_cancelled_task_unanswered_user_messages(self, task_id):
                self.marked.append(str(task_id or ""))
                return 1

        self.agent.active_chat_id = "chat-1"
        self.agent._chat_state_manager = _FakeChatStateManager()
        self.agent.session_memory_service = _FakeSessionMemoryService()

        ok = self.agent._close_chat_task("task-9", "cancelled")

        self.assertTrue(ok)
        self.assertEqual(self.agent._chat_state_manager.calls, [("chat-1", "task-9", "cancelled")])
        self.assertEqual(self.agent.session_memory_service.marked, ["task-9"])

    def test_close_chat_task_cancelled_clears_matching_runtime_task(self):
        class _FakeChatStateManager:
            def close_task(self, chat_id, task_id, status):
                _ = (chat_id, task_id, status)
                return True

        class _FakeSessionMemoryService:
            def mark_cancelled_task_unanswered_user_messages(self, task_id):
                _ = task_id
                return 0

            def mark_latest_unanswered_user_message_for_cancel(self):
                return 0

        self.agent.active_chat_id = "chat-1"
        self.agent._chat_state_manager = _FakeChatStateManager()
        self.agent.session_memory_service = _FakeSessionMemoryService()
        self.agent._active_runtime_task_id = "task-9"

        ok = self.agent._close_chat_task("task-9", "cancelled")

        self.assertTrue(ok)
        self.assertEqual(self.agent._active_runtime_task_id, "")

    def test_direct_shell_history_starts_task_without_classification(self):
        start_calls = []
        close_calls = []
        appended = []
        refresh_calls = []
        self.agent._active_runtime_task_id = "task-old"

        class _FakeSessionMemoryService:
            def __init__(self):
                self.calls = []

            def schedule_context_usage_refresh_async(self, **kwargs):
                self.calls.append(dict(kwargs))
                return True

        def _start_chat_task(root_user_input, switched_from_task_id=""):
            start_calls.append(
                {
                    "root_user_input": str(root_user_input or ""),
                    "switched_from_task_id": str(switched_from_task_id or ""),
                }
            )
            self.agent._active_runtime_task_id = "task-direct"
            return "task-direct"

        def _append_chat_message(role, content):
            appended.append(
                {
                    "role": str(role or ""),
                    "content": str(content or ""),
                    "task_id": str(self.agent._active_runtime_task_id or ""),
                }
            )

        def _close_chat_task(task_id, status):
            close_calls.append((str(task_id or ""), str(status or "")))
            if str(task_id or "") == "task-direct":
                self.agent._active_runtime_task_id = ""
            return True

        self.agent._start_chat_task = _start_chat_task
        self.agent._append_chat_message = _append_chat_message
        self.agent._close_chat_task = _close_chat_task
        self.agent.active_chat_id = "chat-1"
        self.agent.session_memory_service = _FakeSessionMemoryService()
        self.agent._refresh_status_context_usage_snapshot = (
            lambda user_input_hint="", context_hint="": refresh_calls.append(
                {
                    "user_input_hint": str(user_input_hint or ""),
                    "context_hint": str(context_hint or ""),
                }
            )
        )

        self.agent._record_direct_shell_execution_history(
            raw_user_command="powershell -Command Get-ChildItem",
            executed_command="powershell -Command Get-ChildItem",
            cwd="D:/ws",
            return_code=1,
            stdout_text="",
            stderr_text="boom",
        )

        self.assertEqual(start_calls[0]["root_user_input"], "powershell -Command Get-ChildItem")
        self.assertEqual(len(appended), 2)
        self.assertTrue(appended[0]["content"].startswith("[DIRECT_SHELL_USER_COMMAND]"))
        self.assertTrue(appended[1]["content"].startswith("[DIRECT_SHELL_RESULT]"))
        self.assertEqual([x["task_id"] for x in appended], ["task-direct", "task-direct"])
        self.assertEqual(close_calls, [("task-direct", "done")])
        self.assertEqual(
            refresh_calls,
            [
                {
                    "user_input_hint": "powershell -Command Get-ChildItem",
                    "context_hint": "direct shell completed",
                }
            ],
        )
        self.assertEqual(
            self.agent.session_memory_service.calls,
            [
                {
                    "user_input_hint": "powershell -Command Get-ChildItem",
                    "context_hint": "direct shell completed",
                    "expected_chat_id": "chat-1",
                }
            ],
        )

    def test_record_aborted_direct_shell_history_skips_slow_task_lifecycle(self):
        appended = []
        start_calls = []
        refresh_calls = []

        class _FakeSessionMemoryService:
            def __init__(self):
                self.calls = []

            def schedule_context_usage_refresh_async(self, **kwargs):
                self.calls.append(dict(kwargs))
                return True

        self.agent._start_chat_task = lambda **kwargs: start_calls.append(dict(kwargs)) or "task-direct"
        self.agent._append_chat_message = lambda role, content: appended.append(
            {"role": str(role or ""), "content": str(content or "")}
        )
        self.agent._refresh_status_context_usage_snapshot = (
            lambda user_input_hint="", context_hint="": refresh_calls.append(
                {
                    "user_input_hint": str(user_input_hint or ""),
                    "context_hint": str(context_hint or ""),
                }
            )
        )
        self.agent.active_chat_id = "chat-1"
        self.agent.session_memory_service = _FakeSessionMemoryService()

        self.agent._record_direct_shell_execution_history(
            raw_user_command="!ping example.com",
            executed_command="ping example.com",
            cwd="D:/ws",
            return_code=130,
            stdout_text="command aborted by user\n",
            stderr_text="",
            aborted_by_user=True,
        )

        self.assertEqual(start_calls, [])
        self.assertEqual(refresh_calls, [])
        self.assertEqual(self.agent.session_memory_service.calls, [])
        self.assertEqual(len(appended), 2)
        self.assertTrue(appended[0]["content"].startswith("[DIRECT_SHELL_USER_COMMAND]"))
        self.assertTrue(appended[1]["content"].startswith("[DIRECT_SHELL_RESULT]"))

    def test_close_chat_task_cancelled_fallback_marks_latest_when_task_mark_misses(self):
        class _FakeChatStateManager:
            def __init__(self):
                self.calls = []

            def close_task(self, chat_id, task_id, status):
                self.calls.append((str(chat_id or ""), str(task_id or ""), str(status or "")))
                return False

        class _FakeSessionMemoryService:
            def __init__(self):
                self.marked = []
                self.fallback_calls = 0

            def mark_cancelled_task_unanswered_user_messages(self, task_id):
                self.marked.append(str(task_id or ""))
                return 0

            def mark_latest_unanswered_user_message_for_cancel(self):
                self.fallback_calls += 1
                return 1

        self.agent.active_chat_id = "chat-1"
        self.agent._chat_state_manager = _FakeChatStateManager()
        self.agent.session_memory_service = _FakeSessionMemoryService()

        ok = self.agent._close_chat_task("task-9", "cancelled")

        self.assertFalse(ok)
        self.assertEqual(self.agent._chat_state_manager.calls, [("chat-1", "task-9", "cancelled")])
        self.assertEqual(self.agent.session_memory_service.marked, ["task-9"])
        self.assertEqual(self.agent.session_memory_service.fallback_calls, 1)

    def test_record_task_interrupted_history_marks_latest_unanswered_user_first(self):
        class _FakeSessionMemoryService:
            def __init__(self):
                self.mark_calls = 0

            def mark_latest_unanswered_user_message_for_cancel(self):
                self.mark_calls += 1
                return 1

        appended = []
        self.agent.session_memory_service = _FakeSessionMemoryService()
        self.agent._append_chat_message = lambda role, content: appended.append((role, content))

        self.agent._record_conversation_interrupted_history(
            interrupted_kind="task",
            reason="user_interrupt",
            detail="pending task",
        )

        self.assertEqual(self.agent.session_memory_service.mark_calls, 1)
        self.assertEqual(len(appended), 1)
        self.assertEqual(appended[0][0], "assistant")
        self.assertIn("[CONVERSATION_INTERRUPTED]", appended[0][1])


if __name__ == "__main__":
    unittest.main()

