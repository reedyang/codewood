"""Unit tests for the desktop host's task-completion notifier.

The notifier module lives under ``desktop/host`` (not a Python package on the
import path), so it is loaded by file path — mirroring ``test_browser_overlay``.
A fake SSE server, a fake window, and patched native-notification functions
keep the tests deterministic without a GUI or a real display.
"""

import importlib.util
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

_NOTIFIER_PATH = (
    Path(__file__).resolve().parents[4] / "desktop" / "host" / "notifier.py"
)


def _load_notifier_module():
    spec = importlib.util.spec_from_file_location(
        "codewood_test_notifier", str(_NOTIFIER_PATH)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


notifier_mod = _load_notifier_module()
TaskNotifier = notifier_mod.TaskNotifier


class _FakeWindow:
    minimized = False
    native = None
    gtk = None


def _make_notifier(window=None, port=1, token="t"):
    return TaskNotifier(port, token, window)


class HandleTaskFinishedTests(unittest.TestCase):
    def test_window_visible_does_not_notify(self):
        notifier = _make_notifier(window=_FakeWindow())
        with patch.object(notifier, "_window_visible", return_value=True), patch.object(
            notifier_mod, "_show_native_notification"
        ) as show:
            notifier._handle_task_finished(
                {"chatName": "My Chat", "elapsedSeconds": 3}
            )
        show.assert_not_called()

    def test_window_hidden_notifies_with_chat_name(self):
        notifier = _make_notifier(window=_FakeWindow())
        with patch.object(notifier, "_window_visible", return_value=False), patch.object(
            notifier_mod, "_show_native_notification"
        ) as show:
            notifier._handle_task_finished(
                {"chatName": "My Chat", "elapsedSeconds": 3}
            )
        show.assert_called_once_with("My Chat", 3)

    def test_cooldown_suppresses_rapid_duplicate(self):
        notifier = _make_notifier(window=_FakeWindow())
        with patch.object(notifier, "_window_visible", return_value=False), patch.object(
            notifier_mod, "_show_native_notification"
        ) as show:
            notifier._handle_task_finished(
                {"chatName": "My Chat", "elapsedSeconds": 1}
            )
            notifier._handle_task_finished(
                {"chatName": "My Chat", "elapsedSeconds": 1}
            )
        self.assertEqual(show.call_count, 1)

    def test_chat_id_fallback_when_name_missing(self):
        notifier = _make_notifier(window=_FakeWindow())
        with patch.object(notifier, "_window_visible", return_value=False), patch.object(
            notifier_mod, "_show_native_notification"
        ) as show:
            notifier._handle_task_finished(
                {"chatId": "chat-9", "chatName": "", "elapsedSeconds": 0}
            )
        show.assert_called_once_with("chat-9", 0)

    def test_no_name_no_notify(self):
        notifier = _make_notifier(window=_FakeWindow())
        with patch.object(notifier, "_window_visible", return_value=False), patch.object(
            notifier_mod, "_show_native_notification"
        ) as show:
            notifier._handle_task_finished({"elapsedSeconds": 0})
        show.assert_not_called()


class ShowNativeNotificationTests(unittest.TestCase):
    def test_windows_dispatch(self):
        with patch.object(notifier_mod, "_windows_notify") as notify:
            notifier_mod._show_native_notification("My Chat", 5)
        notify.assert_called_once()
        title, body = notify.call_args[0]
        self.assertEqual(title, "My Chat")
        self.assertIn("Task finished", body)
        self.assertIn("5s", body)

    def test_windows_dispatch_no_elapsed(self):
        with patch.object(notifier_mod, "_windows_notify") as notify:
            notifier_mod._show_native_notification("My Chat", 0)
        _, body = notify.call_args[0]
        self.assertEqual(body, "Task finished")

    def test_xml_escape_handles_special_characters(self):
        escaped = notifier_mod._xml_escape('Tom & Jerry <tag> "quoted" \'s')
        self.assertIn("&amp;", escaped)
        self.assertIn("&lt;tag&gt;", escaped)
        self.assertIn("&quot;quoted&quot;", escaped)
        self.assertIn("&apos;s", escaped)

    def test_windows_notify_uses_encoded_powershell_toast(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            from types import SimpleNamespace

            return SimpleNamespace(returncode=0)

        import base64

        with patch.object(notifier_mod, "_windows_register_aumid", return_value=True), patch.object(
            notifier_mod.subprocess, "run", side_effect=fake_run
        ):
            notifier_mod._windows_notify("My Chat", "Task finished · 5s")

        cmd = captured["cmd"]
        self.assertEqual(cmd[0], notifier_mod._windows_powershell_path())
        self.assertIn("-EncodedCommand", cmd)
        encoded = cmd[cmd.index("-EncodedCommand") + 1]
        script = base64.b64decode(encoded).decode("utf-16-le")
        self.assertIn("CreateToastNotifier", script)
        self.assertIn("Windows.UI.Notifications", script)
        self.assertIn("CodeWood.Desktop", script)
        self.assertIn("My Chat", script)
        self.assertIn("Task finished", script)
        self.assertEqual(
            captured["kwargs"].get("creationflags", 0), getattr(notifier_mod.subprocess, "CREATE_NO_WINDOW", 0)
        )

    def test_windows_notify_registers_aumid_then_toasts(self):
        import base64

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            from types import SimpleNamespace

            return SimpleNamespace(returncode=0)

        with patch.object(
            notifier_mod, "_windows_register_aumid", return_value=True
        ) as register, patch.object(
            notifier_mod.subprocess, "run", side_effect=fake_run
        ):
            notifier_mod._windows_notify("My Chat", "Task finished")
        register.assert_called_once()
        script = base64.b64decode(
            captured["cmd"][captured["cmd"].index("-EncodedCommand") + 1]
        ).decode("utf-16-le")
        # The XML embeds the title/body escaped, and single quotes are doubled
        # so user text cannot break out of the PowerShell literal.
        self.assertIn("<text>My Chat</text>", script)
        self.assertIn("'CodeWood.Desktop'", script)

    def test_windows_notify_falls_back_when_powershell_missing(self):
        with patch.object(
            notifier_mod, "_windows_register_aumid", return_value=True
        ), patch.object(
            notifier_mod, "_windows_powershell_path", return_value="nope.exe"
        ), patch.object(
            notifier_mod.subprocess, "run", side_effect=FileNotFoundError
        ), patch.object(notifier_mod, "_windows_shell_notify") as fallback:
            notifier_mod._windows_notify("My Chat", "Task finished")
        fallback.assert_called_once_with("My Chat", "Task finished")

    def test_windows_register_aumid_writes_registry(self):
        try:
            import winreg
        except ImportError:  # pragma: no cover - non-Windows
            self.skipTest("winreg is Windows-only")

        notifier_mod._WINDOWS_AUMID_REGISTERED = False

        class _FakeKey:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __init__(self):
                self.values = []

            def SetValueEx(self, _key, name, _res, _type, value):
                self.values.append((name, value))

        fake_key = _FakeKey()

        def fake_create(base, path, res, access):
            fake_key.path = (base, path, access)
            return fake_key

        with patch.object(notifier_mod, "_windows_app_icon_path", return_value=r"C:\x\app.ico"), patch.object(
            winreg, "CreateKeyEx", side_effect=fake_create
        ), patch.object(winreg, "SetValueEx", side_effect=fake_key.SetValueEx):
            self.assertTrue(notifier_mod._windows_register_aumid())
        values = dict(fake_key.values)
        self.assertEqual(values["DisplayName"], "Code Wood")
        self.assertEqual(values["IconUri"], r"C:\x\app.ico")


class SseConsumeTests(unittest.TestCase):
    def test_task_finished_over_sse_triggers_notification(self):
        payload = {
            "event": "task_finished",
            "data": {
                "chatId": "chat-1",
                "workspaceId": "ws-1",
                "chatName": "Refactor auth",
                "elapsedSeconds": 42,
            },
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence test output
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(
                    b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"
                )
                self.wfile.flush()
                self.wfile.close()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            notifier = _make_notifier(
                port=server.server_address[1], token="tok", window=_FakeWindow()
            )
            with patch.object(notifier, "_window_visible", return_value=False), patch.object(
                notifier_mod, "_show_native_notification"
            ) as show:
                notifier._consume()
            show.assert_called_once_with("Refactor auth", 42)
        finally:
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()


