"""Desktop GUI host.

Creates a WebView2 (Windows) / system WebView window via pywebview,
launches the Code Wood backend in serve mode, loads the TypeScript
frontend, and tears the backend down when the window closes.
"""

from __future__ import annotations

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
    """Bridge exposed to the frontend as ``window.pywebview.api``.

    The window is frameless and renders its own title bar (toggle, menus and
    window controls) in the web layer, so these methods drive the OS window.
    """

    def __init__(self) -> None:
        self._maximized = False

    def pick_folder(self) -> str:
        return _pick_folder()

    def minimize(self) -> None:
        window = webview.active_window()
        if window is not None:
            try:
                window.minimize()
            except Exception:
                pass

    def toggle_maximize(self) -> bool:
        window = webview.active_window()
        if window is None:
            return self._maximized
        try:
            if self._maximized:
                window.restore()
            else:
                window.maximize()
            self._maximized = not self._maximized
        except Exception:
            pass
        return self._maximized

    def close_window(self) -> None:
        window = webview.active_window()
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass


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
        frameless=True,
        easy_drag=False,
        js_api=HostApi(),
    )

    def _on_closed() -> None:
        backend.stop()

    window.events.closed += _on_closed

    try:
        webview.start(gui=_preferred_gui())
    finally:
        backend.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
