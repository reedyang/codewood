import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cli.agent import Agent
from cli.managers.chat_state_manager import CHAT_STATE_VERSION
from cli.server.serve_app import ServeApp, _ChatRuntime


class _FakeBroadcaster:
    def __init__(self) -> None:
        self.published = []

    def publish(self, event, data) -> None:
        self.published.append((event, data))


def _build_app(workspace: Path):
    agent = Agent(
        model_name="gpt-4.1",
        work_directory=str(workspace),
        provider="openai",
        config_dir=str(workspace / "cfg"),
    )
    mgr = agent._chat_state_manager
    c1 = mgr.new_chat_entry("chat-1", name="Alpha")
    c2 = mgr.new_chat_entry("chat-2", name="Beta")
    agent._chat_state = {"version": CHAT_STATE_VERSION, "active": "chat-1", "chats": [c1, c2]}
    mgr.save_chat_state()

    app = ServeApp.__new__(ServeApp)
    app.agent = agent
    app._ws_persist_lock = threading.Lock()
    app._ws_persist_ctx = {}
    app._runtimes = {}
    app._runtimes_lock = threading.Lock()
    app._focus_key = ""
    app._focus_left_at = {}
    app._focus_left_busy = {}
    app._focus_track_lock = threading.Lock()
    app.broadcaster = _FakeBroadcaster()
    app._confirms_lock = threading.Lock()
    app._request_user_input_lock = threading.Lock()
    app._browser_cmds_lock = threading.Lock()
    return app


class ServeAppUnreadTurnGateTests(unittest.TestCase):
    def _workspace(self):
        td = tempfile.mkdtemp()
        self.addCleanup(self._rmtree, td)
        return Path(td)

    @staticmethod
    def _rmtree(path: str) -> None:
        for _ in range(20):
            try:
                shutil.rmtree(path)
                return
            except PermissionError:
                time.sleep(0.1)

    def _run_provider(self, app, chat_id: str, pending: bool):
        rt = _ChatRuntime(chat_id, "default", str(Path(app.agent.workspace_config_dir)))
        rt.busy.set()
        rt.turn_record_pending = pending
        rt.turn_started_at = time.monotonic() - 5.0
        app._runtimes["default::" + chat_id] = rt
        # User is viewing a DIFFERENT chat (chat-2), so a genuine completion in
        # chat-1 would be marked unread.
        app._focus_key = "default::chat-2"
        app.agent._bind_session(chat_id, "default")
        rt.input_queue.put(None)
        try:
            app._input_provider()
        except SystemExit:
            pass
        return app.agent._chat_state_manager.find_chat_by_id(chat_id)

    def test_internal_command_turn_does_not_mark_unread(self):
        # A GUI-internal command sets busy but is NOT a task (turn_record_pending
        # False); returning to the input provider must NOT mark the chat unread.
        app = _build_app(self._workspace())
        chat = self._run_provider(app, "chat-1", pending=False)
        self.assertNotIn("has_unread", chat)

    def test_genuine_turn_marks_unread(self):
        # A genuine user prompt (turn_record_pending True) finishing in the
        # background marks the chat unread.
        app = _build_app(self._workspace())
        chat = self._run_provider(app, "chat-1", pending=True)
        self.assertTrue(bool(chat.get("has_unread", False)))


if __name__ == "__main__":
    unittest.main()
