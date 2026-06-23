"""Desktop GUI host.

Creates a WebView2 (Windows) / system WebView window via pywebview,
launches the Code Wood backend in serve mode, loads the TypeScript
frontend, and tears the backend down when the window closes.
"""

from __future__ import annotations

import os
import sys


def _prefer_x11_on_wayland() -> None:
    """Ask GTK to use the X11 backend when running under Wayland (Linux).

    pywebview's GTK frameless dragging and programmatic window moves rely on
    ``window.move``, which Wayland compositors forbid — so title-bar dragging
    and "restore from maximize" silently fail under Wayland (e.g. the default
    WSLg session). The X11 path (XWayland) supports these operations. We only
    set ``GDK_BACKEND`` when it is unset, so an explicit user override always
    wins, and only on Linux under Wayland. This must run before ``webview`` /
    GTK is imported, since GDK reads the backend at initialization.
    """
    if sys.platform == "win32" or os.name == "nt":
        return
    if os.environ.get("GDK_BACKEND"):
        return
    on_wayland = bool(os.environ.get("WAYLAND_DISPLAY")) or (
        str(os.environ.get("XDG_SESSION_TYPE", "")).lower() == "wayland"
    )
    if on_wayland:
        os.environ["GDK_BACKEND"] = "x11"


_prefer_x11_on_wayland()

import webview  # noqa: E402 - must follow the GDK_BACKEND setup above

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


def _pick_files(directory: str = "") -> list[str]:
    """Open a native multi-select file picker; return selected paths.

    ``directory`` is an optional starting folder hint. Passing the GUI's
    workspace root keeps the dialog anchored inside the workspace instead of
    reusing pywebview's last-remembered location (which often points outside
    the project after switching workspaces).
    """
    window = webview.active_window()
    if window is None:
        return []
    try:
        kwargs: dict = {"allow_multiple": True}
        # Validate the starting directory before passing it to pywebview so a
        # malformed/empty/non-existent path silently falls back to the default
        # rather than raising. ``directory`` from the renderer is always a
        # string but may be the empty string when no workspace is bound.
        try:
            start = str(directory or "").strip()
        except Exception:
            start = ""
        if start and os.path.isdir(start):
            kwargs["directory"] = start
        result = window.create_file_dialog(_open_dialog(), **kwargs)
    except Exception:
        return []
    if not result:
        return []
    if isinstance(result, (list, tuple)):
        return [str(p) for p in result if p]
    return [str(result)]


def _pick_image(directory: str = "") -> str:
    """Open a native single-select image picker; return the chosen path or "".

    Restricts the dialog to common image types. The returned path is still
    re-validated server-side before any copy, so this filter is a convenience
    only and not a trust boundary.
    """
    window = webview.active_window()
    if window is None:
        return ""
    try:
        kwargs: dict = {
            "allow_multiple": False,
            "file_types": ("Image files (*.png;*.jpg;*.jpeg;*.webp;*.gif;*.bmp)",),
        }
        try:
            start = str(directory or "").strip()
        except Exception:
            start = ""
        if start and os.path.isdir(start):
            kwargs["directory"] = start
        result = window.create_file_dialog(_open_dialog(), **kwargs)
    except Exception:
        return ""
    if not result:
        return ""
    if isinstance(result, (list, tuple)):
        return str(result[0]) if result else ""
    return str(result)


class HostApi:
    """Bridge exposed to the frontend as ``window.pywebview.api``.

    The window is frameless and renders its own title bar (toggle, menus and
    window controls) in the web layer, so these methods drive the OS window.
    """

    def __init__(self) -> None:
        self._maximized = False

    def open_external(self, url: str) -> bool:
        """Open an http/https URL in the user's default system browser.

        Validates the scheme strictly (only http/https) so the bridge can't
        be coerced into launching arbitrary local handlers (file:, etc.).
        """
        raw = str(url or "").strip()
        if not raw:
            return False
        try:
            from urllib.parse import urlparse

            parsed = urlparse(raw)
        except Exception:
            return False
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False
        try:
            import webbrowser

            webbrowser.open(raw)
            return True
        except Exception:
            return False

    def pick_folder(self) -> str:
        return _pick_folder()

    def pick_files(self, directory: str = "") -> list[str]:
        return _pick_files(directory)

    def pick_image(self, directory: str = "") -> str:
        return _pick_image(directory)

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
                self._restore_window(window)
            else:
                window.maximize()
            self._maximized = not self._maximized
        except Exception:
            pass
        return self._maximized

    @staticmethod
    def _restore_window(window) -> None:
        """Un-maximize a window across pywebview backends.

        On Windows/EdgeChromium ``window.restore()`` works. On the GTK backend
        ``restore()`` does not always un-maximize the underlying GtkWindow, so
        fall back to calling ``unmaximize()`` on the native GTK handle directly
        (run on the GTK main thread via ``GLib.idle_add`` to stay thread-safe).
        """
        restored = False
        try:
            window.restore()
            restored = True
        except Exception:
            restored = False
        if sys.platform == "win32":
            return
        # GTK-specific fallback: reach the native GtkWindow and unmaximize it.
        gtk_window = getattr(window, "gtk", None) or getattr(window, "native", None)
        if gtk_window is None:
            return
        try:
            from gi.repository import GLib  # type: ignore

            def _do_unmaximize() -> bool:
                try:
                    gtk_window.unmaximize()
                except Exception:
                    pass
                return False  # one-shot idle callback

            GLib.idle_add(_do_unmaximize)
        except Exception:
            if not restored:
                try:
                    gtk_window.unmaximize()
                except Exception:
                    pass

    def close_window(self) -> None:
        # Destroy the window, then make sure the whole app actually quits. On
        # the GTK/WebKit backend ``window.destroy()`` alone can leave the GTK
        # main loop running, so ``webview.start()`` never returns and the
        # process lingers. Calling ``destroy()`` on every window and (when
        # available) the top-level ``webview`` API gives a clean shutdown
        # across backends without changing Windows behavior.
        try:
            windows = list(getattr(webview, "windows", []) or [])
        except Exception:
            windows = []
        active = webview.active_window()
        if active is not None and active not in windows:
            windows.append(active)
        for win in windows:
            try:
                win.destroy()
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
        # No window yet; surface the failure in a minimal error window. Escape
        # the backend's output so a stray ``<`` in a traceback can't break the
        # markup, and preserve line breaks so multi-line diagnostics read
        # cleanly.
        import html

        safe = html.escape(str(exc)).replace("\n", "<br>")
        error_html = (
            "<body style=\"font-family:Segoe UI,Arial,sans-serif;"
            "padding:24px;color:#111;\">"
            "<h2>Failed to start backend</h2>"
            f"<pre style=\"white-space:pre-wrap;word-break:break-word;"
            "font-size:13px;line-height:1.5;\">"
            f"{safe}</pre></body>"
        )
        webview.create_window(WINDOW_TITLE, html=error_html)
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

    # Opt-in debugging: ``CODEWOOD_GUI_DEBUG=1`` enables pywebview's web
    # inspector (right-click → Inspect Element) so frontend errors behind a
    # blank window can be diagnosed. Off by default to keep production builds
    # locked down.
    debug = str(os.environ.get("CODEWOOD_GUI_DEBUG", "")).strip() not in ("", "0", "false", "False")

    try:
        webview.start(gui=_preferred_gui(), debug=debug)
    finally:
        backend.stop()
    # Guarantee the process terminates. On the GTK/WebKit backend stray helper
    # threads (WebKit network/web processes, GLib workers) can otherwise keep
    # the interpreter alive after the window closes, so a plain ``return`` would
    # hang. ``os._exit`` is safe here: the backend subprocess has already been
    # stopped above and there is no further Python cleanup to run. Windows'
    # EdgeChromium backend returns cleanly, so restrict the hard exit to other
    # platforms to keep Windows behavior unchanged.
    if sys.platform != "win32":
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
