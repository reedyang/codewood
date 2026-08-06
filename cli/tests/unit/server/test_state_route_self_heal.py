"""The ``_route`` state/envelope workspace-id self-heal.

A ``state`` payload must be self-consistent with its SSE envelope: the
renderer routes by ``workspaceId`` and caches the snapshot's chat list under
that workspace. When a chat record was resolved through a stale/ambient index
while its loop thread's runtime carries a different workspace id, the envelope
can disagree with the snapshot. ``_route`` must then re-tag the envelope to the
snapshot's workspace (the snapshot is authoritative) instead of emitting a
payload that would make same-id chats jump between workspaces in the GUI.
"""

import threading
import unittest
from unittest.mock import patch

from cli.server.serve_app import ServeApp


class _FakeAgent:
    def __init__(self) -> None:
        self.active_chat_id = "chat-1"
        self.workspace_id = "ws-A"

    def _current_session_chat_key(self) -> str:
        return ""


def _stub():
    class _Stub:
        pass

    stub = _Stub()
    stub.agent = _FakeAgent()
    stub._runtimes = {}
    stub._runtimes_lock = threading.Lock()
    stub._runtime_for_thread = lambda: None
    stub._active_chat_id = getattr(ServeApp, "_active_chat_id").__get__(
        stub, _Stub
    )
    stub._active_chat_workspace_id = getattr(
        ServeApp, "_active_chat_workspace_id"
    ).__get__(stub, _Stub)
    stub._route = getattr(ServeApp, "_route").__get__(stub, _Stub)
    return stub


class ServeAppStateRouteSelfHealTests(unittest.TestCase):
    def test_mismatch_reroutes_envelope_to_snapshot_workspace(self):
        app = _stub()
        state = {
            "workspace": {"id": "default", "name": "Default"},
            "chats": [{"id": "chat-1", "name": "查看我本月的codex用量"}],
        }

        with patch("cli.server.serve_app._WORKSPACE_ROUTE_LOGGER") as logger:
            payload = app._route(state=state)

        self.assertEqual(payload["workspaceId"], "default")
        self.assertEqual(payload["chatId"], "chat-1")
        self.assertEqual(payload["state"], state)
        logger.warning.assert_called_once()
        self.assertIn("re-routing envelope", logger.warning.call_args[0][0])

    def test_consistent_envelope_is_untouched(self):
        app = _stub()
        state = {"workspace": {"id": "ws-A"}, "chats": []}

        with patch("cli.server.serve_app._WORKSPACE_ROUTE_LOGGER") as logger:
            payload = app._route(state=state)

        self.assertEqual(payload["workspaceId"], "ws-A")
        logger.warning.assert_not_called()

    def test_no_state_payload_keeps_envelope(self):
        app = _stub()

        with patch("cli.server.serve_app._WORKSPACE_ROUTE_LOGGER") as logger:
            payload = app._route(text="hello")

        self.assertEqual(payload["workspaceId"], "ws-A")
        self.assertEqual(payload["chatId"], "chat-1")
        self.assertEqual(payload["text"], "hello")
        logger.warning.assert_not_called()

    def test_mismatch_with_empty_snapshot_workspace_is_untouched(self):
        app = _stub()
        state = {"workspace": {"id": ""}, "chats": []}

        with patch("cli.server.serve_app._WORKSPACE_ROUTE_LOGGER") as logger:
            payload = app._route(state=state)

        self.assertEqual(payload["workspaceId"], "ws-A")
        logger.warning.assert_not_called()

    def test_envelope_follows_runtime_workspace_when_snapshot_matches(self):
        app = _stub()
        app.agent.workspace_id = "ws-B"
        state = {"workspace": {"id": "ws-B"}, "chats": [{"id": "chat-1"}]}

        with patch("cli.server.serve_app._WORKSPACE_ROUTE_LOGGER") as logger:
            payload = app._route(state=state)

        self.assertEqual(payload["workspaceId"], "ws-B")
        logger.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
