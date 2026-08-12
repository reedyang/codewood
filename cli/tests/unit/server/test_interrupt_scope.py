"""Per-chat interrupt scoping (serve mode).

Regression test for the bug where editing a history message in one (idle)
chat aborted a task that was running in a different chat. The interrupt is
now scoped to the target chat's SessionState and process bucket, so only
that chat's loop thread consumes it.
"""

import types
import time
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
    agent._pause_aborted_process_keys = set()
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
    stub.pause = getattr(ServeApp, "pause").__get__(stub, _Stub)
    stub._resolve_chat_scope = getattr(ServeApp, "_resolve_chat_scope").__get__(stub, _Stub)
    stub._resolve_chat_record = getattr(ServeApp, "_resolve_chat_record").__get__(stub, _Stub)
    stub._publish_idle_if_chat_parked = getattr(
        ServeApp, "_publish_idle_if_chat_parked"
    ).__get__(stub, _Stub)
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
            # The tree kill runs on a temporary daemon thread; wait for it so
            # the assertion below is deterministic.
            deadline = time.time() + 2
            while not terminate.call_args_list and time.time() < deadline:
                time.sleep(0.005)
        self.assertEqual(
            [call.args[0] for call in terminate.call_args_list],
            [proc_b],
            "only chat-b's own subprocess may be terminated",
        )

    def test_interrupt_marks_already_exited_inflight_process_aborted(self):
        agent = _agent()
        # The subprocess exited on its own (exit code 1) but its shell tool
        # call is still in flight: it stays registered until the round ends.
        proc = types.SimpleNamespace(poll=lambda: 1, pid=3333)
        agent._register_interruptible_process(proc, "ws-1::chat-a")

        agent._request_chat_interrupt("chat-a", "ws-1")

        self.assertTrue(
            agent._consume_process_aborted(proc),
            "an interrupt landing while the tool call is still in flight must "
            "record the result as user-aborted, not a plain command failure",
        )

    def test_tool_round_keeps_pause_notice_instead_of_cancelled_text(self):
        from cli.agent import Agent
        from cli.tools.shell import SHELL_PAUSE_ABORT_NOTICE

        class _Stub(Agent):
            def __init__(self):
                self.conversation_history = [
                    {
                        "role": "assistant",
                        "content": '{"tool_calls":[]}',
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "shell", "arguments": "{}"},
                            }
                        ],
                    }
                ]
                self._accumulated_tool_rounds = []
                self._accumulated_tool_rounds_raw = []
                self._last_tool_issuing_assistant = self.conversation_history[0]
                self.sync_calls = 0

            def _sync_active_chat_messages(self):
                self.sync_calls += 1

            def _format_tool_call_feedback_line(
                self, tool_name, args, failed=False, is_add_file=None, background=False
            ):
                return f"tool={tool_name} failed={failed}"

            def _ui_language(self):
                return "en"

        agent = _Stub()
        for name in (
            "_next_tool_call_id",
            "_record_model_tool_execution_history",
        ):
            setattr(agent, name, getattr(Agent, name).__get__(agent, _Stub))
        agent._extract_live_tool_round_suffix = Agent._extract_live_tool_round_suffix
        agent._extract_tool_result_output = Agent._extract_tool_result_output

        # A message-jump PAUSE: the tool step must keep the supplement notice.
        agent._record_model_tool_execution_history(
            "shell",
            {"command": "sleep 100"},
            {
                "success": False,
                "error": "Command aborted by user",
                "output": "partial line\n" + SHELL_PAUSE_ABORT_NOTICE,
                "aborted_by_user": True,
                "pause_interrupt": True,
                "return_code": 130,
            },
        )
        raw = agent._accumulated_tool_rounds_raw[0]
        self.assertIn(SHELL_PAUSE_ABORT_NOTICE.strip(), raw["output"])
        self.assertNotIn("Cancelled by user", raw["output"])

        # A plain user cancel (no pause marker) still shows the localized text.
        agent._accumulated_tool_rounds_raw = []
        agent.conversation_history = []
        agent._record_model_tool_execution_history(
            "shell",
            {"command": "sleep 100"},
            {
                "success": False,
                "error": "Command aborted by user",
                "output": "partial line\ncommand aborted by user\n",
                "aborted_by_user": True,
                "return_code": 130,
            },
        )
        raw = agent._accumulated_tool_rounds_raw[0]
        self.assertIn("Cancelled by user", raw["output"])

    def test_pause_sets_consumeable_pause_flag_but_stop_does_not(self):
        agent = _agent()
        agent._request_chat_pause("chat-b", "ws-1")
        sess_b = agent._session_for_key("ws-1::chat-b")
        self.assertTrue(
            sess_b.pause_interrupt_requested,
            "a pause must flag the session so the runtime loop skips the "
            "[CONVERSATION_INTERRUPTED] marker",
        )
        # The bound loop thread consumes the flag exactly once.
        agent._bind_session("chat-b", "ws-1")
        self.assertTrue(agent._consume_chat_pause_interrupt_requested())
        self.assertFalse(agent._consume_chat_pause_interrupt_requested())

        # A plain Stop (interrupt) never flags the pause marker.
        agent._request_chat_interrupt("chat-a", "ws-1")
        self.assertFalse(
            agent._session_for_key("ws-1::chat-a").pause_interrupt_requested,
            "a regular interrupt must not be treated as a pause",
        )

    def test_chat_pause_scoped_to_target_chat_and_marks_pause_reason(self):
        from cli.tools.shell import (
            SHELL_CANCEL_ABORT_NOTICE,
            SHELL_PAUSE_ABORT_NOTICE,
            _shell_abort_notice,
        )

        agent = _agent()
        proc_a = types.SimpleNamespace(poll=lambda: None, pid=1111)
        proc_b = types.SimpleNamespace(poll=lambda: None, pid=2222)
        agent._register_interruptible_process(proc_a, "ws-1::chat-a")
        agent._register_interruptible_process(proc_b, "ws-1::chat-b")

        with __import__("unittest.mock").mock.patch.object(
            agent, "_terminate_single_process_tree", return_value=True
        ) as terminate:
            agent._request_chat_pause("chat-b", "ws-1")
            deadline = time.time() + 2
            while not terminate.call_args_list and time.time() < deadline:
                time.sleep(0.005)

        # Only chat-b's session is flagged; chat-a stays untouched.
        self.assertFalse(
            agent._session_for_key("ws-1::chat-a").task_interrupt_requested,
            "a pause requested for chat-b must not flag chat-a",
        )
        self.assertTrue(
            agent._session_for_key("ws-1::chat-b").task_interrupt_requested
        )

        # Only chat-b's subprocess was terminated.
        self.assertEqual(
            [call.args[0] for call in terminate.call_args_list],
            [proc_b],
            "only chat-b's own subprocess may be terminated by a pause",
        )

        # chat-b's process carries the pause reason; chat-a's has none.
        self.assertTrue(agent._consume_process_aborted(proc_b))
        self.assertTrue(
            agent._consume_process_pause(proc_b),
            "a pause must mark the aborted process so the shell reports the "
            "supplement intent instead of a plain cancel",
        )
        self.assertFalse(agent._consume_process_aborted(proc_a))
        self.assertFalse(agent._consume_process_pause(proc_a))

        # The shell notice for a pause reports the supplement intent.
        agent._mark_process_paused(proc_b)
        self.assertEqual(
            _shell_abort_notice(agent, proc_b), SHELL_PAUSE_ABORT_NOTICE
        )
        # Consumed once: a second notice falls back to the cancel marker.
        self.assertEqual(
            _shell_abort_notice(agent, proc_b),
            SHELL_CANCEL_ABORT_NOTICE,
        )

    def test_shell_abort_notice_defaults_to_cancel_marker(self):
        from cli.tools.shell import SHELL_CANCEL_ABORT_NOTICE, _shell_abort_notice

        agent = _agent()
        proc = types.SimpleNamespace(poll=lambda: 1, pid=4444)
        agent._register_interruptible_process(proc, "ws-1::chat-a")
        agent._mark_process_aborted(proc)

        self.assertEqual(
            _shell_abort_notice(agent, proc), SHELL_CANCEL_ABORT_NOTICE
        )
        # The pause mark is consumed exactly once per process.
        agent._mark_process_paused(proc)
        self.assertTrue(agent._consume_process_pause(proc))
        self.assertFalse(agent._consume_process_pause(proc))

    def test_serveapp_pause_with_chat_id_does_not_set_global_flag(self):
        agent = _agent()
        app = _app(agent)
        app.pause(chat_id="chat-b", workspace_id="ws-1")
        self.assertFalse(
            agent._task_interrupt_requested,
            "a chat-scoped pause must not set the agent-global flag",
        )
        self.assertTrue(
            agent._session_for_key("ws-1::chat-b").task_interrupt_requested
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

    def test_serveapp_interrupt_parked_chat_publishes_idle(self):
        agent = _agent()
        app = _app(agent)
        app._chat_is_busy = lambda *a, **k: False
        app.state = lambda: {"chats": []}

        app.interrupt(chat_id="chat-b", workspace_id="ws-1")

        self.assertTrue(
            agent._session_for_key("ws-1::chat-b").task_interrupt_requested
        )
        self.assertEqual(
            [(event, data.get("chat_id")) for event, data in app.broadcaster.published],
            [("idle", "chat-b")],
            "stopping a parked chat must publish an idle snapshot so a stale "
            "GUI live turn settles instead of spinning forever",
        )

    def test_serveapp_interrupt_busy_chat_skips_parked_idle(self):
        agent = _agent()
        app = _app(agent)
        app._chat_is_busy = lambda *a, **k: True
        app.state = lambda: {"chats": []}

        app.interrupt(chat_id="chat-b", workspace_id="ws-1")

        self.assertTrue(
            agent._session_for_key("ws-1::chat-b").task_interrupt_requested
        )
        self.assertEqual(
            app.broadcaster.published,
            [],
            "a busy chat's loop emits the terminal idle itself; no snapshot "
            "should be published here",
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
