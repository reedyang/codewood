"""Desktop GUI host.

Creates a WebView2 (Windows) / system WebView window via pywebview,
launches the Code Wood backend in serve mode, loads the TypeScript
frontend, and tears the backend down when the window closes.
"""

from __future__ import annotations

import json
import sys

import webview

try:
    from backend import BackendError, BackendProcess
    from bridge import resolve_frontend_url
except ImportError:  # pragma: no cover - allow running as a module too
    from .backend import BackendError, BackendProcess  # type: ignore
    from .bridge import resolve_frontend_url  # type: ignore

WINDOW_TITLE = "Code Wood"


def _preferred_gui() -> str | None:
    # On Windows, force the EdgeChromium (WebView2) renderer.
    if sys.platform == "win32":
        return "edgechromium"
    return None


def _pick_folder() -> str:
    """Open a native folder picker; return the selected path or ""."""
    window = webview.active_window()
    if window is None:
        return ""
    try:
        result = window.create_file_dialog(webview.FOLDER_DIALOG)
    except Exception:
        return ""
    if not result:
        return ""
    return result[0] if isinstance(result, (list, tuple)) else str(result)


class HostApi:
    """Minimal bridge exposed to the frontend as ``window.pywebview.api``."""

    def pick_folder(self) -> str:
        return _pick_folder()


def _dispatch_menu(action: str, payload: str | None = None) -> None:
    """Forward a native-menu action to the frontend handler."""
    window = webview.active_window()
    if window is None:
        return
    if payload is None:
        js = f"window.__codewoodMenu && window.__codewoodMenu({json.dumps(action)})"
    else:
        js = (
            "window.__codewoodMenu && "
            f"window.__codewoodMenu({json.dumps(action)}, {json.dumps(payload)})"
        )
    try:
        window.evaluate_js(js)
    except Exception:
        pass


def _close_window() -> None:
    window = webview.active_window()
    if window is not None:
        try:
            window.destroy()
        except Exception:
            pass


def _open_folder_dialog() -> None:
    path = _pick_folder()
    if path:
        _dispatch_menu("open-folder", path)


def _build_menu() -> list:
    """Build the native File/Help menu, or [] if unsupported."""
    try:
        from webview.menu import Menu, MenuAction, MenuSeparator
    except Exception:
        return []
    return [
        Menu(
            "File",
            [
                MenuAction("New Chat", lambda: _dispatch_menu("new-chat")),
                MenuAction("Open Folder...", _open_folder_dialog),
                MenuAction("Close", _close_window),
                MenuSeparator(),
                MenuAction("Settings...", lambda: _dispatch_menu("settings")),
                MenuSeparator(),
                MenuAction("Exit", _close_window),
            ],
        ),
        Menu(
            "Help",
            [
                MenuAction("About Code Wood", lambda: _dispatch_menu("about")),
            ],
        ),
    ]


def main() -> int:
    backend = BackendProcess()
    try:
        port, token = backend.start()
    except BackendError as exc:
        # No window yet; surface the failure in a minimal error window.
        webview.create_window(WINDOW_TITLE, html=f"<h2>Failed to start backend</h2><p>{exc}</p>")
        webview.start()
        return 1

    url = resolve_frontend_url(port, token)
    window = webview.create_window(
        WINDOW_TITLE,
        url=url,
        width=1280,
        height=860,
        min_size=(960, 640),
        js_api=HostApi(),
    )

    def _on_closed() -> None:
        backend.stop()

    window.events.closed += _on_closed

    try:
        webview.start(gui=_preferred_gui(), menu=_build_menu())
    finally:
        backend.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
