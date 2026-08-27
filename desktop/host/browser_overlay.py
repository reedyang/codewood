"""In-window embedded browser implemented as a tracked overlay window.

pywebview cannot dock a webview inside another window's DOM region; it only
creates independent OS windows. To give the GUI a "native browser inside the
right panel" we create a second, frameless pywebview window and continuously
position/size it over the right panel's browser viewport, hiding it whenever
the Browser tab is not the thing the user should see (tab inactive, panel
closed, main window minimized, divider being dragged, an in-app overlay open,
etc.).

On Windows the overlay is reparented as a true ``WS_CHILD`` of the main window
(via ``SetParent``), so it is clipped to and moves/minimizes/closes with the
parent, never shows a taskbar button, and is positioned in parent-client
coordinates. Its bottom-right corner is rounded with a custom GDI window region
to tuck into the panel.

Because the overlay is a *real* webview, it can
load external sites that send ``X-Frame-Options``/CSP frame-ancestors, and the
host can read its DOM / run scripts via ``window.evaluate_js`` for the model's
``browser_read_dom`` / ``browser_eval`` tools. Console output is captured by a
small script injected on every load and read back through the same channel.

All public methods are safe to call from the renderer (js_api) thread and from
the main window's event handlers; pywebview marshals webview operations onto the
GUI thread internally. Every webview call is defensively wrapped so a transient
backend hiccup never crashes the host.

Storage isolation note: pywebview exposes ``storage_path`` only as a global
``webview.start()`` argument, not per window, so the overlay cannot be given a
fully separate cookie/localStorage profile through the public API. In practice
the app shell and the browsed content are already separated by origin (the app
shell loads from ``file://`` / ``http://127.0.0.1`` while the browser navigates
to external ``https://`` origins or our own preview pages), which is the
relevant boundary for cookies/localStorage. Achieving a distinct WebView2 user
data folder for the overlay would require dropping to the native WebView2
environment API, which is out of scope here.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any, Dict, Optional

try:  # macOS-only dependencies; absent on other platforms.
    import AppKit  # type: ignore
    import Foundation  # type: ignore
except Exception:  # pragma: no cover - non-macOS
    AppKit = None
    Foundation = None

# Capture console output into a bounded ring buffer on ``window`` so the host
# can read it back with evaluate_js. Injected after every navigation (the page
# replaces the global on load, so re-injection per load is required). Kept tiny
# and dependency-free; it only wraps the four common console levels and never
# throws so it cannot break the page it is injected into.
_CONSOLE_CAPTURE_JS = r"""
(function () {
  try {
    if (window.__codewoodConsole && window.__codewoodConsole.__installed) {
      return;
    }
    var buf = [];
    var MAX = 500;
    function push(level, args) {
      try {
        var parts = [];
        for (var i = 0; i < args.length; i++) {
          var a = args[i];
          if (a && typeof a === "object") {
            try { parts.push(JSON.stringify(a)); }
            catch (e) { parts.push(String(a)); }
          } else {
            parts.push(String(a));
          }
        }
        buf.push({ level: level, text: parts.join(" "), ts: Date.now() });
        if (buf.length > MAX) { buf.splice(0, buf.length - MAX); }
      } catch (e) { /* never break the page */ }
    }
    var store = { __installed: true, buffer: buf };
    var levels = ["log", "info", "warn", "error", "debug"];
    for (var j = 0; j < levels.length; j++) {
      (function (lvl) {
        var orig = console[lvl];
        store["__orig_" + lvl] = orig;
        console[lvl] = function () {
          push(lvl, arguments);
          if (orig) { try { orig.apply(console, arguments); } catch (e) {} }
        };
      })(levels[j]);
    }
    window.addEventListener("error", function (e) {
      push("error", [String((e && e.message) || "error")]);
    });
    window.__codewoodConsole = store;
  } catch (e) { /* swallow */ }
})();
"""


def _native_hwnd(window: Any) -> Optional[int]:
    """Return the Win32 HWND for a pywebview window, or None.

    Available only after the window's ``before_show`` event has fired (when
    ``window.native`` is populated) and only on the Windows/EdgeChromium
    backend. Any failure returns None so callers degrade gracefully.
    """
    try:
        native = getattr(window, "native", None)
        if native is None:
            return None
        handle = native.Handle  # System.Windows.Forms.Form.Handle (IntPtr)
        return int(handle.ToInt64())
    except Exception:
        try:
            return int(native.Handle.ToInt32())  # type: ignore[union-attr]
        except Exception:
            return None


# Radius (device px) of the single rounded bottom-right corner applied via a
# custom window region. Kept small to match the surrounding panel inset.
_BR_CORNER_RADIUS = 0


def _style_overlay_window_win32(overlay_hwnd: int, parent_hwnd: Optional[int]) -> None:
    """Apply Windows-native window styling to the overlay browser window.

    - Reparent the overlay as a true ``WS_CHILD`` of the main window (via
      ``SetParent`` + the ``WS_CHILD`` style). As a child it is clipped to and
      moves/min/closes with the parent and never appears in the taskbar; its
      coordinates become parent-client-relative (handled by the caller).
    - Add ``WS_EX_TOOLWINDOW`` / drop ``WS_EX_APPWINDOW`` as a belt-and-braces
      measure against a separate taskbar button.
    - Disable the Win11 all-corner rounding, then apply a custom window region
      that rounds *only the bottom-right* corner (square elsewhere) so the
      embedded browser tucks into the panel's bottom-right.

    All calls are best-effort; failures are swallowed so a styling hiccup never
    breaks the browser.
    """
    if sys.platform != "win32" or not overlay_hwnd:
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        GWL_STYLE = -16
        GWL_EXSTYLE = -20
        WS_CHILD = 0x40000000
        WS_POPUP = 0x80000000
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_APPWINDOW = 0x00040000
        WS_EX_TOPMOST = 0x00000008

        get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)

        ex = get_long(wintypes.HWND(overlay_hwnd), GWL_EXSTYLE)
        ex = (ex | WS_EX_TOOLWINDOW) & ~(WS_EX_APPWINDOW | WS_EX_TOPMOST)
        set_long(wintypes.HWND(overlay_hwnd), GWL_EXSTYLE, ex)

        if parent_hwnd:
            # Switch from a top-level popup to a child window and reparent it so
            # the main window becomes the actual parent (not merely the owner).
            style = get_long(wintypes.HWND(overlay_hwnd), GWL_STYLE)
            style = (style | WS_CHILD) & ~WS_POPUP
            set_long(wintypes.HWND(overlay_hwnd), GWL_STYLE, style)
            user32.SetParent(
                wintypes.HWND(overlay_hwnd),
                wintypes.HWND(parent_hwnd),
            )
    except Exception:
        pass
    try:
        import ctypes

        # DWMWA_WINDOW_CORNER_PREFERENCE = 33; DWMWCP_DONOTROUND = 1
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_DONOTROUND = 1
        value = ctypes.c_int(DWMWCP_DONOTROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.wintypes.HWND(overlay_hwnd),
            DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except Exception:
        pass


def _round_bottom_right_corner_win32(overlay_hwnd: int, width: int, height: int) -> None:
    """Set a window region that rounds only the bottom-right corner.

    Win11's DWM corner preference rounds all four corners (or none), so to round
    a single corner we install a composite GDI region: the full rectangle with
    the bottom-right square carved out and replaced by a rounded-rect region
    covering twice the radius (whose top-left stays square inside the union).

    The region uses device pixels and must be re-applied whenever the window is
    resized. Best-effort; failures are swallowed.
    """
    if sys.platform != "win32" or not overlay_hwnd:
        return
    w = int(width)
    h = int(height)
    if w <= 0 or h <= 0:
        return
    r = min(_BR_CORNER_RADIUS, w, h)
    try:
        import ctypes
        from ctypes import wintypes

        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32

        if r <= 0:
            user32.SetWindowRgn(wintypes.HWND(overlay_hwnd), None, True)
            return

        RGN_OR = 2

        # Base rectangle excluding a square of side r at the bottom-right.
        base = gdi32.CreateRectRgn(0, 0, w, h - r)
        left = gdi32.CreateRectRgn(0, h - r, w - r, h)
        # Rounded-rect region sized 2r x 2r anchored so its bottom-right rounds;
        # only the bottom-right quadrant lies in the carved corner.
        rounded = gdi32.CreateRoundRectRgn(
            w - 2 * r, h - 2 * r, w, h, 2 * r, 2 * r
        )
        if base and left and rounded:
            gdi32.CombineRgn(base, base, left, RGN_OR)
            gdi32.CombineRgn(base, base, rounded, RGN_OR)
            # SetWindowRgn takes ownership of the region handle on success.
            user32.SetWindowRgn(wintypes.HWND(overlay_hwnd), base, True)
            gdi32.DeleteObject(left)
            gdi32.DeleteObject(rounded)
        else:
            for h_obj in (base, left, rounded):
                if h_obj:
                    gdi32.DeleteObject(h_obj)
    except Exception:
        pass


def _style_overlay_window_macos(overlay_window: Any, main_window: Any) -> None:
    """Configure the overlay for correct mouse-event routing on macOS.

    The overlay is an independent floating window at ``NSFloatingWindowLevel``.
    We install a global ``NSEvent`` monitor for mouse-moved and left-mouse-down
    events that **toggles ``ignoresMouseEvents``** based on the cursor position:

    * Cursor **over the overlay** → ``ignoresMouseEvents = NO`` → browser is
      interactive (clicks, typing, scrolling).
    * Cursor **outside the overlay** → ``ignoresMouseEvents = YES`` → events
      pass through to the main window (resizer works).

    On macOS 11+ we also strip the titled style mask so the overlay has sharp
    corners, and remove the window shadow.

    Safe to call repeatedly.
    """
    if sys.platform != "darwin":
        return
    try:
        import AppKit
    except Exception:
        return
    try:
        overlay_win = getattr(overlay_window, "native", None)
        if overlay_win is None:
            return

        # Apply critical settings + focus-follows-mouse monitor (re-assert
        # after show/hide).  The monitor keeps the correct window key so the
        # browser can receive keyboard input while the resizer (in the main
        # window's web content) still works.
        _apply_critical_macos_styling(overlay_win, main)

        # --- NON-CRITICAL: can run async ---
        def _apply_async() -> None:
            # Remove system rounded corners & prevent user resize.
            _TITLED = 1
            _CLOSABLE = 1 << 1
            _MINIATURIZABLE = 1 << 2
            _RESIZABLE = 1 << 3
            _FULL_SIZE_CV = 1 << 15
            _TEXTURED_BG = 1 << 8
            try:
                mask = overlay_win.styleMask()
                new_mask = (
                    mask
                    & ~_TITLED
                    & ~_CLOSABLE
                    & ~_MINIATURIZABLE
                    & ~_RESIZABLE
                    & ~_FULL_SIZE_CV
                    & ~_TEXTURED_BG
                )
                if new_mask != mask:
                    overlay_win.setStyleMask_(new_mask)
            except Exception:
                pass

            # Remove the window shadow.
            try:
                overlay_win.setHasShadow_(False)
            except Exception:
                pass

            # Force the content view square: pywebview keeps NSTitledWindowMask
            # (rounded) even for "frameless" windows, and the WebView's layer
            # can also round.  Explicitly zero the layer corner radius so the
            # overlay is a clean square rectangle.
            try:
                overlay_win.setStyleMask_(0)
            except Exception:
                pass
            try:
                content = overlay_win.contentView()
                if content is not None:
                    content.setWantsLayer_(True)
                    layer = content.layer()
                    if layer is not None:
                        layer.setCornerRadius_(0.0)
                        layer.setMasksToBounds_(False)
            except Exception:
                pass

        # Non-critical settings can run async.
        try:
            from PyObjCTools.AppHelper import callAfter
            callAfter(_apply_async)
        except Exception:
            _apply_async()
    except Exception:
        pass


# Track which overlay windows already have a focus-follows-mouse monitor so we
# install it only once (the styling function is called repeatedly).
_MONITORED_WINDOW_IDS: "set" = set()

# When the cursor is within this many logical px of the overlay's LEFT edge and
# a left mouse button is pressed, treat it as a right-panel resize gesture (the
# browser's left edge acts as the resize handle).  Kept thin so only the very
# edge grabs; browser content just inside remains clickable.
_RESIZE_EDGE_PX = 16

# Overlay windows already scheduled for a delayed re-style (the WKWebView only
# replaces the placeholder content view after the first page navigation, so the
# chrome strip must run again once the real web view exists).
_RESCHEDULED_IDS: "set" = set()


def _strip_view_chrome(view: Any) -> None:
    """Recursively clear border / corner-radius on a view and all subviews.

    The 1px hairline around the overlay can live on an inner WKWebView layer
    rather than the content view itself, so we walk the whole hierarchy.
    """
    if view is None:
        return
    try:
        view.setWantsLayer_(True)
    except Exception:
        pass
    try:
        layer = view.layer()
        if layer is not None:
            layer.setBorderWidth_(0.0)
            layer.setCornerRadius_(0.0)
            layer.setMasksToBounds_(False)
    except Exception:
        pass
    try:
        subs = view.subviews()
        if subs is not None:
            for sub in subs:
                _strip_view_chrome(sub)
    except Exception:
        pass


def _apply_critical_macos_styling(overlay_win: Any, main: Any = None) -> None:
    """Apply CRITICAL macOS styling + focus-follows-mouse monitor.

    Runs synchronously on the main thread.  Allows the overlay to become key
    (so the browser can receive keyboard input) but prevents it from becoming
    the *main* window and from participating in window cycling.  A local event
    monitor routes key status by cursor position (see _install_focus_follows_mouse).
    """
    if sys.platform != "darwin":
        return
    try:
        import AppKit
    except Exception:
        return
    if overlay_win is None:
        return

    def _apply_critical() -> None:
        # Allow the overlay to become key so the embedded browser can receive
        # keyboard input.  (Key status is actively managed by the focus-
        # follows-mouse monitor installed below.)
        try:
            overlay_win.setCanBecomeKey_(True)
        except Exception:
            pass
        # The overlay must never become the *main* window (that would steal the
        # app's menu bar / main-window semantics from the real main window).
        try:
            overlay_win.setCanBecomeMainWindow_(False)
        except Exception:
            pass

        # Prevent the overlay from participating in window cycling (Cmd+`).
        try:
            _NSWindowCollectionBehaviorIgnoresCycle = 1 << 7
            _NSWindowCollectionBehaviorManaged = 1 << 1
            current = overlay_win.collectionBehavior()
            new = current | _NSWindowCollectionBehaviorIgnoresCycle | _NSWindowCollectionBehaviorManaged
            if new != current:
                overlay_win.setCollectionBehavior_(new)
        except Exception:
            pass

        # Always interactive: mouse events go to window under cursor.
        try:
            overlay_win.setIgnoresMouseEvents_(False)
        except Exception:
            pass

        # Keep the overlay attached to and above the MAIN window, but NOT above
        # other apps.  A previous "status-level + 1" approach made it a global
        # always-on-top window, so clicking another app's window landed *between*
        # the main and browser windows.  Making it a child window of the main
        # window (at the normal level) keeps it above the main window, tracks
        # the main window's movement, and lets other apps rise above both when
        # they're activated.
        try:
            overlay_win.setLevel_(AppKit.NSNormalWindowLevel)
        except Exception:
            pass
        try:
            main_native = getattr(main, "native", None)
            if main_native is not None and hasattr(main_native, "addChildWindow_ordered_"):
                # NSWindowAbove places the child above the parent: the browser
                # stays above the main window, tracks its movement, and yields
                # to other apps.  macOS DETACHES child windows when the parent is
                # miniaturized, so after a minimize/restore cycle the browser is
                # no longer a child and drops behind the main window.  Re-attach
                # whenever it isn't currently a child (detected by windowNumber,
                # which is stable across the PyObjC wrapper boundary).
                try:
                    overlay_num = overlay_win.windowNumber()
                    child_nums = []
                    try:
                        for c in (main_native.childWindows() or []):
                            child_nums.append(c.windowNumber())
                    except Exception:
                        child_nums = []
                    is_child = overlay_num in child_nums
                except Exception:
                    is_child = False
                if not is_child:
                    main_native.addChildWindow_ordered_(overlay_win, AppKit.NSWindowAbove)
        except Exception:
            pass

        # Transparent window background so the overlay blends with the main
        # window behind it (no white corner artifact) and reads as part of the
        # main window rather than a separate floating widget.
        try:
            overlay_win.setBackgroundColor_(AppKit.NSColor.clearColor())
        except Exception:
            pass

        # CRITICAL: drop ALL default window chrome.  pywebview keeps
        # NSTitledWindowMask on "frameless" windows, which (a) rounds the
        # corners and (b) makes the window draggable by macOS.  The latter is
        # what caused "auto-bounce": grabbing the browser edge let macOS drag
        # the window while the resize logic (frontend) repositioned it via
        # _sync, so the two fought and the window snapped back.  A pure
        # borderless mask (0) is non-draggable and square.  Must run BEFORE
        # show so the change takes visual effect.
        try:
            overlay_win.setStyleMask_(0)
        except Exception:
            pass
        # No shadow / non-opaque so there is no 1px hairline around the window.
        try:
            overlay_win.setHasShadow_(False)
        except Exception:
            pass
        try:
            overlay_win.setOpaque_(False)
        except Exception:
            pass
        try:
            content = overlay_win.contentView()
            if content is not None:
                # Fill the window exactly so no 1px theme-frame inset (which
                # reads as a thin border around the overlay) remains.
                try:
                    superview = content.superview()
                    if superview is not None:
                        # Fill the parent exactly (setFrame_ is in the
                        # superview's coordinate space, NOT window space).
                        content.setFrame_(superview.bounds())
                    else:
                        content.setFrame_(overlay_win.contentLayoutRect())
                except Exception:
                    pass
                try:
                    content.setAutoresizingMask_(
                        AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
                    )
                except Exception:
                    pass
                # Strip any border/corner radius on the WKWebView and every
                # layer beneath it (the hairline can live on an inner
                # WKWebView subview, not the content view itself).
                _strip_view_chrome(content)
        except Exception:
            pass

        # Install the focus-follows-mouse monitor (runs on main thread here).
        if main is not None:
            try:
                _install_focus_follows_mouse(overlay_win, main)
            except Exception:
                pass

        # The WKWebView only replaces the placeholder content view AFTER the
        # first page navigation, so re-apply the chrome strip a few times (on
        # the main thread) to catch the real web view once it exists.
        wid = id(overlay_win)
        if wid not in _RESCHEDULED_IDS:
            _RESCHEDULED_IDS.add(wid)
            try:
                from PyObjCTools.AppHelper import callAfter

                for delay in (0.4, 1.2, 2.5):
                    threading.Timer(
                        delay,
                        lambda: callAfter(
                            lambda: _apply_critical_macos_styling(overlay_win, main)
                        ),
                    ).start()
            except Exception:
                pass


    # Run synchronously on main thread.
    try:
        if AppKit.NSThread.isMainThread():
            _apply_critical()
        else:
            import threading
            done = threading.Event()
            def _block():
                try:
                    _apply_critical()
                finally:
                    done.set()
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_block)
            done.wait()
    except Exception:
        # Fallback: best-effort direct call (may crash if off main thread)
        _apply_critical()


def _install_focus_follows_mouse(overlay_win: Any, main: Any) -> None:
    wid = id(overlay_win)
    if wid in _MONITORED_WINDOW_IDS:
        return
    import AppKit

    def _handler(event: Any) -> Any:
        try:
            mouse = AppKit.NSEvent.mouseLocation()
            frame = overlay_win.frame()
            inside = (
                frame.origin.x <= mouse.x <= frame.origin.x + frame.size.width
                and frame.origin.y <= mouse.y <= frame.origin.y + frame.size.height
            )
            buttons = AppKit.NSEvent.pressedMouseButtons()
            # A left mouse-down within the resize zone on the overlay's left
            # edge starts a right-panel resize (the browser's left edge is the
            # resize handle).  We forward the gesture to the frontend, which owns
            # the actual resize logic.
            if (
                event is not None
                and event.type() == AppKit.NSLeftMouseDown
                and 0 <= (mouse.x - frame.origin.x) <= _RESIZE_EDGE_PX
            ):
                main_native = getattr(main, "native", None)
                if main_native is not None:
                    client_x = int(mouse.x - main_native.frame().origin.x)

                    def _begin_resize(cx: int) -> None:
                        try:
                            # Run on a background thread: pywebview's
                            # evaluate_js schedules the JS on the main run loop
                            # and then blocks a semaphore for the result.  If we
                            # called it from the monitor (main) thread, the
                            # run loop can't service the inner eval while we're
                            # blocked -> deadlock.  A background thread lets the
                            # main run loop process the eval and release us.
                            main.evaluate_js(
                                "window.__codewoodBeginResizeRight && "
                                "window.__codewoodBeginResizeRight(%d)" % cx
                            )
                        except Exception:
                            pass

                    threading.Thread(
                        target=_begin_resize, args=(client_x,), daemon=True
                    ).start()
            if buttons & (1 << 0):
                # Mid-drag (e.g. dragging the resizer).  Pass mouse events
                # through to the main window so the drag keeps working even
                # when the cursor crosses over the overlay, and never change
                # key status mid-drag.
                if not overlay_win.ignoresMouseEvents():
                    overlay_win.setIgnoresMouseEvents_(True)
                return event
            if inside:
                # Cursor over the overlay: make the browser interactive and key
                # so it receives mouse + keyboard input.
                if overlay_win.ignoresMouseEvents():
                    overlay_win.setIgnoresMouseEvents_(False)
                if not overlay_win.isKeyWindow():
                    overlay_win.makeKeyWindow()
            else:
                # Cursor over the main window: pass events through and keep the
                # main window key so its resizer / inputs work.
                if not overlay_win.ignoresMouseEvents():
                    overlay_win.setIgnoresMouseEvents_(True)
                main_native = getattr(main, "native", None)
                if main_native is not None and not main_native.isKeyWindow():
                    main_native.makeKeyWindow()
        except Exception:
            pass
        return event

    _mask = (
        AppKit.NSMouseMovedMask
        | AppKit.NSLeftMouseDownMask
        | AppKit.NSLeftMouseUpMask
        | AppKit.NSLeftMouseDraggedMask
        | AppKit.NSRightMouseDownMask
        | AppKit.NSRightMouseUpMask
        | AppKit.NSRightMouseDraggedMask
        | AppKit.NSScrollWheelMask
    )
    AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(_mask, _handler)
    _MONITORED_WINDOW_IDS.add(wid)


def _macos_window_visible(window: Any) -> bool:
    """Return True if the macOS window's native NSWindow is visible."""
    if sys.platform != "darwin":
        return True
    ns_win = getattr(window, "native", None)
    if ns_win is None:
        return False
    return bool(ns_win.isVisible())


def _read_console_js() -> str:
    """JS expression returning the captured console buffer as a JSON string."""
    return (
        "(function(){try{var s=window.__codewoodConsole;"
        "if(!s||!s.buffer){return '[]';}"
        "return JSON.stringify(s.buffer);}catch(e){return '[]';}})()"
    )


def _read_dom_js() -> str:
    """JS expression returning the full serialized document HTML."""
    return (
        "(function(){try{return document.documentElement"
        "?document.documentElement.outerHTML:'';}catch(e){return '';}})()"
    )


class BrowserOverlay:
    """Owns the overlay browser window and its geometry/visibility state.

    The overlay window object is created lazily on first use.
    """

    def __init__(self, webview_module: Any, main_window: Any, *, enabled: bool) -> None:
        self._webview = webview_module
        self._main = main_window
        self._enabled = bool(enabled)
        self._overlay: Any = None
        self._lock = threading.RLock()
        # Last requested viewport rect (logical px, relative to the main
        # window's client area) and whether the renderer wants it visible.
        self._rect: Optional[Dict[str, int]] = None
        self._want_visible = False
        # Whether the OS window is currently shown. Tracked so we avoid
        # redundant show/hide churn (which can steal focus on some backends).
        self._shown = False
        self._current_url = ""
        self._styled = False
        # True once the overlay has been reparented as a WS_CHILD of the main
        # window. In child mode ``move`` is parent-client-relative, so geometry
        # sync must skip the screen-origin offset.
        self._is_child = False
        # Last device-pixel size the corner region was built for, to avoid
        # rebuilding the GDI region on every redundant sync.
        self._corner_size: Optional[tuple] = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    # -- lifecycle ---------------------------------------------------------

    def ensure_window(self) -> Optional[Any]:
        """Create the hidden overlay window on first use; return it (or None)."""
        if not self._enabled:
            return None
        with self._lock:
            if self._overlay is not None:
                return self._overlay
            try:
                self._overlay = self._webview.create_window(
                    "Code Wood Browser",
                    url="about:blank",
                    frameless=True,
                    easy_drag=False,
                    hidden=True,
                    on_top=True,
                    min_size=(120, 120),
                    width=480,
                    height=360,
                )
                # Re-inject the console capture after every navigation so reads
                # work regardless of which page is loaded.
                try:
                    self._overlay.events.loaded += self._on_overlay_loaded
                except Exception:
                    pass
            except Exception:
                self._overlay = None
            # Apply native styling (WS_CHILD reparent + clear WS_EX_TOPMOST)
            # AFTER create_window returns so that pywebview's internal Show/Hide
            # cycle (opacity=0 → Show → Hide → opacity=1 for hidden=True) has
            # completed.  Applying SetParent + WS_CHILD *before* that cycle —
            # inside a ``before_show`` handler — is unsafe because WinForms
            # ``Form.Show()`` on a reparented WS_CHILD window can silently reset
            # the window styles, undoing the reparent and leaving the overlay as
            # a top-level WS_POPUP with TopMost still active.  Call styling here
            # instead, after all WinForms initialization settles.
            self._apply_native_styling()
            # Explicitly clear the pywebview/.NET ``TopMost`` flag so that the
            # form never re-asserts WS_EX_TOPMOST behind our back (the extended
            # style bit is cleared inside ``_style_overlay_window_win32``, but
            # the .NET ``Form.TopMost`` property getter returns the cached value
            # and some WinForms operations may re-apply it on Show/Activate).
            try:
                self._overlay.on_top = False
            except Exception:
                pass
            return self._overlay

    def _apply_native_styling(self) -> None:
        ov = self._overlay
        if ov is None:
            return
        if sys.platform == "darwin":
            _style_overlay_window_macos(ov, self._main)
            self._styled = True
            return
        if sys.platform != "win32":
            return
        overlay_hwnd = _native_hwnd(ov)
        parent_hwnd = _native_hwnd(self._main)
        if overlay_hwnd is not None:
            _style_overlay_window_win32(overlay_hwnd, parent_hwnd)
            self._styled = True
            if parent_hwnd:
                self._is_child = True

    def _on_overlay_loaded(self) -> None:
        ov = self._overlay
        if ov is None:
            return
        try:
            ov.run_js(_CONSOLE_CAPTURE_JS)
        except Exception:
            pass
        try:
            url = ov.get_current_url()
            if isinstance(url, str) and url:
                self._current_url = url
        except Exception:
            pass

    def destroy(self) -> None:
        with self._lock:
            ov = self._overlay
            self._overlay = None
            self._shown = False
            self._styled = False
            self._is_child = False
        if ov is not None:
            try:
                ov.destroy()
            except Exception:
                pass

    # -- geometry / visibility --------------------------------------------

    def set_bounds(self, x: int, y: int, width: int, height: int) -> bool:
        """Record the target rect (client-relative, logical px) and re-sync."""
        if not self._enabled:
            return False
        try:
            rect = {
                "x": int(round(x)),
                "y": int(round(y)),
                "w": max(1, int(round(width))),
                "h": max(1, int(round(height))),
            }
        except (TypeError, ValueError):
            return False
        with self._lock:
            self._rect = rect
        self._sync()
        return True

    def show(self) -> bool:
        if not self._enabled:
            return False
        with self._lock:
            self._want_visible = True
        self.ensure_window()
        self._sync()
        return True

    def hide(self) -> bool:
        if not self._enabled:
            return False
        with self._lock:
            self._want_visible = False
        self._sync()
        return True

    def _main_origin(self) -> Optional[tuple]:
        """Return the main window's client-area origin in screen coords.

        The main window is frameless, so the web layer fills the whole window
        and client (0,0) coincides with the window's top-left.
        """
        main = self._main
        if main is None:
            return None
        try:
            return (int(main.x), int(main.y))
        except Exception:
            return None

    def _sync(self) -> None:
        """Apply the desired geometry/visibility to the overlay window."""
        if not self._enabled:
            return
        with self._lock:
            rect = dict(self._rect) if self._rect else None
            want = self._want_visible
            shown = self._shown
        ov = self._overlay
        if not want or rect is None:
            if ov is not None and shown:
                try:
                    ov.hide()
                except Exception:
                    pass
                with self._lock:
                    self._shown = False
            return
        ov = self.ensure_window()
        if ov is None:
            return
        with self._lock:
            is_child = self._is_child
        if is_child:
            # Child windows are positioned relative to the parent client area,
            # which the renderer's rect already is — no screen-origin offset.
            target_x, target_y = rect["x"], rect["y"]
        else:
            origin = self._main_origin()
            if origin is None:
                return
            target_x = origin[0] + rect["x"]
            target_y = origin[1] + rect["y"]
        try:
            ov.resize(rect["w"], rect["h"])
        except Exception:
            pass
        try:
            ov.move(target_x, target_y)
        except Exception:
            pass
        self._apply_corner_region(rect["w"], rect["h"])
        if not shown:
            # Apply CRITICAL macOS styling + focus-follows-mouse monitor BEFORE
            # showing the window.  This allows the overlay to become key (so the
            # browser can receive keyboard) while the monitor routes key status
            # by cursor position so the resizer keeps working.
            if sys.platform == "darwin":
                native = getattr(ov, "native", None)
                if native is not None:
                    _apply_critical_macos_styling(native, self._main)
            try:
                ov.show()
            except Exception:
                pass
            # pywebview's show() calls makeKeyAndOrderFront_ (via callAfter),
            # which forcibly makes the overlay the key window.  Restore key
            # status to the main window so its resizer works initially; the
            # focus-follows-mouse monitor then keeps it correct as the cursor
            # moves.  We use callAfter (same FIFO queue pywebview uses) so this
            # runs AFTER pywebview's makeKeyAndOrderFront.
            if sys.platform == "darwin":
                main_native = getattr(self._main, "native", None)
                if main_native is not None:
                    def _make_main_key() -> None:
                        try:
                            main_native.makeKeyWindow()
                        except Exception:
                            pass
                    try:
                        from PyObjCTools.AppHelper import callAfter
                        callAfter(_make_main_key)
                    except Exception:
                        _make_main_key()
            with self._lock:
                self._shown = True
            # Re-assert native styling after the window becomes visible so
            # that any attributes reset by the WinForms ``Show()`` call
            # (notably the Win11 DwmSetWindowAttribute corner preference and
            # the WS_EX_TOPMOST extended style) are re-applied immediately.
            self._apply_native_styling()

    def _apply_corner_region(self, logical_w: int, logical_h: int) -> None:
        """Round only the overlay's bottom-right corner, in device pixels.

        Uses ``GetClientRect`` to read the true device-pixel size (correct under
        any DPI scaling) and rebuilds the GDI region only when that size
        changes. Best-effort and Windows-only.
        """
        if sys.platform != "win32":
            return
        ov = self._overlay
        if ov is None:
            return
        overlay_hwnd = _native_hwnd(ov)
        if not overlay_hwnd:
            return
        try:
            import ctypes
            from ctypes import wintypes

            rect = wintypes.RECT()
            if not ctypes.windll.user32.GetClientRect(
                wintypes.HWND(overlay_hwnd), ctypes.byref(rect)
            ):
                return
            dev_w = int(rect.right - rect.left)
            dev_h = int(rect.bottom - rect.top)
        except Exception:
            dev_w, dev_h = int(logical_w), int(logical_h)
        if dev_w <= 0 or dev_h <= 0:
            return
        with self._lock:
            if self._corner_size == (dev_w, dev_h):
                return
            self._corner_size = (dev_w, dev_h)
        _round_bottom_right_corner_win32(overlay_hwnd, dev_w, dev_h)

    def resync(self) -> None:
        """Public hook for main-window move/resize/restore events."""
        self._sync()

    def suspend_for_main_minimized(self) -> None:
        """Hide while the main window is minimized (keep the want-visible intent)."""
        if not self._enabled:
            return
        ov = self._overlay
        if ov is not None:
            try:
                ov.hide()
            except Exception:
                pass
            with self._lock:
                self._shown = False

    def set_passthrough(self, enabled: bool) -> bool:
        """No-op kept for API compatibility.

        Mouse-event routing is now handled by a global NSEvent monitor
        installed in ``_style_overlay_window_macos`` that toggles
        ``ignoresMouseEvents`` based on cursor position.
        """
        return self._enabled and sys.platform == "darwin"

    # -- navigation / reads ------------------------------------------------

    def navigate(self, url: str) -> Dict[str, Any]:
        if not self._enabled:
            return {"success": False, "error": "overlay disabled"}
        target = str(url or "").strip()
        if not target:
            return {"success": False, "error": "missing url"}
        ov = self.ensure_window()
        if ov is None:
            return {"success": False, "error": "overlay unavailable"}
        try:
            ov.load_url(target)
            self._current_url = target
            return {"success": True, "url": target}
        except Exception as exc:  # pragma: no cover - backend-specific
            return {"success": False, "error": f"navigate failed: {exc}"}

    def load_preview(self, html: str) -> Dict[str, Any]:
        if not self._enabled:
            return {"success": False, "error": "overlay disabled"}
        ov = self.ensure_window()
        if ov is None:
            return {"success": False, "error": "overlay unavailable"}
        try:
            ov.load_html(str(html or ""))
            self._current_url = "preview"
            return {"success": True, "url": "preview"}
        except Exception as exc:  # pragma: no cover
            return {"success": False, "error": f"load failed: {exc}"}

    def refresh(self) -> Dict[str, Any]:
        if not self._enabled:
            return {"success": False, "error": "overlay disabled"}
        ov = self._overlay
        if ov is None:
            return {"success": False, "error": "overlay unavailable"}
        try:
            ov.evaluate_js("location.reload()")
            return {"success": True, "url": self._current_url}
        except Exception as exc:  # pragma: no cover
            return {"success": False, "error": f"refresh failed: {exc}"}

    def get_url(self) -> Dict[str, Any]:
        if not self._enabled:
            return {"success": False, "error": "overlay disabled"}
        ov = self._overlay
        if ov is None:
            return {"success": True, "url": ""}
        try:
            url = ov.get_current_url()
            return {"success": True, "url": url if isinstance(url, str) else ""}
        except Exception:
            return {"success": True, "url": self._current_url}

    def read_dom(self) -> Dict[str, Any]:
        if not self._enabled:
            return {"success": False, "error": "overlay disabled"}
        ov = self._overlay
        if ov is None:
            return {"success": False, "error": "no page loaded"}
        try:
            dom = ov.evaluate_js(_read_dom_js())
            return {"success": True, "dom": dom if isinstance(dom, str) else ""}
        except Exception as exc:  # pragma: no cover
            return {"success": False, "error": f"read_dom failed: {exc}"}

    def read_console(self) -> Dict[str, Any]:
        if not self._enabled:
            return {"success": False, "error": "overlay disabled"}
        ov = self._overlay
        if ov is None:
            return {"success": False, "error": "no page loaded"}
        try:
            raw = ov.evaluate_js(_read_console_js())
            entries: Any = []
            if isinstance(raw, str):
                try:
                    entries = json.loads(raw)
                except Exception:
                    entries = []
            elif isinstance(raw, list):
                entries = raw
            return {"success": True, "console": entries}
        except Exception as exc:  # pragma: no cover
            return {"success": False, "error": f"read_console failed: {exc}"}

    def evaluate(self, script: str) -> Dict[str, Any]:
        if not self._enabled:
            return {"success": False, "error": "overlay disabled"}
        ov = self._overlay
        if ov is None:
            return {"success": False, "error": "no page loaded"}
        code = str(script or "")
        if not code.strip():
            return {"success": False, "error": "empty script"}
        try:
            result = ov.evaluate_js(code)
            return {"success": True, "result": result}
        except Exception as exc:  # pragma: no cover
            return {"success": False, "error": f"eval failed: {exc}"}

    def close_page(self) -> Dict[str, Any]:
        """Blank the page and hide the overlay (the window object is kept)."""
        if not self._enabled:
            return {"success": False, "error": "overlay disabled"}
        ov = self._overlay
        if ov is not None:
            try:
                ov.load_url("about:blank")
            except Exception:
                pass
        self._current_url = ""
        self.hide()
        return {"success": True}

    # -- command dispatch --------------------------------------------------

    def run_command(self, action: str, url: str = "", script: str = "") -> Dict[str, Any]:
        """Execute a model-issued browser command and return its result.

        Mirrors the action vocabulary used by the frontend ``BrowserPanel``
        so the backend tool protocol is unchanged.
        """
        act = str(action or "")
        if act == "open":
            return self.navigate(url)
        if act == "open_preview":
            # ``url`` is a backend chat-file URL serving bridged HTML; the
            # overlay loads it directly as a real page (same isolation as any
            # navigation), so a simple navigate is sufficient and lets the
            # console/DOM readers work on it too.
            return self.navigate(url)
        if act == "close":
            return self.close_page()
        if act == "refresh":
            return self.refresh()
        if act == "get_url":
            return self.get_url()
        if act == "read_dom":
            return self.read_dom()
        if act == "read_console":
            return self.read_console()
        if act == "eval":
            return self.evaluate(script)
        return {"success": False, "error": f"unknown action: {act}"}
