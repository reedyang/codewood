import threading
import time
import unittest
from unittest.mock import patch

from cli.server.serve_app import ServeApp


class _FakeBroadcaster:
    def __init__(self) -> None:
        self.published = []

    def publish(self, event, data) -> None:
        self.published.append((event, data))


class _FakeAgent:
    def __init__(self) -> None:
        self.workspace_id = "ws-1"
        self._chat_state_lock = threading.Lock()
        self._chat_state = {"active": "chat-1"}
        self.active_chat_id = ""
        self.active_chat_name = "New Chat"
        self.saved = 0

    def _resolve_chat_selector(self, chat_id: str):
        if chat_id == "chat-2":
            return {"id": "chat-2", "name": "Running Chat"}
        return None

    def _save_chat_state(self) -> None:
        self.saved += 1


def _app():
    class _Stub:
        pass

    stub = _Stub()
    stub.agent = _FakeAgent()
    stub.broadcaster = _FakeBroadcaster()
    stub._ws_persist_lock = threading.Lock()
    stub._ws_persist_ctx = {}
    stub._route = lambda **payload: payload
    stub._chat_is_busy = lambda chat_id, workspace_id=None: chat_id == "chat-2"
    stub.select_chat = getattr(ServeApp, "select_chat").__get__(stub, _Stub)
    return stub


class ServeAppSelectChatTests(unittest.TestCase):
    def test_select_busy_chat_updates_active_pointers(self):
        app = _app()

        with patch("cli.server.serve_app._build_state", return_value={"ok": True}):
            ok = app.select_chat("chat-2")

        self.assertTrue(ok)
        self.assertEqual(app.agent._chat_state["active"], "chat-2")
        self.assertEqual(app.agent.active_chat_id, "chat-2")
        self.assertEqual(app.agent.active_chat_name, "Running Chat")
        self.assertEqual(app.agent.saved, 1)

        for _ in range(50):
            if app.broadcaster.published:
                break
            time.sleep(0.01)
        self.assertTrue(app.broadcaster.published)


if __name__ == "__main__":
    unittest.main()
