import os
import sys
import threading
import time
from typing import Any, List, Optional, Tuple


def _decode_subprocess_output(data: Optional[bytes]) -> str:
    """
    Decode shell stdout/stderr: prefer UTF-8, else system locale.
    Fixes mojibake when a UTF-8 child is decoded as cp936 on Chinese Windows.
    """
    if not data:
        return ""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    for dec in ("utf-8", "utf-8-sig"):
        try:
            return data.decode(dec, errors="strict")
        except UnicodeDecodeError:
            continue
    import locale

    enc = locale.getpreferredencoding(False) or "utf-8"
    try:
        return data.decode(enc, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _safe_console_write(text: str, stream: Any = None, append_newline: bool = True) -> None:
    """
    Write text to console safely on Windows terminals with legacy encodings (e.g. GBK).
    Falls back to replacement encoding instead of raising UnicodeEncodeError.
    """
    if text is None:
        return
    s = stream or sys.stdout
    try:
        s.write(text)
        if append_newline and not text.endswith("\n"):
            s.write("\n")
        s.flush()
        return
    except UnicodeEncodeError:
        pass

    enc = getattr(s, "encoding", None) or "utf-8"
    payload = text if (text.endswith("\n") or not append_newline) else (text + "\n")
    try:
        if hasattr(s, "buffer"):
            s.buffer.write(payload.encode(enc, errors="replace"))
            s.flush()
        else:
            s.write(payload.encode(enc, errors="replace").decode(enc, errors="replace"))
            s.flush()
    except Exception:
        # Last-resort fallback; avoid crashing the agent on terminal encoding issues.
        try:
            print(payload.encode("ascii", errors="replace").decode("ascii"), end="")
        except Exception:
            pass


def _enable_windows_console_vt() -> None:
    """Enable ANSI escape sequences on Windows 10+ console when stdout is a TTY."""
    if sys.platform != "win32":
        return
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        h = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            kernel32.SetConsoleMode(h, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        pass


def _stdout_color_enabled() -> bool:
    """
    Whether ANSI colors should be emitted. Unix/macOS terminals typically support SGR
    sequences on a TTY; Windows needs VT processing (see _enable_windows_console_vt).
    Honors NO_COLOR (https://no-color.org/), TERM=dumb, and optional FORCE_COLOR.
    """
    # NO_COLOR: any presence disables color (spec: regardless of value)
    if "NO_COLOR" in os.environ:
        return False
    force = (os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE") or "").strip().lower()
    if force in ("1", "true", "yes", "always"):
        return True
    if os.environ.get("TERM", "") == "dumb":
        return False
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    return True


def _ansi_red(text: str) -> str:
    if not _stdout_color_enabled():
        return text
    if sys.platform == "win32":
        _enable_windows_console_vt()
    return f"\033[31m{text}\033[0m"


def _ansi_yellow(text: str) -> str:
    if not _stdout_color_enabled():
        return text
    if sys.platform == "win32":
        _enable_windows_console_vt()
    return f"\033[33m{text}\033[0m"


def _ansi_gray(text: str) -> str:
    if not _stdout_color_enabled():
        return text
    if sys.platform == "win32":
        _enable_windows_console_vt()
    return f"\033[90m{text}\033[0m"


def _ansi_blue(text: str) -> str:
    if not _stdout_color_enabled():
        return text
    if sys.platform == "win32":
        _enable_windows_console_vt()
    return f"\033[34m{text}\033[0m"

def _ansi_bright_blue(text: str) -> str:
    if not _stdout_color_enabled():
        return text
    if sys.platform == "win32":
        _enable_windows_console_vt()
    return f"\033[94m{text}\033[0m"

def _ansi_cyan(text: str) -> str:
    if not _stdout_color_enabled():
        return text
    if sys.platform == "win32":
        _enable_windows_console_vt()
    return f"\033[36m{text}\033[0m"

def _ansi_green(text: str) -> str:
    if not _stdout_color_enabled():
        return text
    if sys.platform == "win32":
        _enable_windows_console_vt()
    # Use a softer syntax-green that matches the CLI screenshot style better than the default bright green.
    return f"\033[38;2;152;195;121m{text}\033[0m"


def _ansi_bold(text: str) -> str:
    if not _stdout_color_enabled():
        return text
    if sys.platform == "win32":
        _enable_windows_console_vt()
    return f"\033[1m{text}\033[0m"


def _ansi_rgb(text: str, r: int, g: int, b: int) -> str:
    if not _stdout_color_enabled():
        return text
    if sys.platform == "win32":
        _enable_windows_console_vt()
    rr = max(0, min(255, int(r)))
    gg = max(0, min(255, int(g)))
    bb = max(0, min(255, int(b)))
    return f"\033[38;2;{rr};{gg};{bb}m{text}\033[0m"


def _format_elapsed_minutes_seconds(elapsed_seconds: int) -> str:
    total = max(0, int(elapsed_seconds or 0))
    minutes, seconds = divmod(total, 60)
    return f"{minutes}m {seconds}s"


def _build_marquee_windows(text_length: int, max_window: int = 3) -> List[Tuple[int, int]]:
    length = max(0, int(text_length or 0))
    if length <= 0:
        return [(0, 0)]
    window = min(max(1, length // 3), max(1, int(max_window or 1)), length)
    windows: List[Tuple[int, int]] = []
    # Head: grow from a single highlighted character.
    for size in range(1, window + 1):
        windows.append((0, size))
    # Body: fixed-width window moving right.
    for start in range(1, max(1, length - window + 1)):
        windows.append((start, window))
    # Tail: shrink while exiting at the far right.
    for size in range(window - 1, 0, -1):
        windows.append((length - size, size))
    return windows or [(0, 0)]


def _gray_segment(text: str) -> str:
    s = str(text or "")
    if not s:
        return ""
    return _ansi_gray(s)


def _render_working_status_line(
    elapsed_seconds: int,
    frame: int,
    label: str = "Working...",
    interrupt_hint: str = "esc to interrupt",
) -> str:
    work_label = str(label or "Working...")
    elapsed = _format_elapsed_minutes_seconds(elapsed_seconds)
    plain = f"• {work_label} ({elapsed} • {interrupt_hint})"
    if not _stdout_color_enabled():
        return plain

    label_offset = plain.find(work_label)
    if label_offset < 0 or not work_label:
        return _ansi_gray(plain)
    windows = _build_marquee_windows(len(work_label), max_window=3)
    start, size = windows[int(frame or 0) % len(windows)]
    if size <= 0:
        return _ansi_gray(plain)
    seg_start = label_offset + start
    seg_end = seg_start + size
    return (
        _gray_segment(plain[:seg_start])
        + plain[seg_start:seg_end]
        + _gray_segment(plain[seg_end:])
    )


class _WorkingStatusTicker:
    def __init__(
        self,
        stream: Any,
        fps: float = 6.0,
        min_interval_seconds: float = 0.02,
    ) -> None:
        self._stream = stream
        safe_fps = float(fps or 0.0)
        if safe_fps <= 0:
            safe_fps = 1.0
        safe_min_interval = max(0.001, float(min_interval_seconds or 0.02))
        self._interval_seconds = max(safe_min_interval, 1.0 / safe_fps)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._printed_plain_line = False
        self._start_ts = 0.0

    def _is_tty(self) -> bool:
        try:
            return bool(hasattr(self._stream, "isatty") and self._stream.isatty())
        except Exception:
            return False

    def _render_frame(self, elapsed_seconds: int, frame: int) -> None:
        line = _render_working_status_line(elapsed_seconds=elapsed_seconds, frame=frame)
        try:
            self._stream.write(f"\r\x1b[2K{line}")
            self._stream.flush()
        except Exception:
            pass

    def _run(self) -> None:
        frame = 1
        while not self._stop_event.wait(timeout=self._interval_seconds):
            elapsed = int(time.monotonic() - self._start_ts)
            self._render_frame(elapsed_seconds=elapsed, frame=frame)
            frame += 1

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._start_ts = time.monotonic()
        if not self._is_tty():
            try:
                self._stream.write(_render_working_status_line(elapsed_seconds=0, frame=0))
                self._stream.write("\n")
                self._stream.flush()
                self._printed_plain_line = True
            except Exception:
                pass
            return
        self._render_frame(elapsed_seconds=0, frame=0)
        self._thread = threading.Thread(
            target=self._run,
            name="smartshell-working-status",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if not self._started:
            return
        self._stop_event.set()
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=self._interval_seconds + 0.2)
        if self._is_tty():
            try:
                self._stream.write("\r\x1b[2K")
                self._stream.flush()
            except Exception:
                pass
        elif self._printed_plain_line:
            try:
                self._stream.flush()
            except Exception:
                pass
