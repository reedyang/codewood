"""Per-chat isolation of the tool-round accumulators.

Regression test for the bug where two tasks running concurrently (GUI
serve mode, possibly in different workspaces) leaked one chat's
``_tool_rounds_raw`` entries into another chat's assistant message:
``_accumulated_tool_rounds`` / ``_accumulated_tool_rounds_raw`` /
``_last_tool_issuing_assistant`` were plain Agent attributes shared by every
chat, while ``conversation_history`` was already routed through per-chat
SessionState. The first chat to flush picked up the merged accumulator and
attached it to its own message.
"""

import unittest

from cli.agent import Agent
from cli.runtime.session_state import install_session_properties


def _agent() -> Agent:
    agent = Agent.__new__(Agent)
    install_session_properties(Agent)
    agent.workspace_id = "ws-1"
    agent.workspace_name = "ws-1"
    agent._sync_active_chat_messages = lambda: None
    return agent


class ToolRoundsSessionIsolationTests(unittest.TestCase):
    def test_accumulator_and_issuing_reference_are_per_chat(self):
        agent = _agent()
        msg_a = {"role": "assistant", "content": ""}
        msg_b = {"role": "assistant", "content": ""}

        # chat-a records a tool round (e.g. a read) while its task runs.
        agent._bind_session("chat-a", "ws-1")
        agent.conversation_history = [msg_a]
        agent._last_tool_issuing_assistant = msg_a
        agent._accumulated_tool_rounds = ["round-a"]
        agent._accumulated_tool_rounds_raw = [
            {"tool": "read", "args": {"path": "ws-a/file.py"}, "output": "a"}
        ]

        # chat-b (another workspace's chat, same agent) starts clean: chat-a's
        # pending rounds must be invisible to it.
        agent._bind_session("chat-b", "ws-1")
        self.assertEqual(agent._accumulated_tool_rounds, [])
        self.assertEqual(agent._accumulated_tool_rounds_raw, [])
        self.assertIsNone(agent._last_tool_issuing_assistant)
        agent.conversation_history = [msg_b]
        agent._last_tool_issuing_assistant = msg_b
        agent._accumulated_tool_rounds_raw = [
            {"tool": "console_exec", "args": {"command": "ping"}, "output": "b"}
        ]

        # Back on chat-a: its own rounds are intact; chat-b's never bled in.
        agent._bind_session("chat-a", "ws-1")
        self.assertEqual(agent._accumulated_tool_rounds, ["round-a"])
        self.assertEqual(agent._accumulated_tool_rounds_raw[0]["tool"], "read")
        self.assertIs(agent._last_tool_issuing_assistant, msg_a)

    def test_flush_attaches_only_own_chat_rounds(self):
        agent = _agent()
        msg_a = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-a",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        }
        msg_b = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-b",
                    "type": "function",
                    "function": {"name": "console_exec", "arguments": "{}"},
                }
            ],
        }

        agent._bind_session("chat-a", "ws-1")
        agent.conversation_history = [msg_a]
        agent._last_tool_issuing_assistant = msg_a
        agent._accumulated_tool_rounds_raw = [
            {"tool": "read", "args": {"path": "ws-a/file.py"}, "output": "a"}
        ]

        # chat-b's task flushes first: only ITS round may land on msg_b.
        agent._bind_session("chat-b", "ws-1")
        agent.conversation_history = [msg_b]
        agent._last_tool_issuing_assistant = msg_b
        agent._accumulated_tool_rounds_raw = [
            {"tool": "console_exec", "args": {"command": "ping"}, "output": "b"}
        ]
        agent._flush_tool_rounds()

        self.assertEqual(
            msg_b.get("_tool_rounds_raw"),
            [{"tool": "console_exec", "args": {"command": "ping"}, "output": "b"}],
        )
        self.assertNotIn("_tool_rounds_raw", msg_a)

        # chat-a's pending round is still queued and lands on ITS message.
        agent._bind_session("chat-a", "ws-1")
        agent._flush_tool_rounds()
        self.assertEqual(
            msg_a.get("_tool_rounds_raw"),
            [{"tool": "read", "args": {"path": "ws-a/file.py"}, "output": "a"}],
        )


if __name__ == "__main__":
    unittest.main()
