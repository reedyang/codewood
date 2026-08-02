import threading
import time
import unittest
from unittest.mock import patch

from cli.server.serve_app import ServeApp


class _RecorderAgent:
    def __init__(self, active_chat_id: str, focused_workspace: str):
        self.workspace_id = focused_workspace
        self._chat_state = {"active": active_chat_id}
        self.calls = []

    def _set_chat_unread(self, chat_id: str, unread: bool) -> None:
        self.calls.append((chat_id, bool(unread)))


class _Runtime:
    def __init__(self, chat_id: str, workspace_id: str):
        self.chat_id = chat_id
        self.workspace_id = workspace_id


def _stub(
    active_chat_id: str,
    focused_workspace: str,
    focus_key: str = "",
    left_at: float = 0.0,
    left_busy: bool = False,
):
    app = ServeApp.__new__(ServeApp)
    app.agent = _RecorderAgent(active_chat_id, focused_workspace)
    app._focus_key = focus_key
    app._focus_left_at = {}
    app._focus_left_busy = {}
    if left_at > 0:
        for cid in ("chat-1", "chat-2"):
            key = f"{focused_workspace}::{cid}"
            app._focus_left_at[key] = left_at
            app._focus_left_busy[key] = left_busy
    app._focus_track_lock = threading.Lock()
    return app


class _FakeBroadcaster:
    def publish(self, *args, **kwargs):
        pass


class _NewChatFakeAgent:
    """Minimal agent surface exercised by ``ServeApp.new_chat``."""

    def __init__(self, workspace_id: str = "ws-1"):
        self.workspace_id = workspace_id
        self.active_chat_id = ""
        self._chat_state_lock = threading.Lock()
        self._chat_state: dict = {"active": ""}
        self._entries: list = []
        self.activated: list = []
        self.calls: list = []
        self._next = 0

    def _next_chat_id(self) -> str:
        self._next += 1
        return f"chat-{self._next}"

    def _new_chat_entry(self, cid: str, name: str = "New Chat") -> dict:
        return {"id": cid, "name": name}

    def _chat_entries(self) -> list:
        return self._entries

    def _save_chat_state(self) -> None:
        pass

    def _activate_chat(self, cid: str, **kwargs) -> None:
        self.activated.append(cid)
        self._chat_state["active"] = cid
        self.active_chat_id = cid

    def _current_session_chat_key(self) -> str:
        return ""

    def _set_chat_unread(self, chat_id: str, unread: bool) -> None:
        self.calls.append((chat_id, bool(unread)))


class ServeAppUnreadMarkingTests(unittest.TestCase):
    def test_focused_chat_completion_clears_unread(self):
        # Same workspace, and the completed chat IS the focused chat: the flag
        # is cleared (the user watched it finish). No focus tracking yet, so it
        # falls back to the workspace index's active chat.
        app = _stub(active_chat_id="chat-1", focused_workspace="ws-1")
        app._mark_completed_chat_unread(_Runtime("chat-1", "ws-1"))
        self.assertEqual(app.agent.calls, [("chat-1", False)])

    def test_background_chat_same_workspace_marks_unread(self):
        # Same workspace but a different chat is focused: unread.
        app = _stub(active_chat_id="chat-2", focused_workspace="ws-1")
        app._mark_completed_chat_unread(_Runtime("chat-1", "ws-1"))
        self.assertEqual(app.agent.calls, [("chat-1", True)])

    def test_background_workspace_chat_marks_unread(self):
        # The completed chat lives in a non-focused workspace: always unread,
        # regardless of what the focused workspace considers active.
        app = _stub(active_chat_id="chat-1", focused_workspace="ws-2")
        app._mark_completed_chat_unread(_Runtime("chat-1", "ws-1"))
        self.assertEqual(app.agent.calls, [("chat-1", True)])

    def test_missing_runtime_is_noop(self):
        app = _stub(active_chat_id="chat-1", focused_workspace="ws-1")
        app._mark_completed_chat_unread(None)
        self.assertEqual(app.agent.calls, [])

    def test_focused_workspace_with_no_rt_workspace_clears(self):
        # Runtime has an empty workspace id; treat it as focused so the flag is
        # cleared when its chat is the active one.
        app = _stub(active_chat_id="chat-1", focused_workspace="")
        app._mark_completed_chat_unread(_Runtime("chat-1", ""))
        self.assertEqual(app.agent.calls, [("chat-1", False)])

    def test_user_focus_marker_wins_over_index(self):
        # The user opened chat-2 (focus marker), even if a backend-internal
        # _activate_chat transiently moved the index's active pointer back to
        # chat-1. A chat-1 completion must still be unread.
        app = _stub(active_chat_id="chat-1", focused_workspace="ws-1", focus_key="ws-1::chat-2")
        app._mark_completed_chat_unread(_Runtime("chat-1", "ws-1"))
        self.assertEqual(app.agent.calls, [("chat-1", True)])

    def test_background_completion_right_after_leaving_running_task_marks_unread(self):
        # User left chat-1 WHILE its task was still running (left_busy=True);
        # the task completes a second later -> genuine background completion.
        app = _stub(
            active_chat_id="chat-2",
            focused_workspace="ws-1",
            focus_key="ws-1::chat-2",
            left_at=time.monotonic() - 1.0,
            left_busy=True,
        )
        app._mark_completed_chat_unread(_Runtime("chat-1", "ws-1"))
        self.assertEqual(app.agent.calls, [("chat-1", True)])

    def test_just_left_idle_chat_skips_unread(self):
        # User watched chat-1 finish (it was ALREADY idle when they left ~1s
        # ago); the loop thread processed the completion after the switch. Do
        # NOT mark unread — they saw it complete.
        app = _stub(
            active_chat_id="chat-2",
            focused_workspace="ws-1",
            focus_key="ws-1::chat-2",
            left_at=time.monotonic() - 1.0,
            left_busy=False,
        )
        app._mark_completed_chat_unread(_Runtime("chat-1", "ws-1"))
        self.assertEqual(app.agent.calls, [("chat-1", False)])

    def test_stale_leave_window_marks_unread(self):
        # The user left chat-1 a while ago (10s) and chat-1 completes in the
        # background: unread.
        app = _stub(
            active_chat_id="chat-2",
            focused_workspace="ws-1",
            focus_key="ws-1::chat-2",
            left_at=time.monotonic() - 10.0,
        )
        app._mark_completed_chat_unread(_Runtime("chat-1", "ws-1"))
        self.assertEqual(app.agent.calls, [("chat-1", True)])

    def test_track_focus_records_leave_time_and_busy(self):
        app = _stub(active_chat_id="chat-1", focused_workspace="ws-1")
        app._runtimes = {}
        app._runtimes_lock = threading.Lock()
        app._track_focus("chat-1", "ws-1")
        app._track_focus("chat-2", "ws-1")
        self.assertEqual(app._focus_key, "ws-1::chat-2")
        self.assertIn("ws-1::chat-1", app._focus_left_at)
        self.assertIn("ws-1::chat-1", app._focus_left_busy)
        self.assertFalse(app._focus_left_busy["ws-1::chat-1"])
        self.assertNotIn("ws-1::chat-2", app._focus_left_at)

    def test_new_chat_records_focus_so_first_completion_is_viewed(self):
        # GUI draft flow: the user previously opened chat-1, then materialized
        # a new chat (new_chat) and sent its first message. The new chat must
        # be recorded as the focused chat so its completion while the user is
        # viewing it is NOT flagged unread (regression: the focus marker kept
        # pointing at chat-1 and the brand-new chat got a persistent blue dot).
        agent = _NewChatFakeAgent()
        app = ServeApp.__new__(ServeApp)
        app.agent = agent
        app._runtimes = {}
        app._runtimes_lock = threading.Lock()
        app._focus_key = "ws-1::chat-1"
        app._focus_left_at = {}
        app._focus_left_busy = {}
        app._focus_track_lock = threading.Lock()
        app._ws_persist_lock = threading.Lock()
        app.broadcaster = _FakeBroadcaster()
        app._route = lambda **kw: kw
        with patch("cli.server.serve_app._build_state", return_value={}):
            cid = app.new_chat("", "", "")
        self.assertEqual(cid, "chat-1")
        self.assertEqual(app._focus_key, "ws-1::chat-1")
        # The user watched the new chat's first turn complete -> not unread.
        app._mark_completed_chat_unread(_Runtime("chat-1", "ws-1"))
        self.assertEqual(agent.calls, [("chat-1", False)])

    def test_new_chat_focus_marks_background_completion_unread(self):
        # After materializing the new chat, the user switches away WHILE its
        # task is still running; the completion is a genuine background one and
        # must still be flagged unread.
        agent = _NewChatFakeAgent()
        app = ServeApp.__new__(ServeApp)
        app.agent = agent
        app._runtimes = {}
        app._runtimes_lock = threading.Lock()
        app._focus_key = "ws-1::chat-1"
        app._focus_left_at = {}
        app._focus_left_busy = {}
        app._focus_track_lock = threading.Lock()
        app._ws_persist_lock = threading.Lock()
        app.broadcaster = _FakeBroadcaster()
        app._route = lambda **kw: kw
        with patch("cli.server.serve_app._build_state", return_value={}):
            cid = app.new_chat("", "", "")
        # User switches to chat-2 while the new chat is still running.
        app._track_focus("chat-2", "ws-1")
        with app._focus_track_lock:
            app._focus_left_busy["ws-1::chat-1"] = True
            app._focus_left_at["ws-1::chat-1"] = time.monotonic() - 1.0
        app._mark_completed_chat_unread(_Runtime(cid, "ws-1"))
        self.assertEqual(agent.calls, [("chat-1", True)])


if __name__ == "__main__":
    unittest.main()
