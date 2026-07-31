import os
import queue
import threading
import unittest
from unittest.mock import Mock, patch

from cli.server.serve_app import ServeApp


def _bind_app(**attrs):
    class _Stub(ServeApp):
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    return _Stub(**attrs)


def _rich_app():
    agent = Mock()
    agent.active_chat_id = "chat-1"
    agent.active_chat_name = "Chat 1"
    agent.workspace_id = "ws-1"
    agent.workspace_name = "Workspace 1"
    agent.execution_policy = "confirmation"
    agent._gui_plain_stream = True

    broadcaster = Mock()
    broadcaster._subscribers = []

    runtime = Mock()
    runtime.chat_id = "chat-1"
    runtime.workspace_id = "ws-1"
    runtime.busy = threading.Event()
    runtime.busy.set()
    runtime.input_queue = queue.Queue()
    runtime.thread = threading.current_thread()
    runtime.turn_started_at = 123.0

    httpd = Mock()
    httpd.server_address = ("127.0.0.1", 8123)

    app = _bind_app(
        agent=agent,
        broadcaster=broadcaster,
        _runtimes_lock=threading.RLock(),
        _runtimes={"ws-1::chat-1": runtime},
        _confirms_lock=threading.Lock(),
        _confirms={},
        _request_user_input_lock=threading.Lock(),
        _request_user_input={},
        _browser_cmds_lock=threading.Lock(),
        _browser_cmds={},
        _httpd=httpd,
        _shutdown_event=threading.Event(),
        _token="test",
    )
    return app


class ServerHealthSubmitInputTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("CODEWOOD_DEBUG")
        os.environ["CODEWOOD_DEBUG"] = "1"
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("CODEWOOD_DEBUG", None)
        else:
            os.environ["CODEWOOD_DEBUG"] = self._env

    def test_server_health_is_swallowed_when_debug_enabled(self):
        app = _bind_app(agent=Mock(), broadcaster=Mock())
        spawn = Mock()
        with patch(
            "cli.server.serve_app.ServeApp._diagnose_server_health"
        ) as diagnose, patch(
            "cli.server.serve_app.ServeApp._get_or_spawn_runtime", spawn
        ):
            app.submit_input("/server-health", chat_id="chat-1")
        diagnose.assert_called_once()
        spawn.assert_not_called()

    def test_server_health_is_swallowed_even_as_prompt(self):
        app = _bind_app(agent=Mock(), broadcaster=Mock())
        spawn = Mock()
        with patch(
            "cli.server.serve_app.ServeApp._diagnose_server_health"
        ) as diagnose, patch(
            "cli.server.serve_app.ServeApp._get_or_spawn_runtime", spawn
        ):
            app.submit_input("/server-health", chat_id="chat-1", as_prompt=True)
        diagnose.assert_called_once()
        spawn.assert_not_called()

    def test_server_health_still_queued_without_debug_env(self):
        os.environ.pop("CODEWOOD_DEBUG", None)
        app = _bind_app(agent=Mock(), broadcaster=Mock())
        spawn = Mock()
        with patch(
            "cli.server.serve_app.ServeApp._get_or_spawn_runtime", spawn
        ):
            app.submit_input("/server-health", chat_id="chat-1")
        spawn.assert_called_once()

    def test_other_messages_ignored_by_diagnostic(self):
        app = _bind_app(agent=Mock(), broadcaster=Mock())
        spawn = Mock()
        with patch(
            "cli.server.serve_app.ServeApp._diagnose_server_health"
        ) as diagnose, patch(
            "cli.server.serve_app.ServeApp._get_or_spawn_runtime", spawn
        ):
            app.submit_input("/hello", chat_id="chat-1")
        diagnose.assert_not_called()
        spawn.assert_called_once()


class ServerHealthDiagnosticDumpTests(unittest.TestCase):
    def test_dump_does_not_raise(self):
        app = _rich_app()
        captured = []

        class _FakeLogger:
            def info(self, *args, **kwargs):
                captured.append(args[0] % args[1:] if args else "")

        with patch(
            "cli.server.serve_app.ServeApp._get_or_spawn_runtime"
        ), patch(
            "cli.core.logging.app_logging.get_logger", return_value=_FakeLogger()
        ):
            app._diagnose_server_health()

        self.assertTrue(captured)
        text = captured[0]
        self.assertIn("[server-health]", text)
        self.assertIn("Thread stacks:", text)
        self.assertIn("SSE subscribers:", text)
        self.assertIn("Chat runtimes:", text)


if __name__ == "__main__":
    unittest.main()
