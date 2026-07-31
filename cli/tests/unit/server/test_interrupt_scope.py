"""Per-chat interrupt scoping (serve mode).

Regression test for the bug where editing a history message in one (idle)
chat aborted a task that was running in a different chat. The interrupt is
now scoped to the target chat's SessionState and process bucket, so only
that chat's loop thread consumes it.
"""

import types
import unittest

from cli.agent import Agent
from cli.server.serve_app import ServeApp


class _FakeBroadcaster:
    def __init__(self) -> None:
        self.published = []

    def publish(self, event, data) -> None:
        self.published.append((event, data))


def _agent() -> Agent:
    agent = Agent.__new__(Agent)
    agent.workspace_id = "ws-1"
    agent.workspace_name = "ws-1"
    agent._interrupt_state_lock = __import__("threading").RLock()
    agent._interruptible_processes = {}
    agent._task_interrupt_requested = False
    agent._aborted_process_keys = set()
    agent._process_interrupt_requested = False
    agent._conversation_interrupt_banner_recent = False
    agent._conversation_interrupt_banner_recent_at = 0.0
    agent._chat_state = {
        "active": "chat-a",
        "chats": [
            {"id": "chat-a", "name": "Chat A"},
            {"id": "chat-b", "name": "Chat B"},
        ],
    }
    agent._find_chat_by_id = lambda cid: next(
        (c for c in agent._chat_state["chats"] if c.get("id") == cid), None
    )
    # Lazily installs the per-chat session registry on first access.
    agent._session_for_key("")
    return agent


def _app(agent):
    class _Stub:
        pass

    stub = _Stub()
    stub.agent = agent
    stub.broadcaster = _FakeBroadcaster()
    stub._token = "test"
    stub._route = lambda **payload: payload
    stub.interrupt = getattr(ServeApp, "interrupt").__get__(stub, _Stub)
    stub._resolve_chat_scope = getattr(ServeApp, "_resolve_chat_scope").__get__(stub, _Stub)
    stub._resolve_chat_record = getattr(ServeApp, "_resolve_chat_record").__get__(stub, _Stub)
    return stub


class ChatInterruptScopeTests(unittest.TestCase):
    def test_chat_interrupt_only_sets_target_chat_session_flag(self):
        agent = _agent()
        # chat-a is running a task; chat-b is where the user edits a message.
        agent._bind_session("chat-b", "ws-1")

        ok = agent._request_chat_interrupt("chat-b", "ws-1")

        self.assertTrue(ok)
        self.assertFalse(
            agent._session_for_key("ws-1::chat-a").task_interrupt_requested,
            "an interrupt requested for chat-b must not flag chat-a",
        )
        self.assertTrue(
            agent._session_for_key("ws-1::chat-b").task_interrupt_requested
        )

    def test_chat_a_loop_does_not_consume_chat_b_interrupt(self):
        agent = _agent()
        agent._request_chat_interrupt("chat-b", "ws-1")

        # chat-a's loop thread is bound to chat-a's session.
        agent._bind_session("chat-a", "ws-1")
        self.assertFalse(
            agent._consume_task_interrupt_requested(),
            "chat-a's task must not be aborted by chat-b's edit",
        )

    def test_chat_b_loop_consumes_its_own_interrupt(self):
        agent = _agent()
        agent._request_chat_interrupt("chat-b", "ws-1")

        agent._bind_session("chat-b", "ws-1")
        self.assertTrue(agent._consume_task_interrupt_requested())
        # Consumed once: a second poll sees nothing.
        self.assertFalse(agent._consume_task_interrupt_requested())

    def test_interrupt_processes_scoped_to_target_chat(self):
        agent = _agent()
        proc_a = types.SimpleNamespace(poll=lambda: None, pid=1111)
        proc_b = types.SimpleNamespace(poll=lambda: None, pid=2222)
        agent._register_interruptible_process(proc_a, "ws-1::chat-a")
        agent._register_interruptible_process(proc_b, "ws-1::chat-b")

        # Interrupting chat-b must not touch chat-a's subprocess.
        with __import__("unittest.mock").mock.patch.object(
            agent, "_terminate_single_process_tree", return_value=True
        ) as terminate:
            agent._request_chat_interrupt("chat-b", "ws-1")
        self.assertEqual(
            [call.args[0] for call in terminate.call_args_list],
            [proc_b],
            "only chat-b's own subprocess may be terminated",
        )

    def test_serveapp_interrupt_with_chat_id_does_not_set_global_flag(self):
        agent = _agent()
        app = _app(agent)
        app.interrupt(chat_id="chat-b", workspace_id="ws-1")
        self.assertFalse(
            agent._task_interrupt_requested,
            "a chat-scoped interrupt must not set the agent-global flag",
        )
        self.assertTrue(
            agent._session_for_key("ws-1::chat-b").task_interrupt_requested
        )

    def test_serveapp_interrupt_without_chat_id_keeps_legacy_global_path(self):
        agent = _agent()
        app = _app(agent)
        app.interrupt()
        self.assertTrue(agent._task_interrupt_requested)

    def test_resolve_chat_scope_validates_pair_and_falls_back_to_active(self):
        agent = _agent()
        app = _app(agent)

        # Valid pair: resolves unchanged.
        self.assertEqual(app._resolve_chat_scope("chat-b", "ws-1"), ("chat-b", "ws-1"))

        # Unknown chat id in a valid workspace: falls back to the active chat.
        self.assertEqual(app._resolve_chat_scope("nope", "ws-1"), ("chat-a", "ws-1"))

        # No ids: falls back to the active chat + current workspace.
        self.assertEqual(app._resolve_chat_scope("", ""), ("chat-a", "ws-1"))

    def test_file_changes_scope_key_is_workspace_qualified(self):
        from cli.server.serve_app import ServeApp

        self.assertEqual(ServeApp._file_changes_scope_key("chat-1", "ws-1"), "ws-1::chat-1")
        self.assertEqual(ServeApp._file_changes_scope_key("chat-1", ""), "chat-1")
        self.assertNotEqual(
            ServeApp._file_changes_scope_key("chat-1", "ws-1"),
            ServeApp._file_changes_scope_key("chat-1", "ws-2"),
        )


if __name__ == "__main__":
    unittest.main()
