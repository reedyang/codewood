"""Unit tests for the ``task_finished`` SSE event.

The desktop host raises a native notification when a genuine task finishes
while the window is hidden. The backend publishes ``task_finished`` exactly
when a REAL user turn completes (never for GUI-internal commands), carrying
the chat name the host displays.
"""

import queue
import threading
import time
import unittest

from cli.server.serve_app import ServeApp


class _RecorderBroadcaster:
    def __init__(self) -> None:
        self.events = []

    def publish(self, event, data=None):
        self.events.append((str(event), data))


class _FakeAgent:
    def __init__(self, session_key: str) -> None:
        self.session_key = session_key

    def _current_session_chat_key(self) -> str:
        return self.session_key

    def _set_persist_workspace_ctx(self, _ctx) -> None:  # pragma: no cover
        pass


def _build_app(
    session_key: str,
    chat_id: str,
    workspace_id: str,
    *,
    busy: bool,
    pending: bool,
    started_at: float | None,
    input_text: str | None,
) -> tuple[ServeApp, _RecorderBroadcaster, object]:
    from cli.server.serve_app import _ChatRuntime

    rt = _ChatRuntime(chat_id, workspace_id)
    rt.busy.set() if busy else rt.busy.clear()
    rt.turn_record_pending = pending
    rt.turn_started_at = started_at
    if input_text is not None:
        rt.input_queue.put(input_text)

    app = ServeApp.__new__(ServeApp)
    app.agent = _FakeAgent(session_key)
    app._runtimes = {session_key: rt}
    app._runtimes_lock = threading.Lock()
    app._bridge = None
    app.broadcaster = _RecorderBroadcaster()
    app._route = lambda **kw: dict(kw)
    app._mark_completed_chat_unread = lambda _rt: None
    app._record_turn_elapsed = lambda _rt: None
    app._state_for_loop_thread = lambda: {
        "chats": [{"id": chat_id, "name": "My Chat"}]
    }
    return app, app.broadcaster, rt


class TaskFinishedEventTests(unittest.TestCase):
    def _published(self, broadcaster: _RecorderBroadcaster, name: str):
        return [d for (e, d) in broadcaster.events if e == name]

    def test_genuine_turn_publishes_task_finished_with_chat_name(self):
        app, broadcaster, _ = _build_app(
            session_key="ws-1::chat-1",
            chat_id="chat-1",
            workspace_id="ws-1",
            busy=True,
            pending=True,
            started_at=time.monotonic() - 5.0,
            input_text="hello",
        )
        returned = app._input_provider()
        self.assertEqual(returned, "hello")
        finished = self._published(broadcaster, "task_finished")
        self.assertEqual(len(finished), 1)
        payload = finished[0]
        self.assertEqual(payload["chatId"], "chat-1")
        self.assertEqual(payload["workspaceId"], "ws-1")
        self.assertEqual(payload["chatName"], "My Chat")
        self.assertGreaterEqual(payload["elapsedSeconds"], 5)
        self.assertLessEqual(payload["elapsedSeconds"], 6)
        # The completion is still a normal loop pass: idle + next turn_start.
        self.assertEqual(
            [e for (e, _d) in broadcaster.events],
            ["idle", "task_finished", "turn_start"],
        )

    def test_internal_command_does_not_publish_task_finished(self):
        # A GUI-internal command turn (pending=False) must not raise a
        # notification even though the loop returned while busy.
        app, broadcaster, _ = _build_app(
            session_key="ws-1::chat-1",
            chat_id="chat-1",
            workspace_id="ws-1",
            busy=True,
            pending=False,
            started_at=time.monotonic() - 5.0,
            input_text="/rename x",
        )
        returned = app._input_provider()
        self.assertEqual(returned, "/rename x")
        self.assertEqual(self._published(broadcaster, "task_finished"), [])
        # Internal slash commands also suppress the turn_start broadcast.
        self.assertEqual(
            [e for (e, _d) in broadcaster.events], ["idle"]
        )

    def test_no_task_finished_when_loop_was_not_busy(self):
        # A parked loop tick (no turn in flight) is not a completion.
        app, broadcaster, _ = _build_app(
            session_key="ws-1::chat-1",
            chat_id="chat-1",
            workspace_id="ws-1",
            busy=False,
            pending=True,
            started_at=time.monotonic() - 5.0,
            input_text="hi",
        )
        app._input_provider()
        self.assertEqual(self._published(broadcaster, "task_finished"), [])

    def test_elapsed_zero_when_turn_never_started(self):
        # ``started`` is one half of the genuine-completion gate: a busy loop
        # tick with no start marker (e.g. an internal command) is not a real
        # finished task, so no notification fires.
        app, broadcaster, _ = _build_app(
            session_key="ws-1::chat-1",
            chat_id="chat-1",
            workspace_id="ws-1",
            busy=True,
            pending=True,
            started_at=None,
            input_text="hello",
        )
        app._input_provider()
        self.assertEqual(self._published(broadcaster, "task_finished"), [])

    def test_chat_name_missing_in_state_falls_back_to_empty(self):
        app, broadcaster, _ = _build_app(
            session_key="ws-1::chat-9",
            chat_id="chat-9",
            workspace_id="ws-1",
            busy=True,
            pending=True,
            started_at=time.monotonic() - 1.0,
            input_text="hi",
        )
        app._state_for_loop_thread = lambda: {"chats": []}
        app._input_provider()
        finished = self._published(broadcaster, "task_finished")
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["chatName"], "")


class ChatNameFromStateTests(unittest.TestCase):
    def test_finds_name_by_id(self):
        state = {
            "chats": [
                {"id": "chat-1", "name": "Alpha"},
                {"id": "chat-2", "name": "Beta"},
            ]
        }
        self.assertEqual(ServeApp._chat_name_from_state(state, "chat-2"), "Beta")

    def test_missing_chat_returns_empty(self):
        state = {"chats": [{"id": "chat-1", "name": "Alpha"}]}
        self.assertEqual(ServeApp._chat_name_from_state(state, "nope"), "")

    def test_non_state_input_returns_empty(self):
        self.assertEqual(ServeApp._chat_name_from_state(None, "chat-1"), "")
        self.assertEqual(ServeApp._chat_name_from_state([], "chat-1"), "")


if __name__ == "__main__":
    unittest.main()
