import tempfile
import threading
import unittest
from pathlib import Path

from cli.server.serve_app import ServeApp
from cli.tools import registry
from cli.tools.browser import (
    BrowserOpenTool,
    BrowserPreviewFileTool,
    BrowserReadDomTool,
)
from cli.runtime import prompt_composer


class _FakeChatStateManager:
    def __init__(self, cfg_dir: Path) -> None:
        self._cfg = Path(cfg_dir)

    def chat_records_dir(self) -> Path:
        return self._cfg / "chats"

    def chat_data_dir_for_chat(self, chat_id: str):
        cid = str(chat_id or "").strip()
        if not cid:
            return None
        return self.chat_records_dir() / "data" / f"record-{cid}"


class _FakeAgent:
    def __init__(self, cfg_dir: Path) -> None:
        self._chat_state_manager = _FakeChatStateManager(cfg_dir)


def _app(cfg_dir: Path) -> ServeApp:
    """Bind the methods under test onto a lightweight stub, skipping the heavy
    ``ServeApp.__init__`` (server / agent loop)."""

    class _Stub:
        pass

    import queue

    stub = _Stub()
    stub.agent = _FakeAgent(cfg_dir)
    stub._browser_cmds = {}
    stub._browser_cmds_lock = threading.Lock()

    class _FakeBroadcaster:
        def __init__(self) -> None:
            self.published = []

        def publish(self, event, data):
            self.published.append((event, data))

    stub.broadcaster = _FakeBroadcaster()
    for name in (
        "dispatch_browser_command",
        "answer_browser_result",
        "save_preview_html",
        "read_chat_file",
    ):
        setattr(stub, name, getattr(ServeApp, name).__get__(stub, _Stub))
    stub._BROWSER_CMD_TIMEOUT_S = 2.0
    stub._PREVIEW_HTML_MAX_BYTES = ServeApp._PREVIEW_HTML_MAX_BYTES
    return stub  # type: ignore[return-value]


class BrowserToolGatingTests(unittest.TestCase):
    def test_browser_tools_require_gui(self):
        # No _browser_dispatch -> hidden.
        class _A:
            pass

        agent = _A()
        names = {s["function"]["name"] for s in registry.iter_specs(agent)}
        self.assertNotIn("browser_open", names)

        # With _browser_dispatch -> visible.
        agent._browser_dispatch = lambda action, payload=None: {"success": True}
        names = {s["function"]["name"] for s in registry.iter_specs(agent)}
        self.assertIn("browser_open", names)
        self.assertIn("browser_read_dom", names)

    def test_tool_without_dispatch_returns_error(self):
        class _A:
            pass

        res = BrowserOpenTool().execute(_A(), {"url": "https://example.com"})
        self.assertFalse(res.get("success"))

    def test_tool_forwards_to_dispatch(self):
        class _A:
            pass

        agent = _A()
        seen = {}

        def _dispatch(action, payload=None):
            seen["action"] = action
            seen["payload"] = payload
            return {"success": True, "url": payload.get("url")}

        agent._browser_dispatch = _dispatch
        res = BrowserOpenTool().execute(agent, {"url": "example.com"})
        self.assertTrue(res.get("success"))
        self.assertEqual(seen["action"], "open")

    def test_read_dom_tool_takes_no_url(self):
        class _A:
            pass

        agent = _A()
        agent._browser_dispatch = lambda action, payload=None: {"action": action}
        res = BrowserReadDomTool().execute(agent, {})
        self.assertEqual(res.get("action"), "read_dom")

    def test_preview_file_tool_forwards_to_hook(self):
        class _A:
            pass

        agent = _A()
        seen = {}

        def _hook(path):
            seen["path"] = path
            return {"success": True}

        agent._browser_preview_file = _hook
        res = BrowserPreviewFileTool().execute(agent, {"path": "page.html"})
        self.assertTrue(res.get("success"))
        self.assertEqual(seen["path"], "page.html")

    def test_preview_file_tool_without_hook_errors(self):
        class _A:
            pass

        res = BrowserPreviewFileTool().execute(_A(), {"path": "page.html"})
        self.assertFalse(res.get("success"))


class BrowserPromptAppendTests(unittest.TestCase):
    def test_append_empty_without_gui(self):
        class _A:
            pass

        self.assertEqual(prompt_composer.build_browser_system_append(_A()), "")

    def test_append_describes_browser_with_gui(self):
        class _A:
            pass

        agent = _A()
        agent._browser_dispatch = lambda action, payload=None: {"success": True}
        text = prompt_composer.build_browser_system_append(agent)
        self.assertIn("browser_preview_file", text)
        self.assertIn("Embedded browser", text)


class BrowserCommandChannelTests(unittest.TestCase):
    def test_dispatch_then_answer_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            app = _app(Path(d))
            results = {}

            def _runner():
                results["out"] = app.dispatch_browser_command("open", {"url": "x"})

            t = threading.Thread(target=_runner)
            t.start()
            # The publish records the requestId; fetch it and answer.
            import time

            request_id = ""
            for _ in range(100):
                if app.broadcaster.published:
                    request_id = app.broadcaster.published[-1][1]["requestId"]
                    break
                time.sleep(0.01)
            self.assertTrue(request_id)
            ok = app.answer_browser_result(request_id, {"success": True, "url": "x"})
            self.assertTrue(ok)
            t.join(timeout=3)
            self.assertEqual(results["out"], {"success": True, "url": "x"})

    def test_answer_unknown_request_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            app = _app(Path(d))
            self.assertFalse(app.answer_browser_result("nope", {"success": True}))

    def test_dispatch_times_out_without_answer(self):
        with tempfile.TemporaryDirectory() as d:
            app = _app(Path(d))
            app._BROWSER_CMD_TIMEOUT_S = 0.1
            res = app.dispatch_browser_command("open", {"url": "x"})
            self.assertFalse(res.get("success"))


class PreviewHtmlTests(unittest.TestCase):
    def test_save_and_read_preview_html(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            app = _app(cfg)
            res = app.save_preview_html("c1", "<html><body><h1>hi</h1></body></html>")
            self.assertTrue(res.get("ok"), res)
            saved = Path(res["path"])
            self.assertTrue(saved.exists())
            self.assertEqual(
                saved.parent, (cfg / "chats" / "data" / "record-c1").resolve()
            )
            # The bridge script is injected before </body>.
            text = saved.read_text(encoding="utf-8")
            self.assertIn("__codewoodBridge", text)
            self.assertIn("<h1>hi</h1>", text)

            data_ct = app.read_chat_file(str(saved))
            self.assertIsNotNone(data_ct)
            data, ct = data_ct
            self.assertIn("text/html", ct)
            self.assertIn(b"__codewoodBridge", data)

    def test_save_rejects_missing_chat(self):
        with tempfile.TemporaryDirectory() as d:
            app = _app(Path(d))
            self.assertFalse(app.save_preview_html("", "<html></html>").get("ok"))

    def test_save_rejects_empty_html(self):
        with tempfile.TemporaryDirectory() as d:
            app = _app(Path(d))
            self.assertFalse(app.save_preview_html("c1", "   ").get("ok"))

    def test_read_chat_file_rejects_outside_data_dir(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            app = _app(cfg)
            outside = cfg / "secret.html"
            outside.write_text("<html></html>", encoding="utf-8")
            self.assertIsNone(app.read_chat_file(str(outside)))

    def test_read_chat_file_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            app = _app(cfg)
            res = app.save_preview_html("c1", "<html><body>x</body></html>")
            saved = Path(res["path"])
            traversal = saved.parent / ".." / ".." / ".." / "etc.html"
            self.assertIsNone(app.read_chat_file(str(traversal)))


if __name__ == "__main__":
    unittest.main()
