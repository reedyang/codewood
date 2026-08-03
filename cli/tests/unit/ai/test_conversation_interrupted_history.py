"""The interrupted-task history marker and the shell tool result retrofit.

``_record_conversation_interrupted_history`` persists the marker with
``exclude_from_model_context=True`` so the next user message never carries the
previously cancelled task into the model context, while the TUI banner replay
still sees it. ``_retrofit_last_shell_tool_result_aborted`` fixes the trailing
``role:tool`` message when a task interrupt is consumed at a round boundary
right after a failed shell round: the recorded result must reflect the user
stop instead of a plain command failure.
"""

import json
import sys
import types
import unittest


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from cli.agent import Agent, CONVERSATION_INTERRUPTED_HISTORY_PREFIX


class _FakeSessionMemoryService:
    def __init__(self, agent):
        self.agent = agent

    def append_chat_message(self, role, content, **kwargs):
        msg = {"role": role, "content": content}
        if kwargs.get("exclude_from_model_context"):
            msg["exclude_from_model_context"] = True
        self.agent.conversation_history.append(msg)

    def mark_latest_unanswered_user_message_for_cancel(self):
        return 0


class ConversationInterruptedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.agent = Agent.__new__(Agent)
        self.agent.conversation_history = []
        self.agent.session_memory_service = _FakeSessionMemoryService(self.agent)

    def test_record_interrupted_history_excludes_from_model_context(self):
        self.agent._record_conversation_interrupted_history(
            interrupted_kind="task",
            reason="user_interrupt",
            detail="查看我的codex用量",
        )
        self.assertEqual(len(self.agent.conversation_history), 1)
        msg = self.agent.conversation_history[0]
        # Bookkeeping for the TUI banner replay: never fed back to the model.
        self.assertEqual(msg["role"], "assistant")
        self.assertTrue(msg["exclude_from_model_context"])
        payload = self.agent._parse_conversation_interrupted_history_content(msg["content"])
        self.assertIsNotNone(payload)
        self.assertEqual(payload["interrupted_kind"], "task")
        self.assertEqual(payload["reason"], "user_interrupt")
        self.assertEqual(payload["detail"], "查看我的codex用量")

    def _shell_tool_message(self, success=False, aborted=False, output=""):
        payload = {
            "success": success,
            "error": "Command execution failed, exit code: 1",
            "output": output,
            "return_code": 1,
            "aborted_by_user": aborted,
        }
        return {
            "role": "tool",
            "name": "shell",
            "tool_call_id": "call_1",
            "content": json.dumps(payload, ensure_ascii=False),
        }

    def test_retrofit_marks_trailing_failed_shell_result_aborted(self):
        self.agent.conversation_history = [
            {"role": "user", "content": "查看我的codex用量"},
            self._shell_tool_message(success=False, output=""),
        ]

        ok = self.agent._retrofit_last_shell_tool_result_aborted()

        self.assertTrue(ok)
        msg = self.agent.conversation_history[-1]
        payload = json.loads(msg["content"])
        self.assertTrue(payload["aborted_by_user"])
        self.assertEqual(payload["error"], "Command aborted by user")
        self.assertEqual(payload["output"], "")
        self.assertEqual(payload["return_code"], 1)

    def test_retrofit_noop_when_last_message_is_not_tool(self):
        self.agent.conversation_history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]

        ok = self.agent._retrofit_last_shell_tool_result_aborted()

        self.assertFalse(ok)
        self.assertEqual(self.agent.conversation_history[-1]["content"], "hi")

    def test_retrofit_noop_when_shell_succeeded(self):
        self.agent.conversation_history = [
            {"role": "user", "content": "hello"},
            self._shell_tool_message(success=True),
        ]

        ok = self.agent._retrofit_last_shell_tool_result_aborted()

        self.assertFalse(ok)
        payload = json.loads(self.agent.conversation_history[-1]["content"])
        self.assertFalse(payload["aborted_by_user"])

    def test_retrofit_noop_when_already_aborted(self):
        self.agent.conversation_history = [
            {"role": "user", "content": "hello"},
            self._shell_tool_message(
                success=False,
                aborted=True,
                output="command aborted by user\n",
            ),
        ]

        ok = self.agent._retrofit_last_shell_tool_result_aborted()

        self.assertFalse(ok)
        payload = json.loads(self.agent.conversation_history[-1]["content"])
        self.assertTrue(payload["aborted_by_user"])
        self.assertEqual(payload["output"].count("command aborted by user"), 1)

    def test_handler_order_retrofits_then_records_interrupted_marker(self):
        # Mirrors the runtime-loop KeyboardInterrupt handler: the trailing
        # shell tool result must be retrofitted BEFORE the interrupted marker
        # is recorded (otherwise the marker becomes the last history message
        # and the retrofit guard would not find the tool message).
        self.agent.conversation_history = [
            {"role": "user", "content": "查看我的codex用量"},
            self._shell_tool_message(success=False),
        ]

        self.agent._retrofit_last_shell_tool_result_aborted()
        self.agent._record_conversation_interrupted_history(
            interrupted_kind="task",
            reason="user_interrupt",
            detail="查看我的codex用量",
        )

        tool_msg = self.agent.conversation_history[-2]
        payload = json.loads(tool_msg["content"])
        self.assertTrue(payload["aborted_by_user"])
        marker = self.agent.conversation_history[-1]
        self.assertTrue(marker["exclude_from_model_context"])


if __name__ == "__main__":
    unittest.main()
