"""Desktop GUI host.

Creates a WebView2 (Windows) / system WebView window via pywebview,
launches the Code Wood backend in serve mode, loads the TypeScript
frontend, and tears the backend down when the window closes.

On macOS the title-bar close button only hides the window (the app keeps
running so the backend and any running tasks stay alive); clicking the
Dock icon brings the window back, and Cmd+Q / "Quit Code Wood" quits.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path


def _prefer_wayland_when_available() -> None:
    if sys.platform == "win32" or os.name == "nt":
        return
    return


_prefer_wayland_when_available()

import webview  # noqa: E402

from backend import BackendError, BackendProcess
from bridge import resolve_frontend_url
from browser_overlay import BrowserOverlay
from notifier import TaskNotifier


WINDOW_TITLE = "Code Wood"

# NSMenuItem does not retain its target, so the menu dispatch object must be
# kept alive for the lifetime of the process or AppKit will nil it out.
_MACSOS_MENU_TARGETS: list[object] = []

# Holds the main window and host API for the macOS Dock-icon reopen handler
# (see ``_install_macos_dock_reopen``). Filled in by ``main()`` once the
# window exists; read from the AppKit reopen callback.
_MACOS_REOPEN_STATE: dict = {}


MIN_WIDTH = 960
MIN_HEIGHT = 640


def _preferred_gui() -> str | None:
    # On Windows, force the EdgeChromium (WebView2) renderer.
    if sys.platform == "win32":
        return "edgechromium"
    return None


def _task_notify_enabled() -> bool:
    """Whether the native task-completion notification is enabled.

    On by default so a task that finishes while the window is minimized or
    covered still reaches the user; set ``CODEWOOD_TASK_NOTIFY=0`` to opt out.
    """
    raw = str(os.environ.get("CODEWOOD_TASK_NOTIFY", "")).strip().lower()
    return raw not in ("0", "false", "no", "off")





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


def _save_dialog():
    """Resolve the save-file dialog kind across pywebview versions."""
    file_dialog = getattr(webview, "FileDialog", None)
    if file_dialog is not None and hasattr(file_dialog, "SAVE"):
        return file_dialog.SAVE
    return webview.SAVE_DIALOG


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


def _save_file_dialog() -> str:
    """Open a native Save As dialog for markdown files; return the chosen path or ""."""
    window = webview.active_window()
    if window is None:
        return ""
    try:
        result = window.create_file_dialog(
            _save_dialog(),
            save_filename="chat-export.md",
            file_types=("Markdown files (*.md)",),
        )
    except Exception:
        return ""
    if not result:
        return ""
    return result[0] if isinstance(result, (list, tuple)) else str(result)


def _get_window_geometry(window) -> tuple[int, int, int, int]:
    """Return (x, y, width, height) of a pywebview window."""
    try:
        x = int(window.x)
        y = int(window.y)
        w = int(window.width)
        h = int(window.height)
        return x, y, w, h
    except Exception:
        return 0, 0, 960, 640


def _get_screen_work_area_win32(window) -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) of the monitor work area for *window* on Windows."""
    import ctypes
    from ctypes import wintypes

    hwnd = _pywebview_window_hwnd(window)
    if hwnd is None:
        return 0, 0, 1920, 1040  # sensible fallback

    user32 = ctypes.windll.user32
    MONITOR_DEFAULTTONEAREST = 2
    hmonitor = user32.MonitorFromWindow(wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST)

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if user32.GetMonitorInfoW(hmonitor, ctypes.byref(mi)):
        return (mi.rcWork.left, mi.rcWork.top,
                mi.rcWork.right, mi.rcWork.bottom)

    return 0, 0, 1920, 1040


def _get_vertical_max_geometry_win32(window) -> tuple[int, int, int, int, int, int]:
    """Return (x, y, w, h, new_y, new_h) for vertical-max on Windows.

    Work area coordinates from Win32 are in raw physical pixels, but
    pywebview's ``window.x/y/width/height`` are in logical (CSS/DPI-
    virtualized) pixels.  We use ``MonitorFromWindow`` (which takes the
    HWND directly and avoids any coordinate-unit mismatch) to identify
    the correct monitor, then convert its raw work area to logical
    pixels via ``GetDpiForMonitor``.
    """
    import ctypes
    from ctypes import wintypes

    cur_x, cur_y, cur_w, cur_h = _get_window_geometry(window)

    hwnd = _pywebview_window_hwnd(window)
    if hwnd is None:
        return cur_x, cur_y, cur_w, cur_h, 0, cur_h

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

    user32 = ctypes.windll.user32
    shcore = ctypes.windll.shcore
    MONITOR_DEFAULTTONEAREST = 2

    hmonitor = user32.MonitorFromWindow(
        wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST
    )

    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if user32.GetMonitorInfoW(hmonitor, ctypes.byref(mi)):
        raw_top = mi.rcWork.top
        raw_bottom = mi.rcWork.bottom
        raw_h = raw_bottom - raw_top

        dpi_scale = 1.0
        try:
            dpi_x = wintypes.UINT()
            dpi_y = wintypes.UINT()
            if shcore.GetDpiForMonitor(
                hmonitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)
            ) == 0 and dpi_x.value > 0:
                dpi_scale = dpi_x.value / 96.0
        except Exception:
            pass

        wa_top = int(round(raw_top / dpi_scale))
        wa_height = int(round(raw_h / dpi_scale))
        return cur_x, cur_y, cur_w, cur_h, wa_top, wa_height

    return cur_x, cur_y, cur_w, cur_h, 0, cur_h


def _get_vertical_max_geometry_fallback(window) -> tuple[int, int, int, int, int, int]:
    """Return (x, y, w, h, new_y, new_h) for vertical-max on non-Windows."""
    cur_x, cur_y, cur_w, cur_h = _get_window_geometry(window)
    try:
        wa_top = 0
        wa_height = window.tk.winfo_screenheight()
    except Exception:
        wa_top = 0
        wa_height = 1080
    return cur_x, cur_y, cur_w, cur_h, wa_top, wa_height


def _pywebview_window_hwnd(window) -> int | None:
    """Extract the Win32 HWND from a pywebview window, or None."""
    try:
        native = getattr(window, "native", None)
        if native is None:
            return None
        handle = native.Handle
        return int(handle.ToInt64() if hasattr(handle, "ToInt64") else handle)
    except Exception:
        return None


def _enable_taskbar_minimize_win32(hwnd: int) -> None:
    """Let the taskbar button minimize/restore the frameless main window.

    pywebview builds frameless windows with ``FormBorderStyle.None``, whose
    Win32 style lacks ``WS_MINIMIZEBOX``. The shell minimizes a foreground
    window via ``WM_SYSCOMMAND``/``SC_MINIMIZE``, which ``DefWindowProc`` only
    honors when that style bit is present — so clicking the taskbar icon does
    nothing. Add ``WS_MINIMIZEBOX`` (and ``WS_MAXIMIZEBOX``) to the live
    window style after the native window exists. Best-effort: failures are
    swallowed so a styling hiccup never breaks the app.
    """
    if sys.platform != "win32" or not hwnd:
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        GWL_STYLE = -16
        WS_MINIMIZEBOX = 0x00020000
        WS_MAXIMIZEBOX = 0x00010000
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_FRAMECHANGED = 0x0020

        get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)

        style = get_long(wintypes.HWND(hwnd), GWL_STYLE)
        style |= WS_MINIMIZEBOX | WS_MAXIMIZEBOX
        set_long(wintypes.HWND(hwnd), GWL_STYLE, style)
        user32.SetWindowPos(
            wintypes.HWND(hwnd),
            0,
            0,
            0,
            0,
            0,
            SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
    except Exception:
        pass


class HostApi:
    """Bridge exposed to the frontend as ``window.pywebview.api``.

    The window is frameless and renders its own title bar (toggle, menus and
    window controls) in the web layer, so these methods drive the OS window.
    """

    def __init__(self) -> None:
        self._maximized = False
        self._vertically_maximized = False
        self._always_on_top = False
        self._pre_vertical_max_geometry: tuple[int, int, int, int] | None = None
        # Frame captured before a macOS maximize so restore() can bring the
        # window back to its pre-maximize size and position (pywebview's
        # Cocoa backend does not remember it). None on other platforms.
        self._pre_maximize_geometry: tuple[int, int, int, int] | None = None
        # Set by ``main()`` once the overlay browser window exists.
        self._overlay: BrowserOverlay | None = None
        self._backend_port: int = 0
        self._backend_url: str = ""
        self._backend_token: str = ""

    def attach_overlay(self, overlay: "BrowserOverlay") -> None:
        self._overlay = overlay

    def set_backend(self, port: int, token: str) -> None:
        self._backend_port = int(port)
        self._backend_url = f"http://127.0.0.1:{port}"
        self._backend_token = token

    def backend_info(self) -> dict:
        """Current backend endpoint for the frontend.

        The serve process binds an ephemeral port and mints a fresh token on
        every launch, so the frontend polls this after a disconnect and
        rebuilds its API client when ``port``/``token`` change (crash-restart).
        """
        return {"port": self._backend_port, "token": self._backend_token}

    # -- embedded browser overlay (renderer-callable) ---------------------
    #
    # The frontend reports the on-screen rectangle of the right-panel browser
    # viewport (logical px, relative to the main window's client area) and
    # toggles visibility; the host positions/sizes the overlay window to match.

    def browser_overlay_set_bounds(
        self, x: float, y: float, width: float, height: float
    ) -> bool:
        if self._overlay is None:
            return False
        return self._overlay.set_bounds(x, y, width, height)

    def browser_overlay_show(self) -> bool:
        if self._overlay is None:
            return False
        return self._overlay.show()

    def browser_overlay_hide(self) -> bool:
        if self._overlay is None:
            return False
        return self._overlay.hide()

    def browser_overlay_command(
        self, action: str, url: str = "", script: str = ""
    ) -> dict:
        """Execute a model-issued browser command against the overlay window.

        Returns the structured result the frontend posts back to the waiting
        tool via ``/browser-result``. Errors are returned (not raised) so a
        misbehaving page can't break the js_api bridge.
        """
        if self._overlay is None:
            return {"success": False, "error": "overlay unavailable"}
        try:
            return self._overlay.run_command(
                str(action or ""), str(url or ""), str(script or "")
            )
        except Exception as exc:  # pragma: no cover - defensive
            return {"success": False, "error": f"overlay command failed: {exc}"}

    def browser_overlay_preview_path(self, path: str) -> bool:
        """Re-preview a local HTML file in the overlay browser.

        Called from the frontend when the user clicks a preview-path link
        in the tool feedback.  POSTs to the backend's ``/preview-local-file``
        to re-read the file and save a fresh bridged copy, then opens the
        returned URL in the overlay.
        """
        if self._overlay is None or not self._backend_url:
            return False
        try:
            import json
            from urllib.request import Request, urlopen
            from urllib.parse import urlencode

            query = urlencode({"token": self._backend_token})
            body = json.dumps({"path": path}).encode("utf-8")
            req = Request(
                f"{self._backend_url}/preview-local-file?{query}",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urlopen(req, timeout=10)
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                rel_url = data.get("url", "")
                if rel_url:
                    # The overlay's WebView has no base URL context, so build
                    # an absolute URL from the backend's origin.
                    abs_url = rel_url
                    if rel_url.startswith("/"):
                        abs_url = f"{self._backend_url}{rel_url}"
                    # First ensure the overlay is visible, then navigate.
                    self.browser_overlay_show()
                    self.browser_overlay_command("open_preview", abs_url)
                    return True
        except Exception:
            pass
        return False

    def browser_overlay_set_passthrough(self, enabled: bool = True) -> bool:
        """Toggle the overlay's key-window status on macOS.

        When *enabled* is True the overlay cannot become the key window, so
        the main window keeps focus and the panel resizer works.
        """
        if self._overlay is None:
            return False
        return self._overlay.set_passthrough(enabled)

    def host_platform(self) -> str:
        """Report the host OS family so the frontend can pick drag strategies.

        Returns ``"win32"`` on Windows and ``"darwin"`` on macOS — the
        platforms where pywebview's native ``pywebview-drag-region`` moves the
        frameless window reliably (EdgeChromium and Cocoa/WebKit). Returns
        ``"gtk"`` on Linux/WSL, where the frontend must drive moves/resizes
        through the WM-native ``start_window_drag`` / ``start_window_resize``
        helpers and must NOT also attach the pywebview drag region, which would
        fight the WM drag with its own unreliable ``window.move`` loop.
        """
        if sys.platform == "win32":
            return "win32"
        if sys.platform == "darwin":
            return "darwin"
        return "gtk"

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

    def save_file_dialog(self) -> str:
        return _save_file_dialog()

    def get_clipboard_text(self) -> str:
        """Read the system clipboard as text.

        Used by the composer's right-click Paste menu. Reading the clipboard
        from the web layer (``execCommand("paste")`` /
        ``navigator.clipboard.readText``) makes Chromium pop a permission
        prompt on the ``file://`` origin ("This file wants to see text and
        images copied to the clipboard"), so the host reads it natively
        instead. Returns "" when the clipboard holds no text.
        """
        try:
            if sys.platform == "win32":
                import ctypes

                user32 = ctypes.windll.user32
                kernel32 = ctypes.windll.kernel32
                # ctypes defaults Win32 args/returns to 32-bit c_int, which
                # truncates the 64-bit clipboard handles/pointers on a 64-bit
                # Python build (crash on read / overflow on call). Declare the
                # real pointer-sized types first.
                user32.OpenClipboard.argtypes = [ctypes.c_void_p]
                user32.GetClipboardData.argtypes = [ctypes.c_uint]
                user32.GetClipboardData.restype = ctypes.c_void_p
                kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
                kernel32.GlobalLock.restype = ctypes.c_wchar_p
                kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
                if not user32.OpenClipboard(None):
                    return ""
                try:
                    handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
                    if not handle:
                        return ""
                    text = kernel32.GlobalLock(handle)
                    if text is None:
                        return ""
                    try:
                        return text
                    finally:
                        kernel32.GlobalUnlock(handle)
                finally:
                    user32.CloseClipboard()
            else:
                # Linux/WSL: common CLI clipboard readers.
                import shutil
                import subprocess

                for cmd in (
                    ["xclip", "-selection", "clipboard", "-o"],
                    ["xsel", "--clipboard", "--output"],
                ):
                    if shutil.which(cmd[0]):
                        out = subprocess.run(
                            cmd, capture_output=True, timeout=2
                        )
                        if out.returncode == 0:
                            return out.stdout.decode("utf-8", "replace")
        except Exception:
            pass
        return ""

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
        # On GTK/WSL prefer the native maximize/unmaximize so the window snaps
        # flush to the screen edges (the frameless window's top edge otherwise
        # leaves a gap) and the WM — not a stored geometry — owns the restore
        # size. Track the resulting state from the GtkWindow itself to avoid the
        # toggle flag drifting out of sync.
        if sys.platform != "win32":
            gtk_window = self._gtk_native_window(window)
            if gtk_window is not None and self._toggle_maximize_gtk(gtk_window):
                return self._maximized
        try:
            if self._maximized:
                self._restore_window(window)
            else:
                # On macOS pywebview's Cocoa maximize() only grows the window
                # to fill the screen; it does not remember the previous frame,
                # and restore() only un-minimizes. Capture the geometry now so
                # restore can bring the window back to its pre-maximize state.
                if sys.platform == "darwin":
                    self._pre_maximize_geometry = _get_window_geometry(window)
                window.maximize()
            self._maximized = not self._maximized
        except Exception:
            pass
        return self._maximized

    def toggle_vertical_maximize(self) -> bool:
        """Toggle vertical maximize: snap the window to screen top and fill
        screen work-area height, or restore the previous position/size.
        Double-click on the top or bottom resize border triggers this."""
        window = webview.active_window()
        if window is None:
            return self._vertically_maximized
        if self._vertically_maximized:
            self._restore_vertical(window)
        else:
            self._vertical_maximize(window)
        return self._vertically_maximized

    def _vertical_maximize(self, window) -> None:
        prev_x, prev_y, prev_w, prev_h = _get_window_geometry(window)
        self._pre_vertical_max_geometry = (prev_x, prev_y, prev_w, prev_h)
        if sys.platform == "win32":
            cur_x, cur_y, cur_w, cur_h, wa_top, wa_height = _get_vertical_max_geometry_win32(window)
        else:
            cur_x, cur_y, cur_w, cur_h, wa_top, wa_height = _get_vertical_max_geometry_fallback(window)
        try:
            window.resize(cur_w, wa_height)
        except Exception:
            pass
        try:
            window.move(cur_x, wa_top)
        except Exception:
            pass
        if self._overlay is not None:
            try:
                self._overlay.resync()
            except Exception:
                pass
        self._vertically_maximized = True

    def _restore_vertical(self, window) -> None:
        prev = self._pre_vertical_max_geometry
        if prev is None:
            return
        _, prev_y, _, prev_h = prev
        # Restore only the vertical state: the pre-maximize top edge and
        # height. Keep the current width (and horizontal position) so a
        # width adjustment made while vertically maximized is preserved.
        cur_x, _cur_y, cur_w, _cur_h = _get_window_geometry(window)
        try:
            window.resize(cur_w, prev_h)
        except Exception:
            pass
        try:
            window.move(cur_x, prev_y)
        except Exception:
            pass
        if self._overlay is not None:
            try:
                self._overlay.resync()
            except Exception:
                pass
        self._pre_vertical_max_geometry = None
        self._vertically_maximized = False

    def _toggle_maximize_gtk(self, gtk_window) -> bool:
        """Toggle maximize on the native GtkWindow; sync ``_maximized``.

        Returns ``True`` when the GTK path handled the toggle, ``False`` to
        let the caller fall back to pywebview's cross-backend path.
        """
        try:
            from gi.repository import GLib  # type: ignore
        except Exception:
            return False
        try:
            currently_max = bool(gtk_window.is_maximized())
        except Exception:
            currently_max = self._maximized
        want_max = not currently_max

        def _apply() -> bool:
            try:
                if want_max:
                    gtk_window.maximize()
                else:
                    gtk_window.unmaximize()
            except Exception:
                pass
            return False  # one-shot idle callback

        try:
            GLib.idle_add(_apply)
        except Exception:
            return False
        self._maximized = want_max
        return True

    def _restore_window(self, window) -> None:
        """Un-maximize a window across pywebview backends.

        On Windows/EdgeChromium ``window.restore()`` works. On the GTK backend
        ``restore()`` does not always un-maximize the underlying GtkWindow, so
        fall back to calling ``unmaximize()`` on the native GTK handle directly
        (run on the GTK main thread via ``GLib.idle_add`` to stay thread-safe).
        On macOS/Cocoa ``restore()`` only un-minimizes and leaves the window
        at full screen, so restore the frame captured before maximize instead.
        """
        if sys.platform == "darwin":
            self._restore_macos_geometry(window)
            return
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

    def _restore_macos_geometry(self, window) -> None:
        """Restore the macOS window to its pre-maximize frame.

        pywebview's Cocoa ``restore()`` only un-minimizes and never puts the
        window back to its pre-maximize size/position, so keep our own copy of
        the frame captured in :meth:`toggle_maximize` and re-apply it.
        """
        prev = self._pre_maximize_geometry
        if prev is None:
            return
        prev_x, prev_y, prev_w, prev_h = prev
        try:
            window.resize(prev_w, prev_h)
        except Exception:
            pass
        try:
            window.move(prev_x, prev_y)
        except Exception:
            pass
        if self._overlay is not None:
            try:
                self._overlay.resync()
            except Exception:
                pass
        self._pre_maximize_geometry = None

    def close_window(self) -> None:
        # macOS: the red close button only hides the window — the macOS
        # convention is that closing the last window must not quit the app.
        # The process (and with it the backend and any running tasks) stays
        # alive, and clicking the Dock icon brings the window back (see
        # ``_install_macos_dock_reopen``). Quitting remains with Cmd+Q /
        # "Quit Code Wood" in the menu bar.
        if sys.platform == "darwin":
            window = _MACOS_REOPEN_STATE.get("window") or webview.active_window()
            if window is not None:
                try:
                    window.hide()
                except Exception:
                    pass
            # The overlay browser floats independently of the main window on
            # macOS; hide it too (keeping its want-visible intent) so it can
            # never resurface as an orphan while the main window is hidden.
            # ``resync()`` on reopen re-shows it exactly like the minimize
            # path does.
            if self._overlay is not None:
                try:
                    self._overlay.suspend_for_main_minimized()
                except Exception:
                    pass
            return
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

    @staticmethod
    def _gtk_native_window(window):
        """Return the native ``GtkWindow`` for a pywebview window, or ``None``.

        Only meaningful on the GTK/WebKit backend (Linux/WSL); other backends
        expose no such handle and callers must fall back accordingly.
        """
        if window is None:
            return None
        return getattr(window, "gtk", None) or getattr(window, "native", None)

    # Map the eight resize directions used by the web-rendered grips to the
    # GDK window-edge constants. Kept as strings so the frontend contract stays
    # backend-agnostic; resolved to ``Gdk.WindowEdge`` at call time.
    _GDK_EDGE_NAMES = {
        "nw": "NORTH_WEST",
        "n": "NORTH",
        "ne": "NORTH_EAST",
        "w": "WEST",
        "e": "EAST",
        "sw": "SOUTH_WEST",
        "s": "SOUTH",
        "se": "SOUTH_EAST",
    }

    def start_window_drag(self) -> bool:
        """Begin a window-manager-native move drag (GTK/WSL only).

        Programmatic ``window.move`` is unreliable on WSLg/X11 with
        mixed-DPI multi-monitor setups: the window fails to follow the
        cursor and can lose its decorations/controls. Handing the drag to
        the window manager via ``begin_move_drag`` makes the frameless
        window behave like any other native GTK app. Returns ``False`` when
        the GTK path is unavailable (e.g. Windows), so the frontend can keep
        using the ``pywebview-drag-region`` fallback there (and on macOS,
        where Cocoa uses that same native drag region).
        """
        if sys.platform in ("win32", "darwin"):
            return False
        gtk_window = self._gtk_native_window(webview.active_window())
        if gtk_window is None:
            return False
        return self._begin_gtk_drag(gtk_window, edge=None)

    def start_window_resize(self, direction: str) -> bool:
        """Begin a window-manager-native resize drag (GTK/WSL only).

        Mirrors :meth:`start_window_drag` for the resize grips so resizing
        across mixed-DPI monitors is handled by the compositor rather than
        by JS-computed geometry pushed through ``set_window_geometry``. On
        Windows and macOS the frontend falls back to the JS geometry path.
        """
        if sys.platform in ("win32", "darwin"):
            return False
        edge = self._GDK_EDGE_NAMES.get(str(direction or "").strip().lower())
        if edge is None:
            return False
        gtk_window = self._gtk_native_window(webview.active_window())
        if gtk_window is None:
            return False
        return self._begin_gtk_drag(gtk_window, edge=edge)

    @staticmethod
    def _begin_gtk_drag(gtk_window, edge) -> bool:
        """Kick off a GTK move/resize drag anchored at the live pointer.

        ``edge`` is ``None`` for a move, or a ``Gdk.WindowEdge`` member name
        for a resize. The pointer's root coordinates are read from the
        default GDK seat so the drag starts exactly under the cursor,
        matching the user's grab point across monitors. All work is posted to
        the GTK main thread to stay thread-safe.
        """
        try:
            from gi.repository import Gdk, GLib  # type: ignore
        except Exception:
            return False

        def _do_drag() -> bool:
            try:
                display = Gdk.Display.get_default()
                seat = display.get_default_seat() if display is not None else None
                pointer = seat.get_pointer() if seat is not None else None
                gdk_window = gtk_window.get_window()
                if pointer is None or gdk_window is None:
                    return False
                # ``get_device_position`` returns (window, x, y) relative to the
                # window; use root coordinates for the WM drag anchor instead.
                _scr, root_x, root_y = pointer.get_position()
                timestamp = Gdk.CURRENT_TIME
                if edge is None:
                    gtk_window.begin_move_drag(1, root_x, root_y, timestamp)
                else:
                    gtk_window.begin_resize_drag(
                        getattr(Gdk.WindowEdge, edge), 1, root_x, root_y, timestamp
                    )
            except Exception:
                pass
            return False  # one-shot idle callback

        try:
            GLib.idle_add(_do_drag)
            return True
        except Exception:
            return False

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
        # The overlay browser is positioned relative to the main window's
        # origin, so re-sync it after the main window moves/resizes itself.
        if self._overlay is not None:
            try:
                self._overlay.resync()
            except Exception:
                pass

    def toggle_always_on_top(self) -> bool:
        """Toggle window always-on-top state. Returns the new state."""
        window = webview.active_window()
        if window is None:
            return self._always_on_top
        try:
            self._always_on_top = not self._always_on_top
            
            # Platform-specific implementation
            if sys.platform == "win32":
                import ctypes
                from ctypes import wintypes
                
                hwnd = _pywebview_window_hwnd(window)
                if hwnd is not None:
                    user32 = ctypes.windll.user32
                    SWP_NOMOVE = 0x0002
                    SWP_NOSIZE = 0x0001
                    SWP_NOACTIVATE = 0x0010
                    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                    c_hwnd = ctypes.c_void_p(hwnd)
                    if self._always_on_top:
                        user32.SetWindowPos(c_hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, flags)
                    else:
                        user32.SetWindowPos(c_hwnd, ctypes.c_void_p(-2), 0, 0, 0, 0, flags)
            else:
                # Try pywebview's on_top attribute for other platforms
                try:
                    window.on_top = self._always_on_top
                except Exception:
                    # Fallback to keep the state but don't crash
                    pass
        except Exception:
            pass
        return self._always_on_top


def _find_macos_app_icon() -> Path | None:
    """Locate an ``app_icon.icns`` file to use as the macOS Dock icon."""
    if sys.platform != "darwin":
        return None
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        # In the installed .app the GUI process lives at
        # Code Wood.app/Contents/Resources/codewood/codewood, while the
        # canonical icon sits at Code Wood.app/Contents/Resources/app_icon.icns.
        candidates.append(exe.parent.parent / "app_icon.icns")
        candidates.append(
            exe.parent / "codewood-gui.app" / "Contents" / "Resources" / "app_icon.icns"
        )
        # Walk up to the nearest enclosing .app bundle and use its icon.
        for parent in exe.parents:
            if (parent / "Contents" / "Info.plist").is_file():
                candidates.append(parent / "Contents" / "Resources" / "app_icon.icns")
                break
    else:
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "codewood_assets" / "app_icon.icns")
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _macos_owning_app_bundle() -> Path | None:
    """Return the ``.app`` bundle whose main executable is this process.

    Walks up from ``sys.executable`` to the nearest enclosing ``Contents`` /
    ``Info.plist`` and reports the bundle only when its ``CFBundleExecutable``
    matches this process's binary name — i.e. this process IS the bundle's
    main executable, so LaunchServices already associates it (and its Dock
    icon) with the bundle. Returns ``None`` for helper processes inside a
    bundle and for runs outside any bundle (dev, bare onedir).
    """
    if sys.platform != "darwin":
        return None
    try:
        exe = Path(sys.executable).resolve()
        me = exe.name.lower()
        for parent in exe.parents:
            info = parent / "Contents" / "Info.plist"
            if info.is_file():
                try:
                    import plistlib

                    with open(info, "rb") as fh:
                        plist = plistlib.load(fh)
                    bundle_exe = str(plist.get("CFBundleExecutable", "")).lower()
                    if bundle_exe and bundle_exe == me:
                        return parent
                except Exception:
                    pass
                # Only the nearest enclosing bundle counts: a helper process
                # nested deeper is not owned by an outer bundle either.
                break
    except Exception:
        return None
    return None


def _apply_macos_dock_icon() -> None:
    """Make the GUI window present as a proper macOS app with the Code Wood
    Dock icon.

    Two very different situations:

    - Running as an ``.app`` bundle's main executable (the installed app, or
      ``dist/codewood-gui.app``): LaunchServices already associates the
      process with the bundle and the Dock renders ``CFBundleIconFile``
      itself — at the standard app-icon size, with the system inset and
      shadow. Overriding that with ``setApplicationIconImage_`` makes the
      Dock draw the raw full-bleed artwork edge-to-edge, which reads as an
      oversized Dock icon for as long as the app runs (the normal-size icon
      only comes back after quit). So in this case the icon is deliberately
      left to LaunchServices.

    - Running outside a bundle it owns (``codewood app`` from the console
      build, dev runs): the process would otherwise surface in the Dock as a
      generic "exec" icon, so set the regular activation policy and apply
      the ``.icns`` explicitly.
    """
    if sys.platform != "darwin":
        return
    try:
        import AppKit  # type: ignore
    except Exception:
        return
    try:
        app = AppKit.NSApplication.sharedApplication()
        # Foreground app: keeps a Dock icon, menu bar and Cmd-Tab presence.
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
        if _macos_owning_app_bundle() is not None:
            # Bundle-owned: the Dock already shows the bundle icon; don't
            # override it (see the docstring for why that looks oversized).
            return
        icon = _find_macos_app_icon()
        if icon is not None:
            image = AppKit.NSImage.alloc().initWithContentsOfFile_(str(icon))
            if image is not None:
                app.setApplicationIconImage_(image)
    except Exception:
        pass


def _install_macos_native_menu(window) -> None:
    """Replace the Cocoa main menu with a clean Code Wood menu bar.

    pywebview's built-in ``menu=[...]`` support cannot strip its hardcoded
    ``Services`` item or assign keyboard shortcuts, so on macOS we build the
    menu with AppKit and install it once the app is running. Items that drive
    app state dispatch through the frontend's ``window.__codewoodMenu`` bridge
    (see AppContext); standard items (Hide, Quit, and the Edit menu) target the
    responder chain so text editing and the standard shortcuts keep working.
    """
    if sys.platform != "darwin":
        return
    try:
        import AppKit
        from objc import nil
    except Exception:
        return
    try:
        app = AppKit.NSApplication.sharedApplication()

        class _Target(AppKit.NSObject):
            def initWithWindow_(self, win):
                self._window = win
                return self

            def codewoodAction_(self, sender):
                action = sender.representedObject()
                if not action:
                    return

                def fire() -> None:
                    try:
                        self._window.evaluate_js(
                            "window.__codewoodMenu && window.__codewoodMenu(%s, '')"
                            % json.dumps(action)
                        )
                    except Exception:
                        pass

                # Menu actions run on the main thread, but pywebview's
                # ``evaluate_js`` blocks the main run loop on a semaphore, so
                # calling it synchronously deadlocks. Dispatch in a background
                # thread (the same trick pywebview's own MenuHandler uses).
                threading.Thread(target=fire, daemon=True).start()

        target = _Target.alloc().initWithWindow_(window)
        _MACSOS_MENU_TARGETS.append(target)

        def app_item(title, selector, key="", mask=None):
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, selector, key
            )
            if selector:
                item.setTarget_(nil)
            if key:
                item.setKeyEquivalentModifierMask_(mask or AppKit.NSCommandKeyMask)
            return item

        def js_item(title, action, key=""):
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, "codewoodAction:", key
            )
            item.setTarget_(target)
            item.setRepresentedObject_(action)
            if key:
                item.setKeyEquivalentModifierMask_(AppKit.NSCommandKeyMask)
            return item

        def top_menu(title):
            menu = AppKit.NSMenu.alloc().init()
            menu.setTitle_(title)
            item = AppKit.NSMenuItem.alloc().init()
            item.setTitle_(title)
            item.setSubmenu_(menu)
            main_menu.addItem_(item)
            return menu

        main_menu = AppKit.NSMenu.alloc().init()

        # Application menu (bold, shows the app name). No Services item.
        app_menu_item = AppKit.NSMenuItem.alloc().init()
        app_menu_item.setTitle_("Code Wood")
        app_menu = AppKit.NSMenu.alloc().init()
        app_menu_item.setSubmenu_(app_menu)
        main_menu.addItem_(app_menu_item)
        app_menu.addItem_(js_item("About Code Wood", "about"))
        app_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        app_menu.addItem_(js_item("Settings…", "settings", ","))
        app_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        app_menu.addItem_(app_item("Hide Code Wood", "hide:", "h"))
        app_menu.addItem_(
            app_item(
                "Hide Others",
                "hideOtherApplications:",
                "h",
                AppKit.NSCommandKeyMask | AppKit.NSAlternateKeyMask,
            )
        )
        app_menu.addItem_(app_item("Show All", "unhideAllApplications:"))
        app_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        app_menu.addItem_(app_item("Quit Code Wood", "terminate:", "q"))

        # File: New Chat (Cmd+N), Open Folder… (Cmd+O). No Close Window.
        file_menu = top_menu("File")
        file_menu.addItem_(js_item("New Chat", "new-chat", "n"))
        file_menu.addItem_(js_item("Open Folder…", "open-folder", "o"))

        # View: Always on Top, Browser, Console, then a separate Zoom group and
        # a separate fullscreen group (so the fullscreen item doesn't pull the
        # zoom items into the same alignment bucket).
        view_menu = top_menu("View")
        view_menu.addItem_(js_item("Always on Top", "always-on-top"))
        view_menu.addItem_(js_item("Browser", "browser"))
        view_menu.addItem_(js_item("Console", "console"))
        view_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        view_menu.addItem_(js_item("Zoom In", "zoom-in", "+"))
        view_menu.addItem_(js_item("Zoom Out", "zoom-out", "-"))
        view_menu.addItem_(js_item("Actual Size", "zoom-reset", "0"))
        view_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        view_menu.addItem_(
            app_item(
                "Enter Full Screen",
                "toggleFullScreen:",
                "f",
                AppKit.NSControlKeyMask | AppKit.NSCommandKeyMask,
            )
        )

        # Help: GitHub only (no About).
        help_menu = top_menu("Help")
        help_menu.addItem_(js_item("Code Wood on GitHub", "github"))

        app.setMainMenu_(main_menu)
    except Exception:
        pass


def _install_macos_dock_reopen() -> None:
    """Re-show the hidden main window when the user clicks the Dock icon.

    On macOS the close button only hides the window (see
    ``HostApi.close_window``), so the app keeps running with a Dock icon.
    Clicking that icon fires the AppKit
    ``applicationShouldHandleReopen:hasVisibleWindows:`` delegate callback;
    without a handler macOS does nothing and the app would appear stuck
    running invisibly.

    The method is added to pywebview's shared AppDelegate *class* via
    ``objc.classAddMethod`` instead of swapping in our own delegate object:
    pywebview re-installs its plain shared delegate every time another window
    (e.g. the browser overlay) is created, which would silently drop a
    replacement delegate, while a class-level method survives that. The
    method pywebview already defines (``applicationShouldTerminate:``, used
    for its quit path) is untouched.
    """
    if sys.platform != "darwin":
        return
    try:
        import AppKit
        import objc
        from webview.platforms import cocoa

        def _reopen(self, app, has_visible_windows: bool) -> bool:
            try:
                shared = AppKit.NSApplication.sharedApplication()
                # An app-level Hide (Cmd+H / "Hide Code Wood") unhides
                # automatically on Dock click; just let AppKit do its thing.
                if shared.isHidden():
                    return True
                if not has_visible_windows:
                    window = _MACOS_REOPEN_STATE.get("window")
                    if window is not None:
                        # Safe from any thread: pywebview's Cocoa show()
                        # marshals onto the main run loop internally.
                        window.show()
                    host = _MACOS_REOPEN_STATE.get("host_api")
                    overlay = getattr(host, "_overlay", None)
                    if overlay is not None:
                        # Re-show the browser overlay if the renderer wanted
                        # it visible (same path the minimize/restore cycle
                        # uses; also re-attaches it as a child window).
                        try:
                            overlay.resync()
                        except Exception:
                            pass
            except Exception:
                pass
            # True: the normal reopen handling (activation) should proceed.
            return True

        objc.classAddMethod(
            cocoa.BrowserView.AppDelegate,
            b"applicationShouldHandleReopen:hasVisibleWindows:",
            objc.selector(_reopen, signature=b"B@:@B"),
        )
    except Exception:
        pass


def main() -> int:
    _apply_macos_dock_icon()
    backend = BackendProcess()
    try:
        port, token = backend.start()
    except BackendError as exc:
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
    host_api = HostApi()
    host_api.set_backend(port, token)
    # On macOS the menu lives in the system menu bar (native) and is replaced
    # with the AppKit menu in _install_macos_native_menu on first show.
    if sys.platform == "darwin":
        webview.settings["SHOW_DEFAULT_MENUS"] = False
    window = webview.create_window(
        WINDOW_TITLE,
        url=url,
        width=1140,
        height=780,
        min_size=(960, 640),
        frameless=True,
        easy_drag=False,
        text_select=True,
        js_api=host_api,
    )

    overlay = BrowserOverlay(webview, window, enabled=True)
    host_api.attach_overlay(overlay)

    # Register for the Dock-icon reopen handler *before* anything can hide
    # the window, so a Dock click always brings it back (macOS only).
    if sys.platform == "darwin":
        _MACOS_REOPEN_STATE["window"] = window
        _MACOS_REOPEN_STATE["host_api"] = host_api
        _install_macos_dock_reopen()

    notifier = TaskNotifier(port, token, window) if _task_notify_enabled() else None
    if notifier is not None:
        notifier.start()

    if sys.platform == "win32":

        def _on_shown(*_args: object) -> None:
            _enable_taskbar_minimize_win32(_pywebview_window_hwnd(window))

        try:
            window.events.shown += _on_shown
        except Exception:
            pass
    elif sys.platform == "darwin":

        def _on_shown(*_args: object) -> None:
            try:
                from PyObjCTools import AppHelper

                AppHelper.callAfter(lambda: _install_macos_native_menu(window))
            except Exception:
                _install_macos_native_menu(window)

        try:
            window.events.shown += _on_shown
        except Exception:
            _install_macos_native_menu(window)

    def _on_closing() -> None:
        try:
            overlay.destroy()
        except Exception:
            pass

    def _on_closed() -> None:
        try:
            overlay.destroy()
        except Exception:
            pass
        if notifier is not None:
            try:
                notifier.stop()
            except Exception:
                pass

    window.events.closing += _on_closing
    window.events.closed += _on_closed

    def _resync(*_args: object) -> None:
        try:
            overlay.resync()
        except Exception:
            pass

    def _on_minimized(*_args: object) -> None:
        try:
            overlay.suspend_for_main_minimized()
        except Exception:
            pass

    try:
        window.events.moved += _resync
        window.events.resized += _resync
        window.events.maximized += _resync
        window.events.restored += _resync
        window.events.minimized += _on_minimized
    except Exception:
        pass

    backend_stop = threading.Event()

    def _backend_supervisor() -> None:
        """Watch the backend process and restart it after an unexpected exit.

        Each restart binds a new ephemeral port and mints a new token, so the
        frontend is told about the new endpoint (push + ``backend_info`` poll)
        and rebuilds its API client to reconnect.
        """
        delay = 1.0
        while not backend_stop.is_set():
            proc = backend.proc
            if proc is None:
                return
            rc = proc.poll()
            if rc is None:
                delay = 1.0
                backend_stop.wait(1.0)
                continue
            # Backend exited while the GUI is still open — bring it back.
            print(
                f"[host] backend exited (rc={rc}); restarting...",
                file=sys.stderr,
            )
            try:
                new_port, new_token = backend.restart(timeout=45.0)
            except Exception as exc:
                print(
                    f"[host] backend restart failed: {exc}; "
                    f"retrying in {delay:.0f}s",
                    file=sys.stderr,
                )
                backend_stop.wait(delay)
                delay = min(delay * 2.0, 15.0)
                continue
            delay = 1.0
            host_api.set_backend(new_port, new_token)
            if notifier is not None:
                try:
                    notifier.set_backend(new_port, new_token)
                except Exception:
                    pass
            print(
                f"[host] backend restarted on port {new_port}",
                file=sys.stderr,
            )
            # Push the new endpoint so the frontend reconnects immediately
            # instead of waiting for its next poll.
            try:
                window.evaluate_js(
                    "window.dispatchEvent(new CustomEvent("
                    "'codewood:backend-restarted',"
                    f"{{detail: {{port: {int(new_port)}, "
                    f"token: {json.dumps(new_token)}}}}}))"
                )
            except Exception:
                pass

    threading.Thread(
        target=_backend_supervisor,
        daemon=True,
        name="backend-supervisor",
    ).start()

    debug = str(os.environ.get("CODEWOOD_DEBUG", "")).strip() not in (
        "", "0", "false", "False"
    )
    if debug:
        webview.settings['OPEN_DEVTOOLS_IN_DEBUG'] = False

    try:
        webview.start(gui=_preferred_gui(), debug=debug)
    finally:
        backend_stop.set()
        if notifier is not None:
            try:
                notifier.stop()
            except Exception:
                pass
        backend.stop()
    if sys.platform != "win32":
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
