"""Unit tests for the desktop host's in-window overlay browser.

The overlay module lives under ``desktop/host`` (not a Python package on the
import path), so it is loaded by file path. A fake pywebview module and a fake
window stand in for the real webview so the geometry/visibility/command logic
can be exercised without a GUI.
"""

import importlib.util
import unittest
from pathlib import Path

_OVERLAY_PATH = (
    Path(__file__).resolve().parents[4]
    / "desktop"
    / "host"
    / "browser_overlay.py"
)


def _load_overlay_module():
    spec = importlib.util.spec_from_file_location(
        "codewood_test_browser_overlay", str(_OVERLAY_PATH)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


overlay_mod = _load_overlay_module()
BrowserOverlay = overlay_mod.BrowserOverlay


class _FakeEvents:
    def __init__(self) -> None:
        self.loaded = self  # support ``events.loaded += handler``

    def __iadd__(self, _handler):
        return self


class _FakeWindow:
    """Records webview operations for assertions."""

    def __init__(self) -> None:
        self.events = _FakeEvents()
        self.moves: list = []
        self.resizes: list = []
        self.shown = False
        self.hidden_calls = 0
        self.show_calls = 0
        self.loaded_url = None
        self.loaded_html = None
        self.evaluated: list = []
        self._current_url = ""
        self.run_js_calls: list = []

    def move(self, x, y):
        self.moves.append((x, y))

    def resize(self, w, h):
        self.resizes.append((w, h))

    def show(self):
        self.shown = True
        self.show_calls += 1

    def hide(self):
        self.shown = False
        self.hidden_calls += 1

    def load_url(self, url):
        self.loaded_url = url
        self._current_url = url

    def load_html(self, html):
        self.loaded_html = html

    def get_current_url(self):
        return self._current_url

    def run_js(self, code):
        self.run_js_calls.append(code)

    def evaluate_js(self, code):
        self.evaluated.append(code)
        if "outerHTML" in code:
            return "<html><body>hi</body></html>"
        if "__codewoodConsole" in code:
            return '[{"level":"log","text":"hello"}]'
        return "evaluated"

    def destroy(self):
        pass


class _FakeWebview:
    def __init__(self) -> None:
        self.created: list = []
        self._next = None

    def set_next_window(self, win):
        self._next = win

    def create_window(self, *_args, **_kwargs):
        win = self._next or _FakeWindow()
        self.created.append(win)
        return win


class _FakeMain:
    def __init__(self, x=100, y=50):
        self.x = x
        self.y = y


class OverlayDisabledTests(unittest.TestCase):
    def test_disabled_overlay_is_noop(self):
        wv = _FakeWebview()
        ov = BrowserOverlay(wv, _FakeMain(), enabled=False)
        self.assertFalse(ov.enabled)
        self.assertIsNone(ov.ensure_window())
        self.assertFalse(ov.set_bounds(0, 0, 10, 10))
        self.assertFalse(ov.show())
        self.assertEqual(ov.navigate("https://x").get("success"), False)
        self.assertEqual(len(wv.created), 0)


class OverlayGeometryTests(unittest.TestCase):
    def setUp(self):
        self.wv = _FakeWebview()
        self.win = _FakeWindow()
        self.wv.set_next_window(self.win)
        self.main = _FakeMain(x=200, y=80)
        self.ov = BrowserOverlay(self.wv, self.main, enabled=True)

    def test_show_then_bounds_positions_relative_to_main_origin(self):
        self.ov.show()
        self.ov.set_bounds(30, 40, 300, 200)
        # screen = main origin + client rect offset
        self.assertEqual(self.win.moves[-1], (200 + 30, 80 + 40))
        self.assertEqual(self.win.resizes[-1], (300, 200))
        self.assertTrue(self.win.shown)

    def test_hide_hides_window_and_keeps_intent(self):
        self.ov.show()
        self.ov.set_bounds(0, 0, 100, 100)
        self.assertTrue(self.win.shown)
        self.ov.hide()
        self.assertFalse(self.win.shown)
        self.assertGreaterEqual(self.win.hidden_calls, 1)

    def test_resync_after_main_move_repositions(self):
        self.ov.show()
        self.ov.set_bounds(10, 10, 100, 100)
        self.main.x = 500
        self.main.y = 300
        self.ov.resync()
        self.assertEqual(self.win.moves[-1], (510, 310))

    def test_child_mode_positions_relative_to_parent_client(self):
        # When reparented as a WS_CHILD, move() is parent-client-relative, so
        # the screen-origin offset must be dropped.
        self.ov._is_child = True
        self.ov.show()
        self.ov.set_bounds(30, 40, 300, 200)
        self.assertEqual(self.win.moves[-1], (30, 40))
        self.assertEqual(self.win.resizes[-1], (300, 200))

    def test_no_bounds_means_hidden(self):
        # show() with no rect yet should not position/show the window.
        self.ov.show()
        self.assertFalse(self.win.shown)

    def test_minimized_suspend_hides(self):
        self.ov.show()
        self.ov.set_bounds(0, 0, 50, 50)
        self.assertTrue(self.win.shown)
        self.ov.suspend_for_main_minimized()
        self.assertFalse(self.win.shown)


class OverlayCommandTests(unittest.TestCase):
    def setUp(self):
        self.wv = _FakeWebview()
        self.win = _FakeWindow()
        self.wv.set_next_window(self.win)
        self.ov = BrowserOverlay(self.wv, _FakeMain(), enabled=True)

    def test_open_navigates(self):
        r = self.ov.run_command("open", url="https://example.com")
        self.assertTrue(r["success"])
        self.assertEqual(self.win.loaded_url, "https://example.com")

    def test_read_dom_returns_serialized_html(self):
        self.ov.run_command("open", url="https://example.com")
        r = self.ov.run_command("read_dom")
        self.assertTrue(r["success"])
        self.assertIn("hi", r["dom"])

    def test_read_console_parses_json(self):
        self.ov.run_command("open", url="https://example.com")
        r = self.ov.run_command("read_console")
        self.assertTrue(r["success"])
        self.assertEqual(r["console"], [{"level": "log", "text": "hello"}])

    def test_eval_returns_result(self):
        self.ov.run_command("open", url="https://example.com")
        r = self.ov.run_command("eval", script="1+1")
        self.assertTrue(r["success"])
        self.assertEqual(r["result"], "evaluated")

    def test_eval_empty_script_errors(self):
        self.ov.run_command("open", url="https://example.com")
        r = self.ov.run_command("eval", script="   ")
        self.assertFalse(r["success"])

    def test_close_blanks_and_hides(self):
        self.ov.show()
        self.ov.set_bounds(0, 0, 50, 50)
        self.ov.run_command("open", url="https://example.com")
        r = self.ov.run_command("close")
        self.assertTrue(r["success"])
        self.assertEqual(self.win.loaded_url, "about:blank")
        self.assertFalse(self.win.shown)

    def test_unknown_action_errors(self):
        r = self.ov.run_command("frobnicate")
        self.assertFalse(r["success"])
        self.assertIn("unknown action", r["error"])


if __name__ == "__main__":
    unittest.main()
