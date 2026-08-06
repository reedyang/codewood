"""Native "task finished" notifications for the Code Wood desktop GUI.

The GUI host subscribes to the backend's SSE event stream in a background
thread. When a genuine task finishes (the backend publishes a
``task_finished`` event) while the Code Wood window is NOT visible to the
user (minimized, or covered by another window), a native OS notification is
raised in the bottom-right corner of the primary screen. The notification
names the chat the task ran in.

Standard per-platform notification mechanisms are used:
- Windows: Windows 10/11 toast via PowerShell + WinRT, registered under the
  ``CodeWood.Desktop`` AppUserModelID so the notification center labels it
  "Code Wood" with the Code Wood icon (raw balloons label themselves after
  the launching process, e.g. "Windows PowerShell"). Falls back to a
  ``Shell_NotifyIcon`` balloon when toasts are unavailable.
- macOS: ``osascript`` ``display notification`` (NSUserNotification).
- Linux: ``notify-send`` (libnotify).

Set ``CODEWOOD_TASK_NOTIFY=0`` to disable the feature entirely.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode


def _load_app_info_module():
    """Load ``cli/config/app_info.py`` by file path, or None.

    Loaded by file path instead of ``import cli.config.app_info`` so the GUI
    host never pulls in the heavy ``cli`` package (whose ``__init__`` imports
    the full ``Agent``) just to read branding constants.
    """
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", "") or ".")
        candidates = [
            meipass / "cli" / "config" / "app_info.py",
            Path(__file__).resolve().parents[2] / "cli" / "config" / "app_info.py",
        ]
    else:
        candidates = [
            Path(__file__).resolve().parents[2] / "cli" / "config" / "app_info.py",
        ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                "codewood_host_app_info", str(path)
            )
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        except Exception:
            continue
    return None


def _app_name() -> str:
    """Effective app display name from ``cli/config/app_info.py``.

    ``get_app_name`` honors the ``CODEWOOD_PROMPT_APP_NAME`` override, so
    notifications follow whatever name the rest of the app surfaces. Returns
    ``""`` only when the bundled ``app_info.py`` cannot be resolved.
    """
    module = _load_app_info_module()
    if module is None:
        return ""
    getter = getattr(module, "get_app_name", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "").strip()
    except Exception:
        return ""


def _app_real_slug() -> str:
    """Stable slug of the real (non-overridden) app name, e.g. ``codewood``.

    Used for internal identifiers (the activation URL scheme) that must stay
    stable even when ``CODEWOOD_PROMPT_APP_NAME`` renames the display name.
    """
    module = _load_app_info_module()
    if module is None:
        return "codewood"
    getter = getattr(module, "get_app_real_slug_compact", None)
    if not callable(getter):
        return "codewood"
    try:
        slug = str(getter() or "").strip()
        return slug or "codewood"
    except Exception:
        return "codewood"

# How long a completed task may keep a notification from being raised again.
# Guards against a reconnect replay / duplicate delivery of the same event.
_COOLDOWN_SECONDS = 1.5


def _window_hwnd(window) -> int | None:
    """Extract the Win32 HWND from a pywebview window, or None."""
    if window is None:
        return None
    try:
        native = getattr(window, "native", None)
        if native is None:
            return None
        handle = native.Handle
        return int(handle.ToInt64() if hasattr(handle, "ToInt64") else handle)
    except Exception:
        return None


def _win32_window_visible(hwnd: int | None) -> bool:
    """True when the Code Wood window is actually on screen in front of the user.

    A window is "visible" only when it is shown, not minimized, and is the
    foreground window. If another app covers it (or it is minimized) the user
    is not watching it, so a completion notification is warranted.
    """
    if not hwnd:
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    is_visible = user32.IsWindowVisible
    is_visible.argtypes = [wintypes.HWND]
    is_visible.restype = wintypes.BOOL

    is_iconic = user32.IsIconic
    is_iconic.argtypes = [wintypes.HWND]
    is_iconic.restype = wintypes.BOOL

    get_foreground = user32.GetForegroundWindow
    get_foreground.restype = wintypes.HWND

    if not is_visible(wintypes.HWND(hwnd)):
        return False
    if is_iconic(wintypes.HWND(hwnd)):
        return False
    return int(get_foreground()) == int(hwnd)


def _darwin_window_visible(window) -> bool:
    """True when the Code Wood window is the active key window on macOS."""
    if bool(getattr(window, "minimized", False)):
        return False
    try:
        import AppKit  # pyobjc ships with pywebview's Cocoa backend

        shared = AppKit.NSApplication.sharedApplication()
        if not shared.isActive():
            return False
        native = getattr(window, "native", None)
        key = shared.keyWindow()
        if native is not None and key is not None:
            return key is native
        return True
    except Exception:
        return False


def _gtk_window_visible(window) -> bool:
    """True when the Code Wood window is active (not covered) under GTK/WSL."""
    if bool(getattr(window, "minimized", False)):
        return False
    gtk_window = getattr(window, "gtk", None) or getattr(window, "native", None)
    if gtk_window is not None:
        try:
            return bool(gtk_window.is_active())
        except Exception:
            pass
    # Cannot determine: be conservative and treat the window as visible so we
    # never spam a user who may be looking at it.
    return True


def _windows_powershell_path() -> str:
    """Absolute path to Windows PowerShell 5.1 (ships with every Windows)."""
    default = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )
    if os.path.isfile(default):
        return default
    return shutil.which("powershell") or "powershell"


# AppUserModelID under which Code Wood's toasts are registered. The identity
# is what Windows uses to render the toast header ("Code Wood") and icon, so a
# raw balloon/notify-icon (which labels itself after the launching process,
# e.g. "Windows PowerShell") can never show the right app name.
_WINDOWS_AUMID = "CodeWood.Desktop"
_WINDOWS_AUMID_REGISTERED = False

# Custom URL scheme that toast clicks activate. Clicking a notification runs
# the registered ``shell\open\command`` (codewood with ``--toast-activate``),
# which restores + foregrounds the already-running GUI window. Using a URL
# protocol avoids needing an AppUserModelID shortcut/COM activator, which
# unpackaged apps would otherwise have to register for click activation.
_WINDOWS_ACTIVATION_PROTOCOL = f"{_app_real_slug()}-activate"


def _windows_activate_command() -> str:
    """Command Windows runs when a task-completion notification is clicked.

    ``%1`` receives the activation URL; the launched instance ignores it and
    just foregrounds the running GUI window (see ``cli/main.py``).

    The command targets a *windowed* entry point so clicking the toast never
    flashes a console window:
    - frozen: the windowed ``codewood-gui.exe`` launcher, which then spawns
      ``codewood --toast-activate`` with ``CREATE_NO_WINDOW``;
    - dev: ``pythonw.exe`` (no console), falling back to ``python.exe``.
    """
    exe_dir = Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False):
        launcher = exe_dir / "codewood-gui.exe"
        exe = str(launcher) if launcher.is_file() else str(Path(sys.executable).resolve())
        return f'"{exe}" --toast-activate %1'
    pythonw = exe_dir / "pythonw.exe"
    exe = str(pythonw) if pythonw.is_file() else str(Path(sys.executable).resolve())
    main_py = Path(__file__).resolve().parents[2] / "cli" / "main.py"
    return f'"{exe}" "{main_py}" --toast-activate %1'


def _windows_register_activation_protocol() -> None:
    """Register the toast-click URL protocol under HKCU (no elevation)."""
    import winreg

    protocol = _WINDOWS_ACTIVATION_PROTOCOL
    command = _windows_activate_command()
    base = rf"Software\Classes\{protocol}"
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, base, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"URL:{protocol}")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKeyEx(
            key, "shell\\open\\command", 0, winreg.KEY_SET_VALUE
        ) as command_key:
            winreg.SetValueEx(command_key, "", 0, winreg.REG_SZ, command)


def _windows_app_icon_path() -> str:
    """Absolute path to the Code Wood icon used for toast registration.

    Packaged builds carry the icon bundled at ``_MEIPASS/codewood_assets/``
    (see ``build/codewood.spec``); development runs use ``build/app_icon.ico``.
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", "."))
        candidate = base / "codewood_assets" / "app_icon.ico"
        if candidate.is_file():
            return str(candidate)
        exe_dir = Path(sys.executable).parent
        for candidate in (exe_dir / "app_icon.ico", exe_dir.parent / "build" / "app_icon.ico"):
            if candidate.is_file():
                return str(candidate)
        return ""
    repo_root = Path(__file__).resolve().parents[2]
    candidate = repo_root / "build" / "app_icon.ico"
    return str(candidate) if candidate.is_file() else ""


def _windows_register_aumid() -> bool:
    """Register the toast AppUserModelID in the current user's registry.

    Writes ``HKCU\\Software\\Classes\\AppUserModelId\\CodeWood.Desktop`` with the
    display name ("Code Wood") and icon path. This is the documented way for
    unpackaged desktop apps to give toasts their own name + icon. Cheap and
    idempotent, so it is repeated on every launch (also re-points the icon
    after the bundle is moved). Uses the stdlib ``winreg`` — no PowerShell.
    """
    global _WINDOWS_AUMID_REGISTERED
    if _WINDOWS_AUMID_REGISTERED:
        return True
    icon_uri = _windows_app_icon_path()
    try:
        import winreg

        key_path = r"Software\Classes\AppUserModelId\CodeWood.Desktop"
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        ) as key:
            display_name = _app_name()
            if display_name:
                winreg.SetValueEx(
                    key, "DisplayName", 0, winreg.REG_SZ, display_name
                )
            if icon_uri:
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, icon_uri)
        # Toast clicks (see _windows_toast) activate this URL scheme, which
        # restores + foregrounds the running GUI window.
        _windows_register_activation_protocol()
        _WINDOWS_AUMID_REGISTERED = True
        return True
    except Exception:
        # Do not cache failures: a transient error should not disable toasts
        # for the rest of the session.
        return False


def _xml_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _windows_toast(title: str, text: str) -> bool:
    """Show a Windows 10/11 toast via PowerShell + WinRT.

    The toast is created under ``_WINDOWS_AUMID`` so the notification center
    labels it "Code Wood" with the Code Wood icon (registered in
    ``_windows_register_aumid``) instead of "Windows PowerShell". The script
    is passed via ``-EncodedCommand`` (UTF-16LE base64) so Unicode chat names
    survive verbatim; a single-quoted PowerShell literal carries the XML
    (``'`` doubled) so arbitrary user text can never break out of the script.
    Returns ``True`` when the toast was handed to the shell.
    """
    xml = (
        f'<toast activationType="protocol" '
        f'launch="{_WINDOWS_ACTIVATION_PROTOCOL}:activate">'
        "<visual><binding template=\"ToastGeneric\">"
        f"<text>{_xml_escape(title)}</text>"
        f"<text>{_xml_escape(text)}</text>"
        "</binding></visual>"
        "</toast>"
    )
    xml_literal = xml.replace("'", "''")
    script = (
        "try {\n"
        "[Windows.UI.Notifications.ToastNotificationManager, "
        "Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, "
        "Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null\n"
        f"$xmls = '{xml_literal}'\n"
        "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        "$doc.LoadXml($xmls)\n"
        "$toast = New-Object Windows.UI.Notifications.ToastNotification $doc\n"
        "$n = [Windows.UI.Notifications.ToastNotificationManager]::"
        f"CreateToastNotifier('{_WINDOWS_AUMID}')\n"
        "$n.Show($toast)\n"
        "exit 0\n"
        "} catch { exit 1 }\n"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [
                _windows_powershell_path(),
                "-NoProfile",
                "-STA",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            timeout=15,
            creationflags=creationflags,
        )
        return bool(proc and proc.returncode == 0)
    except Exception:
        return False


def _windows_notify(title: str, text: str) -> None:
    """Show the task-completion notification with the Code Wood identity.

    Primary path is a Windows 10/11 toast (registered AppUserModelID shows the
    "Code Wood" header and app icon). Falls back to a classic tray balloon
    when toasts are unavailable (e.g. PowerShell missing).
    """
    if _windows_register_aumid() and _windows_toast(title, text):
        return
    _windows_shell_notify(title, text)


def _windows_shell_notify(title: str, text: str, hwnd: int | None = None) -> None:
    """Best-effort ``Shell_NotifyIcon`` balloon (ctypes, no PowerShell).

    Kept as a fallback for environments without a usable PowerShell. Note that
    on some Windows 10/11 setups ``NIM_ADD`` fails with ``E_FAIL`` and no
    balloon is displayed — the PowerShell path above is the reliable one.
    """
    import ctypes
    from ctypes import wintypes

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", ctypes.c_wchar * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", ctypes.c_wchar * 256),
            ("uTimeout", wintypes.UINT),
            ("szInfoTitle", ctypes.c_wchar * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HICON),
        ]

    shell32 = ctypes.windll.shell32
    user32 = ctypes.windll.user32

    shell32.Shell_NotifyIconW.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(NOTIFYICONDATAW),
    ]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    user32.GetDesktopWindow.restype = wintypes.HWND

    data = NOTIFYICONDATAW()
    data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    data.hWnd = wintypes.HWND(int(hwnd)) if hwnd else user32.GetDesktopWindow()
    data.uID = 0x6345
    data.uFlags = 0x00000010  # NIF_INFO: balloon with title/text
    data.dwInfoFlags = 0x01  # NIIF_INFO: standard information icon
    data.uTimeout = 10000
    data.szInfoTitle = str(title or "")[:63]
    data.szInfo = str(text or "")[:255]

    # NIM_ADD creates the (hidden) icon, NIM_MODIFY fires the balloon, and
    # NIM_DELETE removes the icon again so no tray residue is left behind.
    # A short pause lets the shell register the balloon before the icon goes.
    shell32.Shell_NotifyIconW(0x00000001, ctypes.byref(data))
    shell32.Shell_NotifyIconW(0x00000002, ctypes.byref(data))
    time.sleep(0.25)
    shell32.Shell_NotifyIconW(0x00000003, ctypes.byref(data))


def _macos_notify(title: str, text: str) -> None:
    """Show a Notification Center notification via ``osascript``."""

    def _escape(value: str) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    subprocess.run(
        [
            "osascript",
            "-e",
            f'display notification "{_escape(text)}" '
            f'with title "{_escape(title)}"',
        ],
        capture_output=True,
        timeout=10,
    )


def _linux_notify(title: str, text: str) -> None:
    """Show a notification via ``notify-send`` (libnotify)."""
    if not shutil.which("notify-send"):
        return
    command = [
        "notify-send",
        "--expire-time",
        "10000",
        str(title)[:256],
        str(text)[:512],
    ]
    app_name = _app_name()
    if app_name:
        command[1:1] = ["--app-name", app_name]
    subprocess.run(
        command,
        capture_output=True,
        timeout=10,
    )


def _show_native_notification(chat_name: str, elapsed_seconds: int) -> None:
    """Raise the platform-native completion notification for *chat_name*."""
    # Collapse any line breaks in the chat name so a multiline title cannot
    # break the notification payload on any backend.
    title = str(chat_name or "Chat").replace("\r", " ").replace("\n", " ")[:64]
    body = "Task finished"
    try:
        if int(elapsed_seconds) > 0:
            body = f"{body} · {int(elapsed_seconds)}s"
    except (TypeError, ValueError):
        pass
    try:
        if sys.platform == "win32":
            _windows_notify(title, body)
        elif sys.platform == "darwin":
            _macos_notify(title, body)
        else:
            _linux_notify(title, body)
    except Exception:
        # A notification failure must never break the app.
        pass


class TaskNotifier:
    """Subscribes to backend SSE and notifies when a task finishes unseen."""

    def __init__(self, port: int, token: str, window=None) -> None:
        self._port = int(port)
        self._token = str(token or "")
        self._window = window
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_notify_at = 0.0
        self._lock = threading.Lock()

    def set_backend(self, port: int, token: str) -> None:
        """Re-target a restarted backend (new ephemeral port + token)."""
        with self._lock:
            self._port = int(port)
            self._token = str(token or "")

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="task-notifier"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._consume()
            except Exception:
                # Backend not reachable yet / restarted / stream dropped:
                # reconnect after a short pause (it also keeps the loop from
                # busy-spinning on a dead connection).
                pass
            if self._stop.wait(2.0):
                break

    def _consume(self) -> None:
        with self._lock:
            port, token = self._port, self._token
        if not port or not token:
            return
        # A hand-rolled SSE client over a raw socket instead of
        # ``urllib.request.urlopen``: the buffered ``http.client`` reader
        # crashes the frozen GUI host when the bundle lives under the user
        # profile (``C:\\Users\\<user>\\...``) on some Windows 10/11 builds,
        # while a plain socket reads the same keep-alive stream reliably.
        import socket

        path = f"/events?{urlencode({'token': token})}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Accept: text/event-stream\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        sock = socket.create_connection(("127.0.0.1", port), timeout=20)
        try:
            sock.settimeout(20)
            sock.sendall(request.encode("utf-8"))
            buf = b""
            # Read the response head so we only consume SSE after a 200.
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    return
                buf += chunk
            head, buf = buf.split(b"\r\n\r\n", 1)
            status_line = head.split(b"\r\n", 1)[0]
            status_parts = status_line.split(b" ", 2)
            if len(status_parts) < 2 or status_parts[1] != b"200":
                return
            while not self._stop.is_set():
                # Drain complete lines already buffered before the next recv.
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    line = raw_line.rstrip(b"\r").decode("utf-8", "replace")
                    if not line.startswith("data: "):
                        continue
                    try:
                        message = json.loads(line[len("data: "):])
                    except Exception:
                        continue
                    if not isinstance(message, dict):
                        continue
                    if message.get("event") != "task_finished":
                        continue
                    data = message.get("data")
                    if isinstance(data, dict):
                        self._handle_task_finished(data)
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                buf += chunk
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _window_visible(self) -> bool:
        """True when the Code Wood window is on screen and in front."""
        window = self._window
        if window is None:
            return False
        try:
            if sys.platform == "win32":
                return _win32_window_visible(_window_hwnd(window))
            if sys.platform == "darwin":
                return _darwin_window_visible(window)
            return _gtk_window_visible(window)
        except Exception:
            # Could not determine visibility: err on the side of notifying the
            # user that their task finished.
            return False

    def _handle_task_finished(self, data: dict) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_notify_at < _COOLDOWN_SECONDS:
                return
        chat_name = str(data.get("chatName") or "").strip()
        if not chat_name:
            chat_name = str(data.get("chatId") or "").strip()
        if not chat_name:
            return
        if self._window_visible():
            return
        with self._lock:
            self._last_notify_at = now
        try:
            elapsed = int(data.get("elapsedSeconds") or 0)
        except (TypeError, ValueError):
            elapsed = 0
        _show_native_notification(chat_name, elapsed)
