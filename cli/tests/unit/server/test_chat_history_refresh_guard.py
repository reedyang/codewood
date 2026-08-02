import contextlib
import threading
import time
import unittest
from unittest.mock import patch

from cli.server.serve_app import ServeApp


class _Runtime:
    def __init__(self, chat_id: str, workspace_id: str):
        self.chat_id = chat_id
        self.workspace_id = workspace_id
        self.busy = threading.Event()
        self.idle_since = None


class _FakeAgent:
    def __init__(self, workspace_id: str = "ws-1"):
        self.workspace_id = workspace_id
        self.refresh_calls = []
        self.activate_calls = []

    def _refresh_chat_record_from_disk(self, chat_id: str) -> bool:
        self.refresh_calls.append(chat_id)
        return True

    def _activate_chat(self, chat_id: str, **kwargs) -> None:
        self.activate_calls.append((chat_id, kwargs))


def _make_app(agent=None):
    app = ServeApp.__new__(ServeApp)
    app.agent = agent or _FakeAgent()
    app._runtimes = {}
    app._runtimes_lock = threading.Lock()
    return app


class ChatRuntimeRecentlyFinishedTests(unittest.TestCase):
    def test_no_runtime_returns_false(self):
        app = _make_app()
        self.assertFalse(app._chat_runtime_recently_finished("chat-1", "ws-1"))

    def test_runtime_without_idle_since_returns_false(self):
        app = _make_app()
        rt = _Runtime("chat-1", "ws-1")
        app._runtimes["ws-1::chat-1"] = rt
        self.assertFalse(app._chat_runtime_recently_finished("chat-1", "ws-1"))

    def test_just_finished_returns_true(self):
        app = _make_app()
        rt = _Runtime("chat-1", "ws-1")
        rt.idle_since = time.monotonic() - 0.5
        app._runtimes["ws-1::chat-1"] = rt
        self.assertTrue(app._chat_runtime_recently_finished("chat-1", "ws-1"))

    def test_long_parked_returns_false(self):
        app = _make_app()
        rt = _Runtime("chat-1", "ws-1")
        rt.idle_since = time.monotonic() - 10.0
        app._runtimes["ws-1::chat-1"] = rt
        self.assertFalse(app._chat_runtime_recently_finished("chat-1", "ws-1"))


class ChatHistoryRefreshGuardTests(unittest.TestCase):
    def _stub_chat_history(self, recent_finish: bool, busy: bool = False):
        agent = _FakeAgent()
        app = _make_app(agent)
        app._resolve_chat_scope = lambda chat_id, workspace_id: ("chat-1", "ws-1")
        app._chat_is_busy = lambda chat_id, workspace_id: busy
        app._chat_runtime_recently_finished = (
            lambda chat_id, workspace_id: recent_finish
        )
        app._session_scope_for_chat = lambda chat_id, workspace_id="": (
            contextlib.nullcontext()
        )
        return app, agent

    def test_just_finished_turn_skips_disk_refresh(self):
        app, agent = self._stub_chat_history(recent_finish=True)
        with patch(
            "cli.server.serve_app._build_structured_turns", return_value=[]
        ):
            result = app.chat_history(None, 12, "chat-1", "ws-1")
        self.assertEqual(agent.refresh_calls, [])
        self.assertEqual(agent.activate_calls, [])
        self.assertEqual(result["total"], 0)

    def test_parked_runtime_still_refreshes_from_disk(self):
        app, agent = self._stub_chat_history(recent_finish=False)
        with patch(
            "cli.server.serve_app._build_structured_turns", return_value=[]
        ):
            app.chat_history(None, 12, "chat-1", "ws-1")
        self.assertEqual(agent.refresh_calls, ["chat-1"])
        self.assertEqual(len(agent.activate_calls), 1)
        self.assertEqual(agent.activate_calls[0][0], "chat-1")
        self.assertFalse(agent.activate_calls[0][1].get("persist", True))

    def test_busy_turn_never_refreshes(self):
        app, agent = self._stub_chat_history(recent_finish=False, busy=True)
        with patch(
            "cli.server.serve_app._build_structured_turns", return_value=[]
        ):
            app.chat_history(None, 12, "chat-1", "ws-1")
        self.assertEqual(agent.refresh_calls, [])
        self.assertEqual(agent.activate_calls, [])

    def test_different_workspace_skips_refresh(self):
        agent = _FakeAgent(workspace_id="ws-2")
        app = _make_app(agent)
        app._resolve_chat_scope = lambda chat_id, workspace_id: ("chat-1", "ws-1")
        app._chat_is_busy = lambda chat_id, workspace_id: False
        app._chat_runtime_recently_finished = (
            lambda chat_id, workspace_id: False
        )
        app._session_scope_for_chat = lambda chat_id, workspace_id="": (
            contextlib.nullcontext()
        )
        with patch(
            "cli.server.serve_app._build_structured_turns", return_value=[]
        ):
            app.chat_history(None, 12, "chat-1", "ws-1")
        self.assertEqual(agent.refresh_calls, [])
        self.assertEqual(agent.activate_calls, [])


if __name__ == "__main__":
    unittest.main()