import unittest

from cli.server.serve_app import ServeApp
from cli.server.console_manager import ConsoleSession
from cli.tools import registry
from cli.tools.console import ConsoleExecTool, ConsoleReadTool, ConsoleInfoTool
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

    def test_tool_without_dispatch_errors(self):
        class _A:
            pass

        res = ConsoleExecTool().execute(_A(), {"command": "ls"})
        self.assertFalse(res.get("success"))

    def test_exec_forwards_command(self):
        class _A:
            pass

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
            pass

        agent = _A()
        agent._console_dispatch = lambda action, payload=None: {"success": True}
        res = ConsoleExecTool().execute(agent, {"command": "   "})
        self.assertFalse(res.get("success"))


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

    def active(self):
        return self._active


class ConsoleDispatchTests(unittest.TestCase):
    def _stub(self):
        class _Stub:
            pass

        stub = _Stub()
        stub._console = _FakeConsole()
        stub.dispatch_console_command = ServeApp.dispatch_console_command.__get__(
            stub, _Stub
        )
        return stub

    def test_no_active_console_errors(self):
        stub = self._stub()
        res = stub.dispatch_console_command("info")
        self.assertFalse(res.get("success"))

    def test_exec_writes_command_with_newline(self):
        stub = self._stub()
        session = ConsoleSession("a", "cmd", "t", "/tmp", 100)
        sent = []
        session.write = lambda data: sent.append(data)
        stub._console._active = session
        res = stub.dispatch_console_command("exec", {"command": "ls -la"})
        self.assertTrue(res.get("success"))
        self.assertEqual(sent, ["ls -la\r"])

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


if __name__ == "__main__":
    unittest.main()
