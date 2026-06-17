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

MIN_WIDTH = 960
MIN_HEIGHT = 640


def _preferred_gui() -> str | None:
    # On Windows, force the EdgeChromium (WebView2) renderer.
    if sys.platform == "win32":
        return "edgechromium"
    return None


def _folder_dialog():
    """Resolve the folder-picker dialog kind across pywebview versions."""
    file_dialog = getattr(webview, "FileDialog", None)
    if file_dialog is not None and hasattr(file_dialog, "FOLDER"):
        return file_dialog.FOLDER
    return webview.FOLDER_DIALOG


def _open_dialog():
    """Resolve the open-file dialog kind across pywebview versions."""
    file_dialog = getattr(webview, "FileDialog", None)
    if file_dialog is not None and hasattr(file_dialog, "OPEN"):
        return file_dialog.OPEN
    return webview.OPEN_DIALOG


def _pick_folder() -> str:
    """Open a native folder picker; return the selected path or ""."""
    window = webview.active_window()
    if window is None:
        return ""
    try:
        result = window.create_file_dialog(_folder_dialog())
    except Exception:
        return ""
    if not result:
        return ""
    return result[0] if isinstance(result, (list, tuple)) else str(result)


def _pick_files() -> list[str]:
    """Open a native multi-select file picker; return selected paths."""
    window = webview.active_window()
    if window is None:
        return []
    try:
        result = window.create_file_dialog(
            _open_dialog(), allow_multiple=True
        )
    except Exception:
        return []
    if not result:
        return []
    if isinstance(result, (list, tuple)):
        return [str(p) for p in result if p]
    return [str(result)]


class HostApi:
    """Bridge exposed to the frontend as ``window.pywebview.api``.

    The window is frameless and renders its own title bar (toggle, menus and
    window controls) in the web layer, so these methods drive the OS window.
    """

    def __init__(self) -> None:
        self._maximized = False

    def pick_folder(self) -> str:
        return _pick_folder()

    def pick_files(self) -> list[str]:
        return _pick_files()

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

    def set_window_geometry(self, x: float, y: float, width: float, height: float) -> None:
        """Resize/move the OS window (drives the web-rendered resize grips).

        WebView2 covers the frameless window edges and swallows the native
        resize hit-test, so the frontend implements edge resizing and calls
        back here. Width/height are clamped to the minimum window size.
        """
        window = webview.active_window()
        if window is None:
            return
        try:
            w = max(MIN_WIDTH, int(round(width)))
            h = max(MIN_HEIGHT, int(round(height)))
            nx = int(round(x))
            ny = int(round(y))
        except (TypeError, ValueError):
            return
        try:
            window.resize(w, h)
        except Exception:
            pass
        try:
            window.move(nx, ny)
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
