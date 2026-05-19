import os
import os
import sys
from typing import Any, Optional


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


def _ansi_white(text: str) -> str:
    if not _stdout_color_enabled():
        return text
    if sys.platform == "win32":
        _enable_windows_console_vt()
    return f"\033[37m{text}\033[0m"


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
