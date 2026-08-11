import contextlib
import threading
import unittest
from unittest.mock import patch

from cli.server.serve_app import ServeApp


class _Session:
    def __init__(self, chat_id: str = "") -> None:
        self.active_chat_id = chat_id
        self.reasoning_level = ""
        self.call_provider = ""
        self.call_model_name = ""
        self.call_model_set = False


class _FakeBroadcaster:
    def __init__(self) -> None:
        self.published = []

    def publish(self, event, data) -> None:
        self.published.append((event, data))


class _FakeAgent:
    def __init__(self) -> None:
        self.workspace_id = "ws-1"
        self.provider = "provider"
        self.model_name = "global-model"
        self._chat_state_lock = threading.RLock()
        self._chat_state = {
            "active": "chat-1",
            "chats": [
                {
                    "id": "chat-1",
                    "name": "Chat 1",
                    "model_provider": "provider",
                    "model_name": "old-chat-1",
                    "reasoning_level": "",
                },
                {
                    "id": "chat-2",
                    "name": "Chat 2",
                    "model_provider": "provider",
                    "model_name": "old-chat-2",
                    "reasoning_level": "",
                },
            ],
        }
        self._session_tls = threading.local()
        self._sessions = {}
        self._session_for_key("")

    def _session_for_key(self, key: str) -> _Session:
        sess = self._sessions.get(key)
        if sess is None:
            chat_id = key.split("::", 1)[-1] if key else ""
            sess = _Session(chat_id)
            self._sessions[key] = sess
        return sess

    def _bind_session(self, chat_id: str, workspace_id=None) -> None:
        key = f"{workspace_id}::{chat_id}" if workspace_id else chat_id
        self._session_tls.chat_id = key
        self._session_tls.session = self._session_for_key(key)

    @contextlib.contextmanager
    def _session_scope(self, chat_id: str):
        prev_chat = getattr(self._session_tls, "chat_id", "")
        prev_session = getattr(self._session_tls, "session", None)
        self._bind_session(chat_id)
        try:
            yield
        finally:
            self._session_tls.chat_id = prev_chat
            self._session_tls.session = prev_session

    def _session(self) -> _Session:
        return getattr(self._session_tls, "session", self._session_for_key(""))

    @property
    def active_chat_id(self) -> str:
        return self._session().active_chat_id

    @active_chat_id.setter
    def active_chat_id(self, value: str) -> None:
        self._session().active_chat_id = value

    @property
    def reasoning_level(self) -> str:
        return self._session().reasoning_level

    @reasoning_level.setter
    def reasoning_level(self, value: str) -> None:
        self._session().reasoning_level = value

    def _find_chat_by_id(self, chat_id: str):
        for chat in self._chat_state["chats"]:
            if str(chat.get("id") or "") == chat_id:
                return chat
        return None

    def _find_configured_model_choice(self, selector: str):
        provider, model_name = selector.split("/", 1)
        return {"selector": selector, "provider": provider, "name": model_name, "params": {}}

    def _apply_chat_model_from_entry(self, chat, persist_if_missing=False) -> bool:
        self.provider = str(chat.get("model_provider") or "")
        self.model_name = str(chat.get("model_name") or "")
        self.reasoning_level = str(chat.get("reasoning_level") or "")
        self._pin_session_model()
        return True

    def _pin_session_model(self) -> None:
        sess = self._session()
        sess.call_provider = self.provider
        sess.call_model_name = self.model_name
        sess.call_model_set = True

    def _set_reasoning_effort(self, level: str, save_state: bool = True) -> str:
        self.reasoning_level = str(level or "").strip()
        chat = self._find_chat_by_id(self.active_chat_id)
        if chat is not None:
            chat["reasoning_level"] = self.reasoning_level
        self._pin_session_model()
        return "ok"

    def _handle_model_builtin_command(self, builtin_line: str) -> bool:
        selector = builtin_line[len("/model"):].strip()
        return self._switch_model_by_selector(selector)

    def _switch_model_by_selector(self, selector: str) -> str:
        provider, model_name = selector.split("/", 1)
        self.provider = provider
        self.model_name = model_name
        chat = self._find_chat_by_id(self.active_chat_id)
        if chat is not None:
            chat["model_provider"] = provider
            chat["model_name"] = model_name
        self._pin_session_model()
        return "ok"

    def _save_chat_state(self) -> None:
        return None


def _app():
    class _Stub:
        pass

    stub = _Stub()
    stub.agent = _FakeAgent()
    stub.broadcaster = _FakeBroadcaster()
    stub._token = "test"
    stub._route = lambda **payload: payload
    stub.set_chat_model = getattr(ServeApp, "set_chat_model").__get__(stub, _Stub)
    stub.set_chat_reasoning = getattr(ServeApp, "set_chat_reasoning").__get__(stub, _Stub)
    stub.rename_chat = getattr(ServeApp, "rename_chat").__get__(stub, _Stub)
    stub._session_scope_for_chat = getattr(ServeApp, "_session_scope_for_chat").__get__(stub, _Stub)
    return stub


class ServeAppImmediateModelBindingTests(unittest.TestCase):
    def test_model_switch_updates_target_chat_session(self):
        app = _app()
        app.agent._bind_session("chat-2")
        app.agent._apply_chat_model_from_entry(app.agent._find_chat_by_id("chat-2"))

        with patch("cli.server.serve_app._build_state", return_value={"ok": True}):
            ok = app.set_chat_model("chat-2", "openai/new-model", "ws-1")

        self.assertTrue(ok)
        chat2 = app.agent._find_chat_by_id("chat-2")
        session2 = app.agent._sessions["ws-1::chat-2"]
        self.assertEqual(chat2["model_provider"], "openai")
        self.assertEqual(chat2["model_name"], "new-model")
        self.assertTrue(session2.call_model_set)
        self.assertEqual(session2.call_provider, "openai")
        self.assertEqual(session2.call_model_name, "new-model")

    def test_reasoning_switch_updates_target_chat_session(self):
        app = _app()
        app.agent._bind_session("chat-2")
        app.agent._apply_chat_model_from_entry(app.agent._find_chat_by_id("chat-2"))

        with patch("cli.server.serve_app._build_state", return_value={"ok": True}):
            ok = app.set_chat_reasoning("chat-2", "high", "ws-1")

        self.assertTrue(ok)
        chat2 = app.agent._find_chat_by_id("chat-2")
        session2 = app.agent._sessions["ws-1::chat-2"]
        self.assertEqual(chat2["reasoning_level"], "high")
        self.assertEqual(session2.reasoning_level, "high")
        self.assertTrue(session2.call_model_set)

    def test_rename_chat_persists_exact_name(self):
        app = _app()
        app.agent._bind_session("chat-2")

        with patch("cli.server.serve_app._build_state", return_value={"ok": True}):
            ok = app.rename_chat("chat-2", "My Chat Name", "ws-1")

        self.assertTrue(ok)
        chat2 = app.agent._find_chat_by_id("chat-2")
        self.assertEqual(chat2["name"], "My Chat Name")
        self.assertEqual(chat2["name_source"], "manual")

    def test_rename_chat_keeps_quotes_inside_name(self):
        # The dedicated endpoint must store the raw name verbatim — unlike the
        # old slash-command path, it must never add or strip quote characters.
        app = _app()
        app.agent._bind_session("chat-1")

        with patch("cli.server.serve_app._build_state", return_value={"ok": True}):
            ok = app.rename_chat("chat-1", 'renamed "quoted" chat', "ws-1")

        self.assertTrue(ok)
        chat1 = app.agent._find_chat_by_id("chat-1")
        self.assertEqual(chat1["name"], 'renamed "quoted" chat')

    def test_rename_chat_rejects_empty_name(self):
        app = _app()
        self.assertFalse(app.rename_chat("chat-1", "   ", "ws-1"))
        chat1 = app.agent._find_chat_by_id("chat-1")
        self.assertEqual(chat1["name"], "Chat 1")


if __name__ == "__main__":
    unittest.main()
