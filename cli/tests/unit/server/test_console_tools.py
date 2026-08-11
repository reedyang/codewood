import os
import queue
import threading
import time
import unittest
from typing import Any, Dict, Optional
from unittest.mock import Mock

from cli.server.serve_app import ServeApp
from cli.server.console_manager import ConsoleSession, _collapse_cr, _strip_ansi
from cli.tools import registry
from cli.tools.console import (
    ConsoleExecTool,
    ConsoleReadTool,
    ConsoleInfoTool,
    ConsoleSendTool,
    ConsoleWaitTool,
    ConsoleInterruptTool,
    ConsoleResizeTool,
    _decode_escapes,
)
from cli.runtime import prompt_composer


class ConsoleToolGatingTests(unittest.TestCase):
    def test_console_tools_require_gui(self):
        class _A:
            pass

        agent = _A()
        names = {s["function"]["name"] for s in registry.iter_specs(agent)}
        self.assertNotIn("console_exec", names)

        # gui_enabled is keyed on _browser_dispatch; the serve app sets both.
        agent._browser_dispatch = lambda action, payload=None: {"success": True}
        names = {s["function"]["name"] for s in registry.iter_specs(agent)}
        self.assertIn("console_exec", names)
        self.assertIn("console_read", names)
        self.assertIn("console_info", names)
        self.assertIn("console_send", names)
        self.assertIn("console_wait", names)
        self.assertIn("console_interrupt", names)
        self.assertIn("console_resize", names)

    def test_tool_without_dispatch_errors(self):
        class _A:
            execution_policy = "confirmation"

            def _load_confirm_allowlist(self):
                pass

            def _shell_command_in_allowlist(self, _command):
                return False

            def _shell_confirm_should_offer_always(self, _command):
                return False

            def _prompt_confirm_yes_no_maybe_always(self, _prompt, **kwargs):
                return True

        res = ConsoleExecTool().execute(_A(), {"command": "ls"})
        self.assertFalse(res.get("success"))

    def test_exec_forwards_command(self):
        class _A:
            execution_policy = "confirmation"

            def _load_confirm_allowlist(self):
                pass

            def _shell_command_in_allowlist(self, _command):
                return False

            def _shell_confirm_should_offer_always(self, _command):
                return False

            def _prompt_confirm_yes_no_maybe_always(self, _prompt, **kwargs):
                return True

        agent = _A()
        seen = {}

        def _dispatch(action, payload=None):
            seen["action"] = action
            seen["payload"] = payload
            return {"success": True}

        agent._console_dispatch = _dispatch
        ConsoleExecTool().execute(agent, {"command": "echo hi"})
        self.assertEqual(seen["action"], "exec")
        self.assertEqual(seen["payload"], {"command": "echo hi"})

    def test_read_forwards_range(self):
        class _A:
            pass

        agent = _A()
        seen = {}
        agent._console_dispatch = lambda action, payload=None: seen.update(
            {"action": action, "payload": payload}
        ) or {"success": True}
        ConsoleReadTool().execute(agent, {"start": 5, "count": 20})
        self.assertEqual(seen["action"], "read")
        self.assertEqual(seen["payload"], {"start": 5, "count": 20})

    def test_info_dispatch(self):
        class _A:
            pass

        agent = _A()
        agent._console_dispatch = lambda action, payload=None: {"success": True, "action": action}
        res = ConsoleInfoTool().execute(agent, {})
        self.assertEqual(res.get("action"), "info")

    def test_exec_requires_command(self):
        class _A:
            execution_policy = "confirmation"

            def _load_confirm_allowlist(self):
                pass

            def _shell_command_in_allowlist(self, _command):
                return False

            def _shell_confirm_should_offer_always(self, _command):
                return False

            def _prompt_confirm_yes_no_maybe_always(self, _prompt, **kwargs):
                return True

        agent = _A()
        agent._console_dispatch = lambda action, payload=None: {"success": True}
        res = ConsoleExecTool().execute(agent, {"command": "   "})
        self.assertFalse(res.get("success"))

    def test_exec_blocks_inside_running_app_dir(self):
        from pathlib import Path

        repo_root = Path("D:/codewood").resolve()
        workspace = Path("D:/other-workspace").resolve()

        class _DenyPolicy:
            def can_run_shell_in_workdir(self, *, is_dependency_install, is_ai_workspace_script):
                return {"allowed": False, "error": "Blocked shell command: test"}

        class _A:
            execution_policy = "confirmation"

            def _load_confirm_allowlist(self):
                pass

            def _shell_command_in_allowlist(self, _command):
                return False

            def _shell_confirm_should_offer_always(self, _command):
                return False

            def _prompt_confirm_yes_no_maybe_always(self, _prompt, **kwargs):
                return True

        agent = _A()
        agent.path_policy = _DenyPolicy()
        agent._console_dispatch = lambda action, payload=None: {"success": True}
        res = ConsoleExecTool().execute(agent, {"command": "npx ccusage codex"})
        self.assertFalse(res.get("success"))
        self.assertIn("Blocked shell command", str(res.get("error") or ""))

    def test_exec_allows_outside_running_app_dir(self):
        class _AllowPolicy:
            def can_run_shell_in_workdir(self, *, is_dependency_install, is_ai_workspace_script):
                return {"allowed": True, "error": ""}

        class _A:
            execution_policy = "confirmation"

            def _load_confirm_allowlist(self):
                pass

            def _shell_command_in_allowlist(self, _command):
                return False

            def _shell_confirm_should_offer_always(self, _command):
                return False

            def _prompt_confirm_yes_no_maybe_always(self, _prompt, **kwargs):
                return True

        seen = {}
        agent = _A()
        agent.path_policy = _AllowPolicy()
        agent._console_dispatch = lambda action, payload=None: seen.update(
            {"action": action, "payload": payload}
        ) or {"success": True}
        res = ConsoleExecTool().execute(agent, {"command": "npx ccusage codex"})
        self.assertTrue(res.get("success"))
        self.assertEqual(seen.get("action"), "exec")


class ConsoleInteractiveToolTests(unittest.TestCase):
    def _stub_dispatch(self):
        class _A:
            pass

        agent = _A()
        seen = {}
        agent._console_dispatch = lambda action, payload=None: seen.update(
            {"action": action, "payload": payload}
        ) or {"success": True}
        return agent, seen

    def test_send_forwards_data(self):
        agent, seen = self._stub_dispatch()
        res = ConsoleSendTool().execute(agent, {"data": "next\n"})
        self.assertTrue(res.get("success"))
        self.assertEqual(seen["action"], "send")
        self.assertEqual(seen["payload"], {"data": "next\n"})

    def test_send_decodes_escapes_by_default(self):
        agent, seen = self._stub_dispatch()
        res = ConsoleSendTool().execute(agent, {"data": "next\\n"})
        self.assertTrue(res.get("success"))
        self.assertEqual(seen["payload"], {"data": "next\n"})

    def test_send_can_opt_out_of_escapes(self):
        agent, seen = self._stub_dispatch()
        res = ConsoleSendTool().execute(
            agent, {"data": "next\\n", "interpretEscapes": False}
        )
        self.assertTrue(res.get("success"))
        self.assertEqual(seen["payload"], {"data": "next\\n"})

    def test_send_requires_data(self):
        agent, seen = self._stub_dispatch()
        res = ConsoleSendTool().execute(agent, {})
        self.assertFalse(res.get("success"))
        self.assertNotIn("action", seen)

    def test_send_interprets_escapes(self):
        agent, seen = self._stub_dispatch()
        res = ConsoleSendTool().execute(
            agent, {"data": "run\n", "interpretEscapes": True}
        )
        self.assertTrue(res.get("success"))
        self.assertEqual(seen["payload"], {"data": "run\n"})

    def test_wait_forwards_params(self):
        agent, seen = self._stub_dispatch()
        ConsoleWaitTool().execute(
            agent, {"start": 7, "count": 50, "timeout": 2.5, "stable": True}
        )
        self.assertEqual(seen["action"], "wait")
        self.assertEqual(
            seen["payload"],
            {"start": 7, "count": 50, "timeout": 2.5, "stable": True},
        )

    def test_wait_optional_start(self):
        agent, seen = self._stub_dispatch()
        ConsoleWaitTool().execute(agent, {"timeout": 1})
        self.assertEqual(seen["action"], "wait")
        self.assertEqual(
            seen["payload"],
            {"start": None, "count": 200, "timeout": 1, "stable": False},
        )

    def test_interrupt_dispatch(self):
        agent, seen = self._stub_dispatch()
        ConsoleInterruptTool().execute(agent, {})
        self.assertEqual(seen["action"], "interrupt")

    def test_resize_forwards_params(self):
        agent, seen = self._stub_dispatch()
        ConsoleResizeTool().execute(agent, {"cols": 120, "rows": 40})
        self.assertEqual(seen["action"], "resize")
        self.assertEqual(seen["payload"], {"cols": 120, "rows": 40})

    def test_decode_escapes(self):
        self.assertEqual(_decode_escapes(r"\n"), "\n")
        self.assertEqual(_decode_escapes(r"\r"), "\r")
        self.assertEqual(_decode_escapes(r"\t"), "\t")
        self.assertEqual(_decode_escapes(r"\e"), "\x1b")
        self.assertEqual(_decode_escapes(r"\\"), "\\")
        self.assertEqual(_decode_escapes(r"\x03"), "\x03")
        self.assertEqual(_decode_escapes(r"\u001b[A"), "\x1b[A")
        self.assertEqual(_decode_escapes("a\\r\\nb"), "a\r\nb")
        # Unknown sequences are left untouched.
        self.assertEqual(_decode_escapes(r"\q"), r"\q")


class ConsoleCrCollapseTests(unittest.TestCase):
    def test_plain_line_unchanged(self):
        self.assertEqual(_collapse_cr("hello"), "hello")

    def test_progress_refresh_keeps_last(self):
        self.assertEqual(_collapse_cr("0%\r10%\r20%\r100%"), "100%")

    def test_partial_overlay(self):
        self.assertEqual(_collapse_cr("abc\rX"), "Xbc")

    def test_trailing_cr_keeps_current_line(self):
        self.assertEqual(_collapse_cr("50%\r"), "50%\r")

    def test_strip_ansi_removes_csi_and_esc(self):
        self.assertEqual(_strip_ansi("\x1b[31mred\x1b[0m"), "red")
        self.assertEqual(_strip_ansi("\x1b[11;1H\x1b7x\x1b8"), "x")
        self.assertEqual(_strip_ansi("plain"), "plain")

    def test_ingest_strips_ansi_from_lines(self):
        session = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        session._ingest(b"\x1b[93mVersion:\x1b[0m 7.2\n")
        r = session.read_lines(0, 10)
        self.assertEqual(r["lines"], ["Version: 7.2"])


class ConsolePromptAppendTests(unittest.TestCase):
    def test_append_empty_without_gui(self):
        class _A:
            pass

        self.assertEqual(prompt_composer.build_console_system_append(_A()), "")

    def test_append_describes_console_with_gui(self):
        class _A:
            pass

        agent = _A()
        agent._console_dispatch = lambda action, payload=None: {"success": True}
        text = prompt_composer.build_console_system_append(agent)
        self.assertIn("console_exec", text)
        self.assertIn("Embedded console", text)


class _FakeConsole:
    def __init__(self) -> None:
        self._active = None
        self.written = []
        self.open_called_with = None

    def active(self):
        return self._active

    def open(self, kind: str) -> Dict[str, Any]:
        self.open_called_with = kind
        session = ConsoleSession("auto", kind, kind, "/tmp", 100)
        self._active = session
        return {"success": True, **session.info()}


class ConsoleDispatchTests(unittest.TestCase):
    def _stub(self):
        class _BroadcasterMock:
            published: list = None

            def __init__(self):
                self.published = []

            def publish(self, event, data=None):
                self.published.append((event, data))

        class _Stub:
            def open_console(self_, kind: str) -> Dict[str, Any]:
                return self_._console.open(kind)

        stub = _Stub()
        stub._console = _FakeConsole()
        stub.broadcaster = _BroadcasterMock()
        stub.dispatch_console_command = ServeApp.dispatch_console_command.__get__(
            stub, _Stub
        )
        return stub

    def test_no_active_console_errors_on_info(self):
        stub = self._stub()
        res = stub.dispatch_console_command("info")
        self.assertFalse(res.get("success"))

    def test_no_active_console_errors_on_read(self):
        stub = self._stub()
        res = stub.dispatch_console_command("read", {"start": 0})
        self.assertFalse(res.get("success"))

    def test_exec_auto_opens_console(self):
        stub = self._stub()
        stub._console._active = None
        res = stub.dispatch_console_command("exec", {"command": "echo hi"})
        self.assertTrue(res.get("success"))
        self.assertEqual(stub._console.open_called_with, "powershell" if os.name == "nt" else "shell")
        self.assertEqual(stub.broadcaster.published[-1][0], "console_open")

    def test_exec_writes_command_with_newline(self):
        stub = self._stub()
        session = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        sent = []
        session.write = lambda data: sent.append(data)
        stub._console._active = session
        res = stub.dispatch_console_command("exec", {"command": "ls -la"})
        self.assertTrue(res.get("success"))
        self.assertEqual(sent, ["ls -la\r"])

    def test_send_writes_raw_text_without_newline(self):
        stub = self._stub()
        session = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        sent = []
        session.write = lambda data: sent.append(data)
        stub._console._active = session
        res = stub.dispatch_console_command("send", {"data": "next\n"})
        self.assertTrue(res.get("success"))
        self.assertEqual(sent, ["next\n"])
        self.assertEqual(res.get("sent"), "next\n")

    def test_send_requires_data(self):
        stub = self._stub()
        stub._console._active = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        res = stub.dispatch_console_command("send", {"data": ""})
        self.assertFalse(res.get("success"))

    def test_interrupt_writes_ctrl_c(self):
        stub = self._stub()
        session = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        sent = []
        session.write = lambda data: sent.append(data)
        stub._console._active = session
        res = stub.dispatch_console_command("interrupt")
        self.assertTrue(res.get("success"))
        self.assertEqual(sent, ["\x03"])

    def test_resize_updates_dimensions(self):
        stub = self._stub()
        session = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        stub._console._active = session
        res = stub.dispatch_console_command("resize", {"cols": 132, "rows": 43})
        self.assertTrue(res.get("success"))
        self.assertEqual(res.get("cols"), 132)
        self.assertEqual(res.get("rows"), 43)

    def test_wait_returns_new_lines(self):
        import threading
        import time

        stub = self._stub()
        session = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        stub._console._active = session

        def _produce():
            time.sleep(0.05)
            session._ingest(b"break main\n(gdb) \n")

        t = threading.Thread(target=_produce)
        t.start()
        res = stub.dispatch_console_command("wait", {"start": 0, "timeout": 2})
        t.join()
        self.assertTrue(res.get("success"))
        self.assertIn("break main", res.get("output", ""))

    def test_wait_without_start_waits_for_unseen_output(self):
        import threading
        import time

        stub = self._stub()
        session = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        # The model already consumed the first line via console_read.
        session._ingest(b"old line\n")
        session.read_lines(0, 100)
        stub._console._active = session

        def _produce():
            time.sleep(0.05)
            session._ingest(b"new line\n")

        t = threading.Thread(target=_produce)
        t.start()
        res = stub.dispatch_console_command("wait", {"timeout": 2})
        t.join()
        self.assertTrue(res.get("success"))
        self.assertIn("new line", res.get("output", ""))
        self.assertNotIn("old line", res.get("output", ""))

    def test_wait_without_start_times_out_when_no_new_output(self):
        stub = self._stub()
        session = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        session._ingest(b"old line\n")
        session.read_lines(0, 100)
        stub._console._active = session
        res = stub.dispatch_console_command("wait", {"timeout": 0.1})
        self.assertTrue(res.get("success"))
        self.assertTrue(res.get("timedOut"))

    def test_wait_wakes_on_inplace_cr_refresh(self):
        import threading
        import time

        stub = self._stub()
        session = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        stub._console._active = session

        def _refresh():
            time.sleep(0.05)
            session._ingest(b"downloading 42%\r")

        t = threading.Thread(target=_refresh)
        t.start()
        res = stub.dispatch_console_command("wait", {"timeout": 2})
        t.join()
        self.assertTrue(res.get("success"))
        self.assertFalse(res.get("timedOut"))
        self.assertIn("downloading 42%", res.get("output", ""))

    def test_wait_stable_returns_after_refresh_stream_ends(self):
        import threading
        import time

        stub = self._stub()
        session = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        session._settle_quiet = 0.05
        stub._console._active = session

        def _stream():
            for pct in (10, 30, 50, 70, 90, 100):
                session._ingest(f"progress {pct}%\r".encode())
                time.sleep(0.03)
            time.sleep(0.01)
            session._ingest(b"progress done\n")

        t = threading.Thread(target=_stream)
        t.start()
        res = stub.dispatch_console_command(
            "wait", {"timeout": 3, "stable": True}
        )
        t.join()
        self.assertTrue(res.get("success"))
        self.assertFalse(res.get("timedOut"))
        self.assertIn("progress done", res.get("output", ""))

    def test_wait_stable_ignores_pre_wait_output(self):
        import time

        stub = self._stub()
        session = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        session._settle_quiet = 0.05
        stub._console._active = session
        # Echo of a just-sent command arrives before the wait starts and is
        # already quiet; the wait must NOT return it as "new output".
        session._ingest(b"echo of command\n")
        time.sleep(0.1)
        res = stub.dispatch_console_command(
            "wait", {"timeout": 0.3, "stable": True}
        )
        self.assertTrue(res.get("success"))
        self.assertTrue(res.get("timedOut"))

    def test_wait_stable_waits_for_output_after_pre_wait_echo(self):
        import threading
        import time

        stub = self._stub()
        session = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        session._settle_quiet = 0.05
        stub._console._active = session
        session._ingest(b"echo of command\n")
        time.sleep(0.1)

        def _later():
            time.sleep(0.05)
            session._ingest(b"next prompt\n")

        t = threading.Thread(target=_later)
        t.start()
        res = stub.dispatch_console_command(
            "wait", {"timeout": 2, "stable": True}
        )
        t.join()
        self.assertTrue(res.get("success"))
        self.assertFalse(res.get("timedOut"))
        self.assertIn("next prompt", res.get("output", ""))

    def test_wait_stable_returns_latest_on_timeout(self):
        import threading
        import time

        stub = self._stub()
        session = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        session._settle_quiet = 0.05
        stub._console._active = session

        def _stream():
            # Keep refreshing well past the wait timeout.
            for _ in range(30):
                session._ingest(b"still working...\r")
                time.sleep(0.02)

        t = threading.Thread(target=_stream)
        t.start()
        res = stub.dispatch_console_command(
            "wait", {"timeout": 0.5, "stable": True}
        )
        t.join()
        self.assertTrue(res.get("success"))
        self.assertTrue(res.get("timedOut"))
        self.assertIn("still working...", res.get("output", ""))

    def test_wait_stable_waits_for_newline_refresh_frames(self):
        import threading
        import time

        stub = self._stub()
        session = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        session._settle_quiet = 0.05
        stub._console._active = session

        def _stream():
            for i in range(10):
                session._ingest(b"frame %d\n" % i)
                time.sleep(0.01)
            time.sleep(0.02)
            session._ingest(b"done\n")

        t = threading.Thread(target=_stream)
        t.start()
        res = stub.dispatch_console_command("wait", {"timeout": 3, "stable": True})
        t.join()
        self.assertTrue(res.get("success"))
        self.assertFalse(res.get("timedOut"))
        self.assertIn("done", res.get("output", ""))

    def test_cr_refresh_folds_into_final_newline(self):
        stub = self._stub()
        session = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        session._ingest(b"progress 10%\rprogress 50%\rprogress 100%\n")
        stub._console._active = session
        res = stub.dispatch_console_command("read", {"start": 0, "count": 10})
        self.assertTrue(res.get("success"))
        self.assertIn("progress 100%", res.get("output", ""))
        self.assertNotIn("progress 10%", res.get("output", ""))

    def test_read_returns_lines_and_total(self):
        stub = self._stub()
        session = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        session._ingest(b"x\ny\nz\n")
        stub._console._active = session
        res = stub.dispatch_console_command("read", {"start": 0, "count": 2})
        self.assertTrue(res.get("success"))
        self.assertEqual(res["lines"], ["x", "y"])
        self.assertEqual(res["totalLines"], 3)

    def test_unknown_action(self):
        stub = self._stub()
        stub._console._active = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        res = stub.dispatch_console_command("explode")
        self.assertFalse(res.get("success"))


class ConsoleExecConfirmChoiceTests(unittest.TestCase):
    """The console_exec execution-policy confirm must route through the
    structured ``_confirm_choice_provider`` (the GUI panel that carries the
    ``rejectSupplement`` flag) instead of the legacy y/n text fallback."""

    def test_confirm_uses_structured_choice_provider(self):
        from cli.services.execution_policy_service import prompt_confirm_yes_no_maybe_always

        class _A:
            calls = []

            def _ui_language(self) -> str:
                return "en"

            def _confirm_choice_provider(
                self,
                prompt: str,
                options: Any,
                offer_always: bool = False,
                command: Optional[str] = None,
                preview_segments: Optional[Any] = None,
            ) -> str:
                self.calls.append(
                    {
                        "prompt": prompt,
                        "options": list(options),
                        "offer_always": offer_always,
                        "command": command,
                    }
                )
                return "n"

            def _suspended_input(self, prompt: str = "") -> str:
                self.text_fallback_calls = getattr(self, "text_fallback_calls", 0) + 1
                return ""

        agent = _A()
        agent.calls = []
        ok = prompt_confirm_yes_no_maybe_always(
            agent,
            "Run this command?",
            offer_always=False,
            kind="console",
            shell_command="echo hi",
            display_command="echo hi",
        )
        self.assertFalse(ok)
        # The structured provider must be the one rendering the confirm, so
        # the frontend receives ``rejectSupplement: true`` and shows the
        # "Reject & supplement info" action.
        self.assertEqual(len(agent.calls), 1)
        self.assertEqual(agent.calls[0]["command"], "echo hi")
        self.assertEqual(len(agent.calls[0]["options"]), 2)
        self.assertFalse(agent.calls[0]["offer_always"])
        self.assertFalse(getattr(agent, "text_fallback_calls", 0))

    def test_confirm_falls_back_to_text_when_provider_fails(self):
        """When the structured provider raises (e.g. a transport error), the
        legacy ``_suspended_input`` path takes over and the frontend confirm
        event carries no ``rejectSupplement`` flag — the user then sees only
        the plain Yes/No(/Always) options."""
        from cli.services.execution_policy_service import prompt_confirm_yes_no_maybe_always

        class _A:
            def _ui_language(self) -> str:
                return "en"

            def _confirm_choice_provider(self, *args: Any, **kwargs: Any) -> str:
                raise RuntimeError("transport down")

            def _suspended_input(self, prompt: str = "") -> str:
                self.text_fallback_calls = getattr(self, "text_fallback_calls", 0) + 1
                return ""

        agent = _A()
        ok = prompt_confirm_yes_no_maybe_always(
            agent,
            "Run this command?",
            offer_always=False,
            kind="console",
            shell_command="echo hi",
            display_command="echo hi",
        )
        self.assertFalse(ok)
        self.assertEqual(agent.text_fallback_calls, 1)

    def test_console_exec_tool_confirm_routes_to_structured_provider(self):
        """End-to-end: ConsoleExecTool.execute() with the real execution-policy
        confirm chain must reach ``_confirm_choice_provider`` (the GUI source
        of the ``rejectSupplement`` flag), never the legacy text prompt."""
        from cli.services.execution_policy_service import prompt_confirm_yes_no_maybe_always

        class _A:
            execution_policy = "confirmation"
            calls = []

            def _load_confirm_allowlist(self) -> None:
                pass

            def _shell_command_in_allowlist(self, _command: str) -> bool:
                return False

            def _shell_confirm_should_offer_always(self, _command: str) -> bool:
                return False

            def _ui_language(self) -> str:
                return "en"

            def _confirm_choice_provider(
                self,
                prompt: str,
                options: Any,
                offer_always: bool = False,
                command: Optional[str] = None,
                preview_segments: Optional[Any] = None,
            ) -> str:
                self.calls.append(
                    {"options": list(options), "offer_always": offer_always, "command": command}
                )
                return "y"

            def _suspended_input(self, prompt: str = "") -> str:
                self.text_fallback_calls = getattr(self, "text_fallback_calls", 0) + 1
                return ""

            def _console_dispatch(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
                return {"success": True}

        agent = _A()
        agent.calls = []
        agent._prompt_confirm_yes_no_maybe_always = (
            lambda prompt_core, **kw: prompt_confirm_yes_no_maybe_always(agent, prompt_core, **kw)
        )
        res = ConsoleExecTool().execute(agent, {"command": "echo hi"})
        self.assertTrue(res.get("success"), res.get("error"))
        self.assertEqual(len(agent.calls), 1)
        self.assertEqual(agent.calls[0]["command"], "echo hi")
        self.assertFalse(getattr(agent, "text_fallback_calls", 0))


class ServeAppConfirmChoiceBroadcastTests(unittest.TestCase):
    """The real serve_app ``_confirm_choice_provider`` must broadcast the
    ``rejectSupplement`` flag for every execution-policy confirm (including
    console_exec), so the frontend always shows the reject-with-supplement
    action."""

    def _app(self):
        from cli.server.serve_app import ServeApp

        class _Stub(ServeApp):
            def __init__(self, **kwargs):
                for key, value in kwargs.items():
                    setattr(self, key, value)

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

        app = _Stub(
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
        return app, broadcaster

    def test_console_confirm_broadcast_includes_reject_supplement(self):
        app, broadcaster = self._app()

        def _answer():
            for _ in range(200):
                with app._confirms_lock:
                    cid = next(iter(app._confirms), None)
                if cid:
                    with app._confirms_lock:
                        app._confirms[cid].put("n")
                    return
                time.sleep(0.01)

        t = threading.Thread(target=_answer, daemon=True)
        t.start()
        result = app._confirm_choice_provider(
            "Run this command?",
            ["Yes, execute", "No, cancel"],
            False,
            "echo hi",
            None,
        )
        t.join(timeout=5)
        self.assertEqual(result, "n")
        self.assertEqual(broadcaster.publish.call_count, 1)
        event, payload = broadcaster.publish.call_args[0]
        self.assertEqual(event, "confirm")
        self.assertTrue(payload.get("rejectSupplement"))
        self.assertEqual(payload.get("command"), "echo hi")
        self.assertEqual(payload.get("options"), ["Yes, execute", "No, cancel"])


if __name__ == "__main__":
    unittest.main()
