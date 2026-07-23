"""Tool: shell.

This module hosts the full shell execution pipeline (formerly
cli/actions/command_actions.py) plus the ShellTool class.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import os
import re
import secrets
import shlex
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..actions.command_execution_buffer import CommandExecutionBuffer
from ..config.app_info import get_app_runtime_attr_name
from ..core.console_utils import (
    GUI_CMD_OUTPUT_END,
    _SpinnerTicker,
    _WorkingStatusTicker,
    _ansi_gray,
    _safe_console_write,
)
from ..core.console_title import restore_app_console_title
from ..core.localization import translate


def _t(agent: Any, key: str, fallback: Optional[str] = None, **kwargs: Any) -> str:
    from ..core.localization import get_display_language, translate

    return translate(key, get_display_language(agent), fallback=fallback, **kwargs)

SHELL_OUTPUT_DISPLAY_TAIL_LINES = 30
SHELL_OUTPUT_DISPLAY_RESERVED_LINES = 3
SHELL_WORKING_STATUS_MARQUEE_FPS = 10.0
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ANSI_OSC_RE = re.compile(r"\x1b\][^\a\x1b]*(?:\a|\x1b\\)")
# Strips ConPTY-injected CSI sequences (window ops, DA, private modes)
# while preserving SGR color/style codes (which end with 'm'), cursor
# movement (A/B/C/D → \b/space), cursor position (H → \b via vcol),
# horizontal-absolute (G → \r), and erase-line (K → \r).
_PTY_CSI_STRIP_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@E-GI-JL-lno-~]")
_CURSOR_LEFT_RE = re.compile(r"\x1b\[([0-9]*)D")
_CURSOR_RIGHT_RE = re.compile(r"\x1b\[([0-9]*)C")
_CURSOR_UP_RE = re.compile(r"\x1b\[([0-9]*)A")
_CURSOR_DOWN_RE = re.compile(r"\x1b\[([0-9]*)B")
_CUP_RE = re.compile(r"\x1b\[(\d+);(\d+)H")
_CHA_RE = re.compile(r"\x1b\[(\d*)G")
_EL_RE = re.compile(r"\x1b\[\d*K")
_STREAM_ATTR_TERMINAL_COLUMNS = get_app_runtime_attr_name("terminal_columns")
_STREAM_ATTR_OUTPUT_INDENT_WIDTH = get_app_runtime_attr_name("output_indent_width")

def _collapse_cr_output(text: str) -> str:
    """Collapse \\r-based line overwrites and \\b-based backspaces
    (spinners, progress bars, timeout countdowns) in captured output.

    Each line overwritten by consecutive \\r is reduced to just the last
    segment. Consecutive \\b overwrites are collapsed by processing
    backspace character-by-character.
    CRLF (\\r\\n) is preserved as LF.
    """
    text = text.replace("\r\n", "\n")
    # Handle carriage-return: keep only the last \\r-separated segment per line.
    lines = text.split("\n")
    out = []
    for line in lines:
        if "\r" in line:
            parts = line.split("\r")
            non_empty = [p for p in parts if p]
            # Multiple \\r frames on one line → spinner / progress bar
            # whose last frame was never finalized (cursor moved to next
            # line with \\n before the last frame was overwritten).
            # Clear the entire line.
            if len(non_empty) >= 2:
                out.append("")
                continue
            line = line.rsplit("\r", 1)[-1]
        if "\b" in line:
            line = _handle_backspace_collapse(line)
        out.append(line)
    return "\n".join(out)


def _handle_backspace_collapse(text: str) -> str:
    """Process \\b (backspace) overwrite semantics on a single line."""
    if "\b" not in text:
        return text
    cur = []
    col = 0
    for ch in text:
        if ch == "\b":
            if col > 0:
                col -= 1
        else:
            if col < len(cur):
                cur[col] = ch
            else:
                cur.append(ch)
            col += 1
    return "".join(cur)


# On Windows, try to use winpty (ConPTY) so child processes like
# timeout.exe see a real console handle instead of a redirected pipe.
# Tests can set this to None to disable.
import subprocess as _subprocess_mod
_ORIG_SUBPROCESS_POPEN = _subprocess_mod.Popen
_WINPTY_PTYPROCESS = None
if sys.platform == "win32":
    try:
        from winpty import PtyProcess as _WINPTY_PTYPROCESS
    except ImportError:
        pass

if _WINPTY_PTYPROCESS is not None:

    class _WinPtyReader:
        """Wrap winpty read() as a byte-stream pipe for _stream_and_capture."""
        def __init__(self, pty_proc, activity_tracker=None):
            self._pty = pty_proc
            self._activity_tracker = activity_tracker
            self._buf = ""
            self._virtual_col = 0
            self._pending_cha = None
        def read(self, n=1024):
            try:
                while True:
                    if self._buf:
                        data = self._buf
                        self._buf = ""
                    else:
                        data = self._pty.read(4096)
                        if not data:
                            return b""
                    if self._activity_tracker is not None:
                        try:
                            self._activity_tracker()
                        except Exception:
                            pass
                    # Strip ConPTY-injected sequences (window ops, DA responses,
                    # private mode sets) but preserve SGR color/style codes.
                    stripped = _PTY_CSI_STRIP_RE.sub("", data)
                    stripped = ANSI_OSC_RE.sub("", stripped)
                    # Convert CUP (Cursor Position) to backspace using a
                    # virtual-column tracker so timeout.exe-style countdowns
                    # that jump to row 2, col N translate to the right number
                    # of \\b to overwrite the target digit.
                    def _handle_cup(m):
                        row = int(m.group(1) or 1)
                        col = int(m.group(2) or 1)
                        target = max(0, col - 1)
                        cur = self._virtual_col
                        if row <= 2 and cur > target:
                            return "\b" * (cur - target)
                        if row <= 2 and cur < target:
                            return " " * (target - cur)
                        return ""
                    stripped = _CUP_RE.sub(_handle_cup, stripped)
                    # Convert cursor-movement CSI / CHA / EL.
                    stripped = _CURSOR_LEFT_RE.sub(
                        lambda m: "\b" * int(m.group(1) or 1), stripped,
                    )
                    stripped = _CURSOR_RIGHT_RE.sub(
                        lambda m: " " * int(m.group(1) or 1), stripped,
                    )
                    stripped = _CURSOR_UP_RE.sub("\r", stripped)
                    stripped = _CURSOR_DOWN_RE.sub("\n", stripped)
                    stripped = _CHA_RE.sub(
                        lambda m: "\r" if int(m.group(1) or 1) <= 1 else "", stripped,
                    )
                    stripped = _EL_RE.sub("\r", stripped)
                    # Advance virtual column for visible characters so that
                    # subsequent CUP conversions compute the correct offset.
                    for ch in stripped:
                        if ch == "\r":
                            self._virtual_col = 0
                        elif ch == "\n":
                            self._virtual_col = 0
                        elif ch == "\b":
                            if self._virtual_col > 0:
                                self._virtual_col -= 1
                        elif ch != "\x1b":
                            self._virtual_col += 1
                    if not stripped:
                        continue
                    # If the previous chunk was a single visible character, it
                    # may be the target of an upcoming CHA+EL erase (e.g. the
                    # trailing \\ that ConPTY emits before clearing a table
                    # row). Buffer it until the next read arrives.
                    visible = stripped.replace("\r", "").replace("\n", "").replace("\b", "")
                    if visible == stripped and len(stripped) == 1 and stripped not in "\r\n\b":
                        if self._pending_cha is None:
                            self._pending_cha = stripped
                            continue
                    # If the current chunk is only \\r characters (CHA/EL
                    # conversion), and we have a buffered single-char, the
                    # character was being erased — flush only the \\r tail.
                    if visible == "" and stripped.strip("\r") == "":
                        if self._pending_cha is not None:
                            self._pending_cha = None
                        continue
                    # Emit: prepend any non-erased buffered character.
                    if self._pending_cha is not None:
                        stripped = self._pending_cha + stripped
                        self._pending_cha = None
                    return stripped.encode("utf-8", errors="replace")
            except EOFError:
                return b""
        def read1(self, n=1024):
            return self.read(n)
        def close(self):
            pass

    class _WinPtyWriter:
        """Wrap winpty write() to accept bytes (like subprocess.PIPE)."""
        def __init__(self, pty_proc):
            self._pty = pty_proc
        def write(self, data: bytes):
            self._pty.write(data.decode("utf-8", errors="replace"))
        def flush(self):
            pass
        def close(self):
            pass

    class _WinPtyProc:
        """Mimic subprocess.Popen interface backed by a winpty ConPTY process."""
        _IDLE_EOF_TIMEOUT = 3.0

        def __init__(self, pty_proc):
            self._pty = pty_proc
            self.pid = pty_proc.pid
            self.returncode = None
            self._last_activity = time.time()
            self.stdout = _WinPtyReader(pty_proc, activity_tracker=self._track_activity)
            self.stdin = None
        def _track_activity(self):
            self._last_activity = time.time()
        def _maybe_send_eof(self):
            if hasattr(self._pty, "sendeof"):
                try:
                    self._pty.sendeof()
                except Exception:
                    pass
        def wait(self, timeout=None):
            import subprocess as _sp
            if timeout is None:
                while self._pty.isalive():
                    time.sleep(0.1)
                    if time.time() - self._last_activity > self._IDLE_EOF_TIMEOUT:
                        self._maybe_send_eof()
            else:
                deadline = time.time() + timeout
                while self._pty.isalive() and time.time() < deadline:
                    time.sleep(0.05)
                    if time.time() - self._last_activity > self._IDLE_EOF_TIMEOUT:
                        self._maybe_send_eof()
                if self._pty.isalive():
                    raise _sp.TimeoutExpired(cmd=self.pid, timeout=timeout)
            self.returncode = self._pty.exitstatus or 0
            return self.returncode
        def poll(self):
            if not self._pty.isalive():
                self.returncode = self._pty.exitstatus or 0
            return self.returncode
        def kill(self):
            self._pty.kill()
        def terminate(self):
            self._pty.terminate(force=True)


def _resolve_shell_execution_cwd(agent: Any) -> Path:
    resolver = getattr(agent, "_shell_execution_cwd", None)
    if callable(resolver):
        try:
            resolved = resolver()
            if isinstance(resolved, Path):
                return resolved
            if resolved:
                return Path(str(resolved))
        except Exception:
            pass
    raw_root = getattr(agent, "workspace_root", None)
    if raw_root:
        try:
            root = Path(str(raw_root)).expanduser().resolve()
            if root.exists() and root.is_dir():
                return root
        except Exception:
            pass
    return Path(getattr(agent, "work_directory", Path.cwd()))


def _reset_work_directory_to_startup_initial(agent: Any) -> None:
    """Best-effort restore of agent work directory to startup initial directory after shell execution."""
    try:
        resetter = getattr(agent, "_reset_work_directory_to_startup_initial", None)
        if callable(resetter):
            resetter()
    except Exception:
        pass


def _count_output_lines(text: str) -> int:
    raw = str(text or "")
    if not raw:
        return 0
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    parts = normalized.split("\n")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return len(parts)


def _strip_console_color_controls(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    no_osc = ANSI_OSC_RE.sub("", raw)
    return ANSI_ESCAPE_RE.sub("", no_osc)


def _gray_shell_display_text(text: str) -> str:
    plain = _strip_console_color_controls(text)
    if not plain:
        return ""
    trailing_newline = plain.endswith("\n")
    core = plain[:-1] if trailing_newline else plain
    if not core:
        return "\n" if trailing_newline else ""
    colored = _ansi_gray(core)
    return colored + ("\n" if trailing_newline else "")


def _clear_streamed_output_window(
    stream: Any,
    rendered_lines: int,
    cursor_at_line_start: bool,
) -> None:
    lines = max(0, int(rendered_lines or 0))
    if lines <= 0:
        return
    try:
        stream.write("\r")
        if bool(cursor_at_line_start):
            stream.write("\x1b[1A")
        for idx in range(lines):
            stream.write("\r\x1b[2K")
            if idx < (lines - 1):
                stream.write("\x1b[1A")
        stream.write("\r")
        stream.flush()
    except Exception:
        pass


def _format_omitted_lines_notice(omitted_lines: int, stream: Any, language: Any = None) -> str:
    from ..core.localization import DEFAULT_DISPLAY_LANGUAGE, normalize_display_language, translate

    lang = normalize_display_language(language) or DEFAULT_DISPLAY_LANGUAGE
    msg = translate("output.omitted_lines", lang, count=int(omitted_lines))
    try:
        if hasattr(stream, "isatty") and stream.isatty():
            return f"\x1b[90;3m{msg}\x1b[0m\n"
    except Exception:
        pass
    return msg + "\n"


def _terminal_columns_for_tail_display(stream: Any) -> int:
    try:
        fn = getattr(stream, _STREAM_ATTR_TERMINAL_COLUMNS, None)
        if callable(fn):
            cols = int(fn() or 0)
            if cols > 0:
                return cols
    except Exception:
        pass
    try:
        if stream is not None and hasattr(stream, "fileno"):
            cols = int(os.get_terminal_size(stream.fileno()).columns or 0)
            if cols > 0:
                return cols
    except Exception:
        pass
    try:
        cols = int(shutil.get_terminal_size(fallback=(80, 24)).columns or 80)
        if cols > 0:
            return cols
    except Exception:
        pass
    return 80


def _terminal_rows_for_tail_display(stream: Any) -> int:
    try:
        if stream is not None and hasattr(stream, "fileno"):
            rows = int(os.get_terminal_size(stream.fileno()).lines or 0)
            if rows > 0:
                return rows
    except Exception:
        pass
    try:
        rows = int(shutil.get_terminal_size(fallback=(80, 24)).lines or 24)
        if rows > 0:
            return rows
    except Exception:
        pass
    return 24


def _dynamic_tail_line_limit(
    stream: Any,
    max_tail_lines: int = SHELL_OUTPUT_DISPLAY_TAIL_LINES,
    reserved_lines: int = SHELL_OUTPUT_DISPLAY_RESERVED_LINES,
) -> int:
    rows = _terminal_rows_for_tail_display(stream)
    safe_rows = max(1, int(rows) - max(0, int(reserved_lines or 0)))
    return min(safe_rows, max(1, int(max_tail_lines or 1)))


def _wrap_line_for_display(line: str, width: int) -> List[str]:
    clean = ANSI_ESCAPE_RE.sub("", str(line or "")).expandtabs(4)
    if not clean:
        return [""]
    w = max(1, int(width or 1))
    chunks: List[str] = []
    current: List[str] = []
    current_w = 0
    for ch in clean:
        if unicodedata.combining(ch):
            ch_w = 0
        else:
            cat = unicodedata.category(ch)
            if cat in ("Cc", "Cf"):
                ch_w = 0
            else:
                ch_w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if current and (current_w + ch_w > w):
            chunks.append("".join(current))
            current = [ch]
            current_w = ch_w
        else:
            current.append(ch)
            current_w += ch_w
    if current:
        chunks.append("".join(current))
    return chunks or [""]


def _visual_rows_for_display_text(text: str, width: int) -> int:
    raw = str(text or "")
    if not raw:
        return 0
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return sum(len(_wrap_line_for_display(line, width)) for line in lines)


def _build_tail_output_for_display(
    text: str,
    stream: Any,
    tail_lines: int = SHELL_OUTPUT_DISPLAY_TAIL_LINES,
    display_indent_width: int = 0,
    allow_partial_start_line: bool = True,
    language: Any = None,
) -> str:
    from ..core.localization import DEFAULT_DISPLAY_LANGUAGE, normalize_display_language, text as _text

    raw = str(text or "")
    if not raw:
        return ""
    lang = normalize_display_language(language) or DEFAULT_DISPLAY_LANGUAGE
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    trailing_newline = normalized.endswith("\n")
    lines = normalized.split("\n")
    if trailing_newline and lines and lines[-1] == "":
        lines = lines[:-1]
    try:
        stream_indent = int(getattr(stream, _STREAM_ATTR_OUTPUT_INDENT_WIDTH, 0) or 0)
    except Exception:
        stream_indent = 0
    try:
        extra_indent = int(display_indent_width or 0)
    except Exception:
        extra_indent = 0
    content_width = max(1, _terminal_columns_for_tail_display(stream) - max(0, stream_indent + extra_indent))
    limit = max(1, int(tail_lines or 1))
    line_chunks = [_wrap_line_for_display(line, content_width) for line in lines]
    total_visual_rows = sum(len(chunks) for chunks in line_chunks)
    if total_visual_rows <= limit:
        return raw

    notice_text = f"... omitted {len(lines)} lines ..."
    notice_rows = max(1, _visual_rows_for_display_text(notice_text, content_width))
    remaining_rows = max(0, limit - notice_rows)
    selected_lines: List[str] = []
    selected_start = len(lines)
    partial_start_line = False
    used_rows = 0
    for idx in range(len(lines) - 1, -1, -1):
        chunks = line_chunks[idx]
        chunk_count = len(chunks)
        if used_rows + chunk_count <= remaining_rows:
            selected_lines.insert(0, lines[idx])
            selected_start = idx
            used_rows += chunk_count
            continue
        available = remaining_rows - used_rows
        if available > 0 and bool(allow_partial_start_line):
            selected_lines.insert(0, "\n".join(chunks[-available:]))
            selected_start = idx
            partial_start_line = True
        break

    if not selected_lines and lines:
        selected_lines.insert(0, lines[-1])
        selected_start = len(lines) - 1
        partial_start_line = False

    omitted = selected_start
    if partial_start_line:
        omitted += 1
    if not selected_lines:
        omitted = len(lines)
    tail = "\n".join(selected_lines)
    if tail and trailing_newline:
        tail += "\n"
    return _format_omitted_lines_notice(omitted, stream, language=language) + tail


def _select_logical_tail_output_for_live_replay(
    text: str,
    stream: Any,
    tail_lines: int,
    display_indent_width: int = 0,
) -> Tuple[str, int]:
    raw = str(text or "")
    if not raw:
        return "", 0
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    trailing_newline = normalized.endswith("\n")
    lines = normalized.split("\n")
    if trailing_newline and lines and lines[-1] == "":
        lines = lines[:-1]
    if not trailing_newline and lines:
        # During live replay the last logical line may still be arriving from
        # the subprocess. Do not use it to seed a resized window.
        lines = lines[:-1]
    if not lines:
        return "", 0
    limit = max(1, int(tail_lines or 1))
    try:
        stream_indent = int(getattr(stream, _STREAM_ATTR_OUTPUT_INDENT_WIDTH, 0) or 0)
    except Exception:
        stream_indent = 0
    try:
        extra_indent = int(display_indent_width or 0)
    except Exception:
        extra_indent = 0
    content_width = max(
        1,
        _terminal_columns_for_tail_display(stream) - max(0, stream_indent + extra_indent),
    )
    line_chunks = [_wrap_line_for_display(line, content_width) for line in lines]
    total_visual_rows = sum(len(chunks) for chunks in line_chunks)
    if total_visual_rows <= limit:
        return "\n".join(lines) + "\n", 0

    notice_text = f"... omitted {len(lines)} lines ..."
    notice_rows = max(1, _visual_rows_for_display_text(notice_text, content_width))
    remaining_rows = max(0, limit - notice_rows)
    selected: List[str] = []
    selected_start = len(lines)
    used_rows = 0
    for idx in range(len(lines) - 1, -1, -1):
        chunk_count = len(line_chunks[idx])
        if used_rows + chunk_count > remaining_rows:
            break
        selected.insert(0, lines[idx])
        selected_start = idx
        used_rows += chunk_count

    omitted = max(0, selected_start)
    if not selected:
        omitted = len(lines)
    tail = "\n".join(selected) + ("\n" if selected else "")
    return tail, omitted


def _build_logical_tail_output_for_live_replay(
    text: str,
    stream: Any,
    tail_lines: int,
    display_indent_width: int = 0,
    language: Any = None,
) -> str:
    tail, omitted = _select_logical_tail_output_for_live_replay(
        text,
        stream,
        tail_lines,
        display_indent_width=display_indent_width,
    )
    if omitted <= 0:
        return tail
    from ..core.localization import DEFAULT_DISPLAY_LANGUAGE, normalize_display_language, translate as _translate

    lang = normalize_display_language(language) or DEFAULT_DISPLAY_LANGUAGE
    return f"{_translate('output.omitted_lines', lang, count=omitted)}\n{tail}"


def _append_completed_output_lines(
    text: str,
    completed_lines: List[str],
    pending_state: Dict[str, str],
) -> None:
    chunk = str(text or "")
    if not chunk:
        return
    normalized = chunk.replace("\r\n", "\n").replace("\r", "\n")
    pending = str(pending_state.get("text", "")) + normalized
    parts = pending.split("\n")
    if pending.endswith("\n"):
        completed_lines.extend(parts[:-1])
        pending_state["text"] = ""
    else:
        completed_lines.extend(parts[:-1])
        pending_state["text"] = parts[-1] if parts else ""


def _enforce_windows_powershell_command_prefix(command: str) -> Dict[str, Any]:
    if os.name != "nt":
        return {"ok": True, "command": command}
    cmd = str(command or "").strip()
    if not cmd:
        return {"ok": True, "command": command}
    # Models occasionally wrap the entire ``powershell ...`` invocation in an
    # extra pair of double quotes (typically because over-eager JSON escaping
    # left ``\"`` markers around the whole token). cmd.exe then tries to
    # locate an executable literally named ``"powershell ..."`` and fails
    # with ``The system cannot find the path specified.``. Peel a single
    # surrounding double-quote layer when doing so reveals a recognizable
    # ``powershell`` invocation.
    if len(cmd) >= 2 and cmd[0] == '"' and cmd[-1] == '"':
        inner = cmd[1:-1].strip()
        if re.match(r"(?i)^powershell(?:\.exe)?\b", inner):
            cmd = inner
    if not re.match(r"(?i)^powershell(?:\.exe)?\b", cmd):
        return {"ok": True, "command": cmd}
    m = re.match(
        r"(?is)^powershell(?:\.exe)?\s+-ExecutionPolicy\s+Bypass\s+-Command\s+(.+)$",
        cmd,
    )
    if not m:
        return {
            "ok": False,
            "error": 'On Windows, PowerShell must be called as: powershell -ExecutionPolicy Bypass -Command "<command>"',
        }
    # Normalize executable token to `powershell` while preserving the command payload.
    payload = m.group(1).strip()
    return {"ok": True, "command": f"powershell -ExecutionPolicy Bypass -Command {payload}"}


# Match a ``powershell ... -Command <payload>`` invocation in a way that's
# tolerant of mixed casing and the optional ``.exe`` suffix.
_WIN_POWERSHELL_COMMAND_RE = re.compile(
    r"(?is)^(?P<exe>powershell(?:\.exe)?)\s+(?P<head>(?:-ExecutionPolicy\s+Bypass\s+)?)"
    r"-Command\s+(?P<payload>.+)$"
)


def _strip_powershell_payload_quotes(payload: str) -> Tuple[str, str]:
    """Return ``(inner, quote_kind)`` after peeling one matched outer layer of
    quotes from a PowerShell ``-Command`` payload.

    PowerShell strings escape an embedded ``"`` as ``\\"`` (when the whole
    token came through cmd.exe) or as ``""`` (PS-native). For single-quoted
    strings the escape is ``''``. Quote kind is ``"d"``, ``"s"`` or ``""``.
    Also tolerates payloads where the model double-escaped the outer quotes
    (``\\"...\\"``) — that's invalid powershell-from-cmd syntax but the
    intent is unambiguous.
    """
    s = str(payload or "").strip()
    # Handle backslash-escaped outer double-quote wrapper (``\"...\"``) that
    # over-escaping models tend to produce. Peel both halves before falling
    # through to the standard quote-pair handling.
    if len(s) >= 4 and s.startswith('\\"') and s.endswith('\\"'):
        s = s[2:-2].strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        quote = s[0]
        inner = s[1:-1]
        if quote == '"':
            inner = inner.replace('\\"', '"').replace('""', '"')
            return inner, "d"
        else:
            inner = inner.replace("''", "'")
            return inner, "s"
    return s, ""


_POWERSHELL_LITERAL_ESCAPE_RE = re.compile(r"\\(?P<ch>[nrt\"'])")


def _decode_powershell_literal_escapes(text: str) -> str:
    """Translate the literal escape sequences models commonly emit (``\\n``,
    ``\\r``, ``\\t``, ``\\\"``, ``\\'``) into the real characters they meant.

    PowerShell uses backtick escapes (``` `n ```), so ``\\n`` is otherwise a
    no-op two-character literal in the resulting script. Decoding them is
    therefore safe for legitimate scripts and unlocks here-strings (which
    *require* a real LF immediately after ``@'`` / ``@"``).
    """
    def _sub(match: "re.Match[str]") -> str:
        ch = match.group("ch")
        return {
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "\"": "\"",
            "'": "'",
        }.get(ch, match.group(0))

    return _POWERSHELL_LITERAL_ESCAPE_RE.sub(_sub, str(text or ""))


def _powershell_payload_needs_normalization(payload: str) -> bool:
    """Heuristic: only rewrite payloads that are likely to fail cmd.exe round
    tripping. Keep simple one-liners untouched so we don't perturb commands
    the model already wrote correctly."""
    s = str(payload or "")
    if not s:
        return False
    if "\n" in s or "\r" in s:
        return True
    if "\\n" in s or "\\r" in s:
        return True
    if "@'" in s or '@"' in s:
        return True
    return False


def _encode_powershell_script_as_encoded_command(script: str) -> str:
    """Encode a PowerShell script for the ``-EncodedCommand`` parameter.

    PowerShell expects a Base64 string built from the script's UTF-16-LE
    bytes. This bypasses every layer of cmd.exe / PowerShell quoting since
    the encoded blob is plain ASCII with no whitespace or quote characters.
    """
    return base64.b64encode(str(script or "").encode("utf-16-le")).decode("ascii")


def _normalize_windows_powershell_command_for_compat(command: str) -> str:
    """Make best-effort fixes to common ``powershell -Command "..."`` mistakes
    on Windows so that semantically valid scripts the model emits actually
    reach PowerShell intact.

    Concretely:

    * Decodes literal ``\\n`` / ``\\r`` / ``\\t`` / ``\\"`` escape sequences
      in the ``-Command`` payload to real characters (no-op for normal PS
      strings; required for here-strings to parse).
    * When the resulting payload spans multiple lines or otherwise contains
      content that wouldn't survive cmd.exe argument parsing, re-emits the
      command as ``powershell -ExecutionPolicy Bypass -EncodedCommand
      <base64>`` so quoting concerns are bypassed entirely.

    Returns the original ``command`` unchanged on non-Windows hosts, when
    no PowerShell invocation is detected, or when the payload looks safe
    to leave alone.
    """
    if os.name != "nt":
        return command
    raw = str(command or "")
    if not raw.strip():
        return command
    match = _WIN_POWERSHELL_COMMAND_RE.match(raw.strip())
    if not match:
        return command
    payload_raw = match.group("payload").strip()
    if not _powershell_payload_needs_normalization(payload_raw):
        return command
    payload, quote_kind = _strip_powershell_payload_quotes(payload_raw)
    decoded = _decode_powershell_literal_escapes(payload)
    # If decoding didn't actually change anything and the payload is single
    # line, leave the command alone — the model already wrote something
    # cmd.exe can dispatch.
    if decoded == payload and "\n" not in decoded and "\r" not in decoded:
        # No newlines after decoding either: nothing to fix.
        if quote_kind:
            return command
    encoded = _encode_powershell_script_as_encoded_command(decoded)
    return f"powershell -ExecutionPolicy Bypass -EncodedCommand {encoded}"


def action_shell_command(
    agent: Any,
    command: str,
    confirmed: bool = False,
    interactive: bool = True,
    input_data: Optional[str] = None,
) -> dict:
    """Run a shell command; capture stdout/stderr for AI context while echoing to the terminal."""
    if not command.strip():
        return {"success": False, "error": "Command cannot be empty"}
    manual_confirm_from_ai = bool(getattr(agent, "_manual_confirm_required_shell_once", False))
    if manual_confirm_from_ai:
        agent._manual_confirm_required_shell_once = False
    command = ensure_absolute_script_for_shell_cwd(agent, command.strip())
    command = enforce_workspace_rg_for_shell_command(agent, command)
    command = tune_7z_output_for_piped_terminal(command, agent)
    enforce_res = _enforce_windows_powershell_command_prefix(command)
    if not enforce_res.get("ok", False):
        return {"success": False, "error": str(enforce_res.get("error", "PowerShell command format is invalid"))}
    command = str(enforce_res.get("command") or command)
    # Rescue common Windows PowerShell quoting failures (literal ``\n`` in
    # here-strings, over-escaped wrappers, etc.) without changing the user-
    # visible command summary that already got captured upstream.
    command = _normalize_windows_powershell_command_for_compat(command)
    policy = agent._get_path_policy()
    decision = policy.can_run_shell_in_workdir(
        is_dependency_install=is_dependency_install_command(command),
        is_ai_workspace_script=is_ai_workspace_script_command(agent, command),
    )
    if not decision.get("allowed", False):
        return {"success": False, "error": decision.get("error", "")}
    agent._load_confirm_allowlist()
    execution_policy = str(getattr(agent, "execution_policy", "confirmation")).lower()
    in_allowlist = agent._shell_command_in_allowlist(command)
    force_manual_confirm_by_policy = (
        (
            ((execution_policy in ("moderate", "unlimited")) and (not confirmed))
            or manual_confirm_from_ai
        )
        and (not in_allowlist)
    )
    # Hard guard: if AI/policy requires manual confirmation, never bypass it via confirmed=True.
    if force_manual_confirm_by_policy:
        confirmed = False
    should_prompt_confirm = (
        force_manual_confirm_by_policy
        or ((not confirmed) and (not in_allowlist))
    )
    if should_prompt_confirm:
        # The selection/inline confirmation UI renders the command on its own
        # styled line, so use a command-less question and pass the command
        # separately via ``display_command``. The plain-text fallback inside
        # ``_prompt_confirm_yes_no_maybe_always`` re-appends the command.
        if force_manual_confirm_by_policy:
            prompt_text = _t(
                agent,
                "execution_policy.prompt.manual_confirmation_required_no_command",
                fallback="⚠️ AI requires manual confirmation. Please confirm this command before continuing.",
            )
            confirm_reason = getattr(agent, "_last_auto_confirm_reason", None) or None
        else:
            prompt_text = _t(
                agent,
                "execution_policy.prompt.confirm_shell_no_command",
                fallback="⚠️ Confirm executing this system command?",
            )
            confirm_reason = None
        ok = agent._prompt_confirm_yes_no_maybe_always(
            prompt_text,
            offer_always=agent._shell_confirm_should_offer_always(command),
            kind="shell",
            shell_command=command,
            display_command=command,
            confirm_reason=confirm_reason,
        )
        if not ok:
            return {"success": False, "error": "Operation cancelled by user"}

    import subprocess

    merge_path: Optional[str] = None
    execution_cwd = _resolve_shell_execution_cwd(agent)

    # Snapshot files that may be deleted by this command so we can backup
    # their content and record a delete change if the command removes them.
    _delete_snapshots: Dict[str, str] = {}
    if _is_potential_delete_command(command):
        _delete_targets = _extract_delete_file_paths(command, execution_cwd)
        if _delete_targets:
            _delete_snapshots = _snapshot_files_content(_delete_targets)

    try:
        run_env = os.environ.copy()
        run_env.setdefault("PYTHONUTF8", "1")
        run_env.setdefault("PYTHONIOENCODING", "utf-8")
        run_env.setdefault("PYTHONUNBUFFERED", "1")
        merge_env_name = resolve_model_context_file_env(agent, command)
        if merge_env_name:
            try:
                fd, merge_p = tempfile.mkstemp(prefix="modelctx_", suffix=".txt")
                os.close(fd)
                merge_path = merge_p
                run_env[merge_env_name] = merge_path
            except OSError:
                merge_path = None
        # Global policy: all shell/script execution is non-interactive.
        interactive = False
        return_code = -1
        out = ""
        displayed_out = ""
        aborted_by_user = False
        status_ticker: Optional[_WorkingStatusTicker] = None
        status_ticker_lock = threading.Lock()

        def _stop_status_ticker() -> None:
            nonlocal status_ticker
            with status_ticker_lock:
                if status_ticker is None:
                    return
                try:
                    status_ticker.stop()
                finally:
                    status_ticker = None
        try:
            # The desktop GUI renders its own "Working.../Worked for" indicator,
            # so suppress the terminal ticker (which would otherwise leave a
            # stale "Working... (0s ...)" line in the GUI step output).
            if not bool(getattr(agent, "_gui_plain_stream", False)):
                feedback_line = str(getattr(agent, "_last_tool_call_feedback_line", "") or "")
                status_ticker = _SpinnerTicker(
                    sys.stdout,
                    prefix_line=feedback_line,
                    fps=SHELL_WORKING_STATUS_MARQUEE_FPS,
                )
                status_ticker.start()
            if interactive:
                import codecs

                stdout_chunks: List[str] = []
                stream_chunks_lock = threading.Lock()
                allow_realtime_echo = threading.Event()
                allow_realtime_echo.set()

                def _restore_console_after_interactive() -> None:
                    if sys.platform != "win32":
                        return
                    try:
                        _safe_console_write(
                            "\x1b[0m\x1b[?25h\x1b[?2004l",
                            sys.stdout,
                            append_newline=False,
                        )
                    except Exception:
                        pass

                def _stream_and_capture(
                    pipe: Any,
                    target: Any,
                    bucket: List[str],
                ) -> None:
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    realtime_started = False
                    try:
                        while True:
                            if hasattr(pipe, "read1"):
                                chunk = pipe.read1(1024)
                            else:
                                chunk = pipe.read(1024)
                            if not chunk:
                                break
                            text_chunk = decoder.decode(chunk, final=False)
                            if text_chunk:
                                with stream_chunks_lock:
                                    bucket.append(text_chunk)
                                _stop_status_ticker()
                                if allow_realtime_echo.is_set():
                                    if not realtime_started:
                                        realtime_started = True
                                        ensure_line = getattr(agent, "_ensure_terminal_line_start", None)
                                        if callable(ensure_line):
                                            try:
                                                ensure_line()
                                            except Exception:
                                                pass
                                    _safe_console_write(text_chunk, target, append_newline=False)
                        tail = decoder.decode(b"", final=True)
                        if tail:
                            with stream_chunks_lock:
                                bucket.append(tail)
                            _stop_status_ticker()
                            if allow_realtime_echo.is_set():
                                if not realtime_started:
                                    realtime_started = True
                                    ensure_line = getattr(agent, "_ensure_terminal_line_start", None)
                                    if callable(ensure_line):
                                        try:
                                            ensure_line()
                                        except Exception:
                                            pass
                                _safe_console_write(tail, target, append_newline=False)
                    except Exception:
                        pass
                    finally:
                        try:
                            pipe.close()
                        except Exception:
                            pass

                try:
                    process = None
                    process = subprocess.Popen(
                        command,
                        shell=True,
                        cwd=str(execution_cwd.resolve()),
                        env=run_env,
                        stdin=sys.stdin,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=False,
                    )
                    reg_proc = getattr(agent, "_register_interruptible_process", None)
                    if callable(reg_proc):
                        reg_proc(process)
                    t_out = threading.Thread(
                        target=_stream_and_capture,
                        args=(process.stdout, sys.stdout, stdout_chunks),  # type: ignore[arg-type]
                        daemon=True,
                    )
                    t_out.start()
                    return_code = process.wait()
                    # Stop direct stream echo immediately after process exit to
                    # prevent delayed raw chunks from appearing in later turns.
                    allow_realtime_echo.clear()
                    t_out.join(timeout=1.0)
                    # Give readers a brief extra window to capture residual bytes
                    # without writing them directly to the terminal.
                    t_out.join(timeout=0.2)
                    with stream_chunks_lock:
                        out = "".join(stdout_chunks)
                    out = _collapse_cr_output(out)
                    consume_abort = getattr(agent, "_consume_process_aborted", None)
                    if callable(consume_abort):
                        aborted_by_user = bool(consume_abort(process))
                    if aborted_by_user:
                        out = str(out) + ("command aborted by user\n")
                finally:
                    try:
                        unreg_proc = getattr(agent, "_unregister_interruptible_process", None)
                        if callable(unreg_proc):
                            unreg_proc(process)
                    except Exception:
                        pass
                    _restore_console_after_interactive()
            else:
                import codecs

                run_input: Optional[bytes] = None
                if input_data is not None:
                    run_input = str(input_data).encode("utf-8")
                stdout_chunks: List[str] = []
                stdout_completed_lines: List[str] = []
                stdout_pending_line_state: Dict[str, str] = {"text": ""}
                stream_chunks_lock = threading.Lock()
                create_streams = getattr(agent, "_create_direct_shell_output_streams", None)
                process_ref: Dict[str, Any] = {"process": None}
                live_tail_limit = _dynamic_tail_line_limit(sys.stdout, reserved_lines=1)

                def _is_current_process_aborted() -> bool:
                    checker = getattr(agent, "_is_process_aborted", None)
                    if not callable(checker):
                        return False
                    try:
                        return bool(checker(process_ref.get("process")))
                    except Exception:
                        return False

                live_stream_state: Dict[str, Any] = {
                    "first_line_emitted": False,
                    "rendered_line_count": 0,
                    "cursor_at_line_start": True,
                    "_first_write_cleared_ticker_line": False,
                    "_first_text_emitted_notified": False,
                    "_suppress_first_write_clear": False,
                    "apply_gray": False,
                    "max_visible_lines": max(1, int(live_tail_limit)),
                    "max_visible_lines_provider": (
                        lambda: _dynamic_tail_line_limit(sys.stdout, reserved_lines=1)
                    ),
                    "suppress_leading_blank_once": True,
                    "on_text_emitted": _stop_status_ticker,
                    "suppress_desync_when": _is_current_process_aborted,
                }

                def _recover_live_window_desync_once() -> None:
                    if bool(live_stream_state.get("_desync_recovery_in_progress", False)):
                        return
                    live_stream_state["_desync_recovery_in_progress"] = True
                    live_stream_state["disable_live_render"] = True
                    _stop_status_ticker()
                    snapshot_out = ""
                    try:
                        reload_fn = getattr(agent, "_reload_chat_history_from_anchor_on_resize", None)
                        if callable(reload_fn):
                            try:
                                reload_fn(include_startup_overview=True)
                            except TypeError:
                                reload_fn()
                    except Exception:
                        pass
                    try:
                        with stream_chunks_lock:
                            snapshot_out = _strip_console_color_controls(
                                "".join(stdout_chunks)
                            )
                    except Exception:
                        pass
                    display_out = snapshot_out
                    live_limit_out = int(live_stream_state.get("max_visible_lines", 1) or 1)
                    omitted_base = 0
                    try:
                        live_limit_out = max(
                            1,
                            int(live_stream_state.get("max_visible_lines", 0) or 0),
                            int(_dynamic_tail_line_limit(sys.stdout, reserved_lines=1) or 0),
                        )
                        if display_out:
                            display_out, omitted_out = _select_logical_tail_output_for_live_replay(
                                display_out,
                                sys.stdout,
                                live_limit_out,
                                display_indent_width=4,
                            )
                            omitted_base += int(omitted_out or 0)
                            display_out = _strip_console_color_controls(display_out)
                    except Exception:
                        pass
                    if not display_out:
                        live_stream_state["_desync_skip_current_chunk"] = True
                        live_stream_state["drop_until_next_newline"] = bool(
                            str(stdout_pending_line_state.get("text", ""))
                        )
                        live_stream_state["disable_live_render"] = False
                        live_stream_state["_desync_recovery_in_progress"] = False
                        return
                    restore_limit = live_stream_state.get("max_visible_lines")
                    restore_provider = live_stream_state.get("max_visible_lines_provider")
                    try:
                        live_stream_state["first_line_emitted"] = bool(int(omitted_base or 0) > 0)
                        live_stream_state["rendered_line_count"] = 0
                        live_stream_state["cursor_at_line_start"] = True
                        live_stream_state["cursor_visual_col"] = 0
                        live_stream_state["_first_write_cleared_ticker_line"] = True
                        live_stream_state["_live_rendered_buffer"] = ""
                        live_stream_state["_live_omitted_base_lines"] = int(omitted_base or 0)
                        live_stream_state["suspend_desync_detection"] = True
                        live_stream_state["suspend_drop_until_next_newline"] = True
                        live_stream_state["max_visible_lines"] = max(
                            int(live_limit_out or 0) + 1000,
                            int(SHELL_OUTPUT_DISPLAY_TAIL_LINES or 0) + 1000,
                        )
                        live_stream_state["max_visible_lines_provider"] = None
                        if callable(create_streams):
                            preview_out, _ = create_streams(live_stream_state)
                        else:
                            preview_out = sys.stdout
                        live_stream_state["disable_live_render"] = False
                        if display_out:
                            preview_out.write(display_out)
                            preview_out.flush()
                        if display_out:
                            live_stream_state["_desync_skip_current_chunk"] = True
                            live_stream_state["drop_until_next_newline"] = bool(
                                str(stdout_pending_line_state.get("text", ""))
                            )
                        live_stream_state["_stream_local_state_version"] = int(
                            live_stream_state.get("_stream_local_state_version", 0) or 0
                        ) + 1
                    except Exception:
                        pass
                    finally:
                        try:
                            live_stream_state["max_visible_lines"] = restore_limit
                            live_stream_state["max_visible_lines_provider"] = restore_provider
                        except Exception:
                            pass
                        live_stream_state["suspend_drop_until_next_newline"] = False
                        live_stream_state["suspend_desync_detection"] = False
                        live_stream_state["disable_live_render"] = False
                        live_stream_state["_desync_recovery_in_progress"] = False

                live_stream_state["on_live_window_desynced"] = _recover_live_window_desync_once
                if callable(create_streams):
                    try:
                        out_stream, _ = create_streams(live_stream_state)
                    except Exception:
                        out_stream = sys.stdout
                else:
                    out_stream = sys.stdout

                def _stream_and_capture(
                    pipe: Any,
                    target: Any,
                    bucket: List[str],
                    completed_lines: List[str],
                    pending_line_state: Dict[str, str],
                ) -> None:
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    realtime_started = False

                    def _write_display_chunk(text: str) -> None:
                        try:
                            target.write(text)
                            target.flush()
                        except Exception:
                            # Rendering must not stop pipe draining. Keep the
                            # bounded live window intact and let the final tail
                            # replay come from the captured output.
                            pass

                    try:
                        while True:
                            if hasattr(pipe, "read1"):
                                chunk = pipe.read1(1024)
                            else:
                                chunk = pipe.read(1024)
                            if not chunk:
                                break
                            text_chunk = decoder.decode(chunk, final=False)
                            if text_chunk:
                                with stream_chunks_lock:
                                    bucket.append(text_chunk)
                                    _append_completed_output_lines(
                                        text_chunk,
                                        completed_lines,
                                        pending_line_state,
                                    )
                                _stop_status_ticker()
                                if not realtime_started:
                                    realtime_started = True
                                    ensure_line = getattr(agent, "_ensure_terminal_line_start", None)
                                    if callable(ensure_line):
                                        try:
                                            ensure_line()
                                        except Exception:
                                            pass
                                _write_display_chunk(text_chunk)
                        tail = decoder.decode(b"", final=True)
                        if tail:
                            with stream_chunks_lock:
                                bucket.append(tail)
                                _append_completed_output_lines(
                                    tail,
                                    completed_lines,
                                    pending_line_state,
                                )
                            _stop_status_ticker()
                            if not realtime_started:
                                realtime_started = True
                                ensure_line = getattr(agent, "_ensure_terminal_line_start", None)
                                if callable(ensure_line):
                                    try:
                                        ensure_line()
                                    except Exception:
                                        pass
                            _write_display_chunk(tail)
                    except Exception:
                        pass
                    finally:
                        try:
                            pipe.close()
                        except Exception:
                            pass

                try:
                    process = None
                    _winpty_obj = None
                    if _WINPTY_PTYPROCESS is not None and subprocess.Popen is _ORIG_SUBPROCESS_POPEN:
                        try:
                            _comspec = run_env.get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
                            _raw_pty = _WINPTY_PTYPROCESS.spawn(
                                [_comspec, "/c", command],
                                cwd=str(execution_cwd.resolve()),
                                env=run_env,
                            )
                            _winpty_obj = _WinPtyProc(_raw_pty)
                            _winpty_obj.stdin = _WinPtyWriter(_raw_pty)
                            process = _winpty_obj
                        except Exception:
                            _winpty_obj = None
                    if process is None:
                        process = subprocess.Popen(
                            command,
                            shell=True,
                            cwd=str(execution_cwd.resolve()),
                            env=run_env,
                            stdin=subprocess.DEVNULL if run_input is None else subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=False,
                        )
                    process_ref["process"] = process
                    if run_input is not None:
                        try:
                            if process.stdin is not None:
                                process.stdin.write(run_input)
                                process.stdin.flush()
                        except Exception:
                            pass
                        finally:
                            try:
                                if process.stdin is not None:
                                    process.stdin.close()
                            except Exception:
                                pass
                    reg_proc = getattr(agent, "_register_interruptible_process", None)
                    if callable(reg_proc):
                        reg_proc(process)
                    t_out = threading.Thread(
                        target=_stream_and_capture,
                        args=(
                            process.stdout,
                            out_stream,
                            stdout_chunks,
                            stdout_completed_lines,
                            stdout_pending_line_state,
                        ),  # type: ignore[arg-type]
                        daemon=True,
                    )
                    t_out.start()
                    return_code = process.wait()
                    consume_abort = getattr(agent, "_consume_process_aborted", None)
                    if callable(consume_abort):
                        aborted_by_user = bool(consume_abort(process))
                    if aborted_by_user:
                        live_stream_state["suspend_desync_detection"] = True
                        try:
                            agent._suppress_next_prompt_chat_reload_once = True
                        except Exception:
                            pass
                    t_out.join(timeout=1.0)
                    t_out.join(timeout=0.2)
                    with stream_chunks_lock:
                        out = "".join(stdout_chunks)
                    out = _collapse_cr_output(out)
                    if aborted_by_user:
                        out = str(out) + ("command aborted by user\n")
                finally:
                    try:
                        unreg_proc = getattr(agent, "_unregister_interruptible_process", None)
                        if callable(unreg_proc):
                            unreg_proc(process)
                    except Exception:
                        pass
            _stop_status_ticker()

            out = append_shell_merge_output_path(out, return_code, merge_path)
            out_tail_limit = _dynamic_tail_line_limit(sys.stdout)
            displayed_out = _build_tail_output_for_display(
                out,
                sys.stdout,
                out_tail_limit,
                language=getattr(agent, "display_language", None),
            )
            displayed_out_plain = _strip_console_color_controls(displayed_out)
            should_replay_out = True
            if interactive:
                # Interactive mode already streamed raw output to console.
                # Skip replay when output fully fits within the tail limit and
                # no post-processing changed the displayed text.
                if displayed_out and (_count_output_lines(out) <= out_tail_limit) and (displayed_out == out):
                    should_replay_out = False
            gui_mode = bool(getattr(agent, "_gui_no_wrap", False))
            gui_streamed = (
                bool(live_stream_state.get("_first_text_emitted_notified"))
                if gui_mode
                else False
            )
            if gui_mode:
                # The GUI streamed the full raw output live and cannot clear a
                # terminal window, so never replay the tail-truncated copy.
                should_replay_out = False
                # Close the command-output block opened by the raw stream so the
                # GUI can render it as a single padded node. The no-output case
                # is wrapped by the history-output replay path below instead.
                if gui_streamed:
                    try:
                        sys.stdout.write(GUI_CMD_OUTPUT_END)
                        sys.stdout.flush()
                    except Exception:
                        pass
            last_rendered_chunk = ""
            replay_out_text = displayed_out_plain if (displayed_out and should_replay_out) else ""
            if (
                (not replay_out_text)
                and int(return_code) == 0
                and (not aborted_by_user)
                and not (gui_mode and gui_streamed)
            ):
                replay_out_text = "(no output)\n"
            replay_rendered_lines = 0
            lock_obj = live_stream_state.get("_write_lock")
            lock_ctx = lock_obj if hasattr(lock_obj, "__enter__") and hasattr(lock_obj, "__exit__") else contextlib.nullcontext()
            with lock_ctx:
                if not gui_mode:
                    _clear_streamed_output_window(
                        sys.stdout,
                        int(live_stream_state.get("rendered_line_count", 0) or 0),
                        bool(live_stream_state.get("cursor_at_line_start", True)),
                    )
                if replay_out_text:
                    replay_direct = getattr(agent, "_print_direct_shell_history_output", None)
                    if callable(replay_direct):
                        try:
                            replay_rendered_lines = max(
                                0,
                                int(replay_direct(replay_out_text, "") or 0),
                            )
                        except Exception:
                            replay_rendered_lines = 0
                    else:
                        if replay_out_text:
                            _safe_console_write(_gray_shell_display_text(replay_out_text), sys.stdout, append_newline=False)
                        replay_rendered_lines = max(
                            0,
                            _count_output_lines(replay_out_text),
                        )
                    last_rendered_chunk = replay_out_text
                if (not last_rendered_chunk) and interactive:
                    if (not should_replay_out) and out:
                        last_rendered_chunk = out
                # Keep next assistant/status lines on a fresh line even when command
                # output does not end with newline. This avoids off-by-one over-clear
                # caused by mixing "Thinking..." into the output's last visual line.
                if last_rendered_chunk and not str(last_rendered_chunk).endswith("\n"):
                    _safe_console_write("\n", sys.stdout, append_newline=False)
                    replay_out_text = str(replay_out_text) + "\n"
                try:
                    agent._last_terminal_block_kind = "command_output"
                    agent._terminal_cursor_at_line_start = True
                except Exception:
                    pass
                try:
                    agent._last_shell_output_visible_lines = 0
                except Exception:
                    pass
                banner_lines = 0
                if aborted_by_user:
                    try:
                        banner_fn = getattr(agent, "_print_conversation_interrupted_banner", None)
                        if callable(banner_fn):
                            banner_lines = int(banner_fn() or 0)
                    except Exception:
                        banner_lines = 0
            # Determine the chat data directory for full output storage.
            _shell_output_path: Optional[Path] = None
            try:
                _chat_mgr = getattr(agent, "_chat_state_manager", None)
                _chat_id = str(getattr(agent, "active_chat_id", "") or "")
                if _chat_mgr is not None and _chat_id:
                    _data_dir = _chat_mgr.chat_data_dir_for_chat(_chat_id)
                    if _data_dir is not None:
                        _stem = datetime.datetime.now().strftime("shell_output_%Y%m%d_%H%M%S_%f")
                        _shell_output_path = _data_dir / f"{_stem}.txt"
            except Exception:
                _shell_output_path = None

            _shell_buf = CommandExecutionBuffer(out)
            _shell_rendered = _shell_buf.render(file_path=_shell_output_path)
            _shell_was_truncated = bool(_shell_output_path) and (_shell_rendered != _shell_buf.raw)

            base_out: Dict[str, Any] = {
                "output": _shell_rendered,
                "return_code": return_code,
                "interactive": interactive,
                "aborted_by_user": bool(aborted_by_user),
                "display_output": replay_out_text,
                "display_rendered_lines": int(replay_rendered_lines) + int(banner_lines),
            }
            if _shell_was_truncated:
                base_out["full_output_path"] = str(_shell_output_path)

            # Check for file deletions: compare snapshotted files against
            # current filesystem state, backup deleted content, and record
            # a delete change via the file_change_tracker.
            if _delete_snapshots:
                try:
                    _chat_mgr2 = getattr(agent, "_chat_state_manager", None)
                    _chat_id2 = str(getattr(agent, "active_chat_id", "") or "")
                    _backups_dir: Optional[Path] = None
                    if _chat_mgr2 is not None and _chat_id2:
                        _backups_dir = _chat_mgr2.chat_backups_dir_for_chat(_chat_id2)
                    _tracker = getattr(agent, "file_change_tracker", None)
                    for _path_str, _content in _delete_snapshots.items():
                        _p = Path(_path_str)
                        if not _p.exists():
                            _backup_name: Optional[str] = None
                            if _backups_dir is not None:
                                _backup_name = _backup_deleted_file(_content, _p, _backups_dir)
                            if _tracker is not None:
                                _tracker.record_delete(
                                    file_path=_path_str,
                                    source="shell",
                                    content_before=_content,
                                    backup_path=_backup_name,
                                )
                except Exception:
                    pass
        finally:
            _stop_status_ticker()
            if merge_path:
                try:
                    os.unlink(merge_path)
                except OSError:
                    pass

        if return_code == 0:
            register_outputs_from_shell_command(agent, command)
            if agent._is_workspace_skill_path(execution_cwd):
                agent._reload_skills_if_workspace_skill_changed([execution_cwd])
            removed = try_remove_ephemeral_script_after_shell(agent, command)
            if removed:
                agent._last_auto_removed_ephemeral = removed
                return {
                    "success": True,
                    "message": (
                        f"Command executed successfully; temporary script '«{removed}»' was auto-deleted."
                        " Please do not run delete on this file again."
                    ),
                    "auto_removed_ephemeral_script": removed,
                    **base_out,
                }
            if interactive:
                return {"success": True, "message": "Command executed successfully (interactive mode)", **base_out}
            return {"success": True, "message": "Command executed successfully", **base_out}

        combo = str(out)
        cmd_l = command.lower()
        is_skillhub_install = ("skillhub_installer.py" in cmd_l) and (" install " in f" {cmd_l} ")
        user_cancelled = ("installation aborted by user." in combo.lower()) or (return_code == 2)
        if is_skillhub_install and user_cancelled:
            return {
                "success": True,
                "cancelled": True,
                "terminal_state": "user_cancelled",
                "message": "Installation was cancelled by the user. Flow ended (should not auto-retry).",
                **base_out,
            }
        no_match_message = _classify_no_match_exit(command, return_code, combo)
        if no_match_message:
            register_outputs_from_shell_command(agent, command)
            return {
                "success": True,
                "no_matches": True,
                "message": no_match_message,
                **base_out,
            }
        return {
            "success": False,
            "error": f"Command execution failed, exit code: {return_code}",
            **base_out,
        }

    except Exception as e:
        return {"success": False, "error": f"System command execution error: {str(e)}"}
    finally:
        _reset_work_directory_to_startup_initial(agent)
        restore_app_console_title()


def action_project_context_search(agent: Any, params: Dict[str, Any]) -> dict:
    if not agent._project_context_tool_allowed():
        return {
            "success": False,
            "error": "project_context_search is not supported in the Default workspace. Please switch to a non-Default workspace and try again.",
        }

    query = str(params.get("query") or "").strip()
    max_files = params.get("max_files", 12)
    refresh = params.get("refresh", None)
    refresh_async = bool(params.get("refresh_async", False))
    status_only = bool(params.get("status_only", False))
    force_rebuild = bool(params.get("force_rebuild", False))
    call_graph_symbol = str(params.get("call_graph") or "").strip()
    call_graph_direction = str(params.get("call_graph_direction") or "both").strip()

    try:
        max_files_i = int(max_files)
    except Exception:
        max_files_i = 12
    if max_files_i <= 0:
        max_files_i = 12
    if max_files_i > 50:
        max_files_i = 50

    agent._bind_project_index_workspace()
    if status_only:
        st = agent._project_context_index.status()
        st["message"] = "Project context index status"
        return st
    if call_graph_symbol:
        if force_rebuild:
            idx_res = agent._project_context_index.refresh_index(force=True)
            if not idx_res.get("success", False):
                return idx_res
        return agent._project_context_index.call_graph(
            symbol=call_graph_symbol,
            direction=call_graph_direction,
            max_results=max_files_i,
            auto_refresh=(
                ((True if refresh is None else bool(refresh)) or force_rebuild)
                and (not refresh_async)
            ),
            refresh_timeout_ms=(None if len(getattr(agent._project_context_index, "files", {})) == 0 else 10000),
        )
    if not query:
        return {"success": False, "error": "Missing required parameter: query for project_context_search"}

    if force_rebuild:
        idx_res = agent._project_context_index.refresh_index(force=True)
        if not idx_res.get("success", False):
            return idx_res
    elif refresh_async:
        agent._schedule_project_context_refresh_background(force=False, reason="project-context-search")

    result = agent._project_context_index.search(
        query=query,
        max_files=max_files_i,
        auto_refresh=(
            ((True if refresh is None else bool(refresh)) or force_rebuild) and (not refresh_async)
        ),
        refresh_timeout_ms=(None if len(getattr(agent._project_context_index, "files", {})) == 0 else 10000),
    )
    if refresh_async:
        result["refresh_scheduled"] = True
    return result


def register_outputs_from_shell_command(agent: Any, command: str) -> None:
    for pat in (
        r"to_excel\s*\(\s*['\"]([^'\"]+)['\"]",
        r"to_csv\s*\(\s*['\"]([^'\"]+)['\"]",
        r"ExcelWriter\s*\(\s*['\"]([^'\"]+)['\"]",
    ):
        for m in re.finditer(pat, command, re.I):
            agent._try_register_ai_output_literal(m.group(1))


_SCRIPT_SUFFIXES = (
    ".py",
    ".ps1",
    ".bat",
    ".cmd",
    ".sh",
    ".bash",
    ".zsh",
    ".ksh",
    ".fish",
    ".vbs",
    ".js",
    ".jse",
    ".wsf",
    ".rb",
    ".pl",
    ".php",
    ".lua",
    ".r",
    ".psm1",
)

_SCRIPT_INTERPRETER_EXES = {
    "python",
    "pythonw",
    "py",
    "node",
    "nodejs",
    "ruby",
    "perl",
    "php",
    "lua",
    "rscript",
    "cscript",
    "wscript",
    "bash",
    "sh",
    "zsh",
    "ksh",
    "dash",
    "fish",
    "pwsh",
    "powershell",
    "deno",
}

_OPTION_VALUE_FLAGS_BY_EXE = {
    "python": {"-x"},
    "pythonw": {"-x"},
    "py": set(),
    "node": set(),
    "nodejs": set(),
    "ruby": set(),
    "perl": set(),
    "php": {"-f"},
    "lua": set(),
    "rscript": {"-e"},
    "cscript": set(),
    "wscript": set(),
    "bash": set(),
    "sh": set(),
    "zsh": set(),
    "ksh": set(),
    "dash": set(),
    "fish": set(),
    "pwsh": {"-file", "-f"},
    "powershell": {"-file", "-f", "/f"},
}

_INLINE_EXEC_FLAGS_BY_EXE = {
    "python": {"-c", "-m"},
    "pythonw": {"-c", "-m"},
    "py": {"-c", "-m"},
    "node": {"-e", "-p"},
    "nodejs": {"-e", "-p"},
    "ruby": {"-e"},
    "perl": {"-e", "-m"},
    "php": {"-r"},
    "lua": {"-e"},
    "rscript": {"-e"},
    "pwsh": {"-command", "-c", "/c", "-encodedcommand", "-enc", "-e"},
    "powershell": {"-command", "-c", "/c", "-encodedcommand", "-enc", "-e"},
}


def _split_shell_like(command: str) -> List[str]:
    try:
        return shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return command.split()


def _token_exe_base(token: str) -> str:
    base = token.replace("\\", "/").split("/")[-1].lower()
    if base.endswith(".exe"):
        return base[:-4]
    return base


def _strip_wrapping_quotes(token: str) -> str:
    t = str(token or "").strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        return t[1:-1]
    return t


def _find_option_value(parts: List[str], names: tuple[str, ...]) -> Optional[str]:
    wanted = {n.lower() for n in names}
    for i in range(1, len(parts) - 1):
        if parts[i].lower() in wanted:
            return parts[i + 1]
    return None


def _unwrap_windows_powershell_command(command: str) -> str:
    s = str(command or "").strip()
    if not s:
        return s
    m = re.match(
        r"(?is)^powershell(?:\.exe)?\s+-ExecutionPolicy\s+Bypass\s+-Command\s+(.+)$",
        s,
    )
    if not m:
        m = re.match(
            r"(?is)^(?:powershell|pwsh)(?:\.exe)?\s+.*?(?:-Command|-C|/C)\s+(.+)$",
            s,
        )
        if not m:
            return s
    payload = m.group(1).strip()
    if len(payload) >= 2 and payload[0] == payload[-1] and payload[0] in ("'", '"'):
        quote = payload[0]
        payload = payload[1:-1]
        if quote == '"':
            payload = payload.replace('`"', '"')
        else:
            payload = payload.replace("''", "'")
    payload = payload.replace('\\"', '"')
    return payload.strip()


def _unwrap_shell_command_layers(command: str, max_depth: int = 8) -> str:
    s = str(command or "").strip()
    if not s:
        return s
    for _ in range(max(1, max_depth)):
        changed = False
        if s.lower().startswith("call "):
            s = s[5:].strip()
            changed = True
        ps_unwrapped = _unwrap_windows_powershell_command(s)
        if ps_unwrapped != s:
            s = ps_unwrapped
            changed = True
        parts = _split_shell_like(s)
        if not parts:
            return s
        exe = _token_exe_base(parts[0])
        if len(parts) >= 3 and exe == "cmd" and parts[1].lower() in ("/c", "/k"):
            s = " ".join(parts[2:]).strip()
            changed = True
        elif exe in ("bash", "sh", "zsh", "ksh", "dash", "fish"):
            payload = _find_option_value(parts, ("-c", "-lc"))
            if payload is not None:
                s = _strip_wrapping_quotes(payload).strip()
                changed = True
        elif exe == "env":
            payload = _find_option_value(parts, ("-s", "--split-string"))
            if payload is not None:
                s = _strip_wrapping_quotes(payload).strip()
                changed = True
            else:
                env_opts_need_value = {"-u", "--unset", "-c", "--chdir", "-p", "--path"}
                i = 1
                if i < len(parts) and parts[i] == "--":
                    i += 1
                while i < len(parts):
                    t = parts[i]
                    if t == "--":
                        i += 1
                        break
                    if t.startswith("-"):
                        if t.lower() in env_opts_need_value and i + 1 < len(parts):
                            i += 2
                            continue
                        if t.lower().startswith("--unset="):
                            i += 1
                            continue
                        i += 1
                        continue
                    if "=" in t and not t.startswith(("/", "\\", ".", "-")):
                        i += 1
                        continue
                    break
                if i > 1 and i < len(parts):
                    s = " ".join(parts[i:]).strip()
                    changed = True
        elif exe in ("sudo", "doas", "nohup", "setsid"):
            sudo_opts_need_value = {
                "-u",
                "-g",
                "-h",
                "-p",
                "-r",
                "-t",
                "-c",
                "--user",
                "--group",
                "--host",
                "--prompt",
                "--role",
                "--type",
                "--chdir",
                "--close-from",
            }
            doas_opts_need_value = {"-u", "-c"}
            i = 1
            while i < len(parts):
                t = parts[i]
                if t == "--":
                    i += 1
                    break
                if t.startswith("-"):
                    tl = t.lower()
                    if (
                        (exe == "sudo" and tl in sudo_opts_need_value)
                        or (exe == "doas" and tl in doas_opts_need_value)
                    ) and i + 1 < len(parts):
                        i += 2
                        continue
                    i += 1
                    continue
                break
            if i > 1 and i < len(parts):
                s = " ".join(parts[i:]).strip()
                changed = True
        if not changed:
            break
    return s


# Tools that document a non-zero exit code as "ran successfully but found
# no matches". For these, exit code 1 with empty stdout is a normal
# outcome, not a hard failure. We surface this as ``success=True`` with
# an explanatory message so the model doesn't think the tool itself
# broke and switch to a worse alternative.
_NO_MATCH_FRIENDLY_TOOLS: frozenset[str] = frozenset({
    "rg",
    "ripgrep",
    "ag",          # the_silver_searcher
    "ack",
    "grep",
    "egrep",
    "fgrep",
    "findstr",
})


def _shell_command_has_compound_operator(command: str) -> bool:
    """Return True when the command contains a pipe / boolean / sequencer.

    These operators (``|``, ``||``, ``&&``, ``;``, ``&``) make the final
    exit code reflect more than just the head tool's behavior, so the
    no-match-friendly relaxation must not apply.
    """
    s = str(command or "")
    in_single = False
    in_double = False
    backtick = False
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n and not in_single:
            i += 2
            continue
        if ch == "'" and not in_double and not backtick:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single and not backtick:
            in_double = not in_double
            i += 1
            continue
        if ch == "`" and not in_single and not in_double:
            backtick = not backtick
            i += 1
            continue
        if not (in_single or in_double or backtick):
            if ch in ("|", ";"):
                return True
            if ch == "&":
                # Ignore the trailing ``&`` form only when we hit a single
                # ``&`` at end of string (rare); both ``&`` and ``&&``
                # affect exit-code semantics so we treat any unquoted
                # ``&`` as compound.
                return True
        i += 1
    return False


def _classify_no_match_exit(
    command: str,
    return_code: int,
    stdout_text: str,
) -> Optional[str]:
    """Classify ``return_code == 1`` from a single search-tool invocation.

    Returns a localized explanation string when the command should be
    treated as a successful "no matches found" result; returns ``None``
    when the original failure classification must stand.
    """
    if int(return_code) != 1:
        return None
    if str(stdout_text or "").strip():
        return None
    inner = _unwrap_shell_command_layers(command)
    if not inner.strip():
        return None
    if _shell_command_has_compound_operator(inner):
        return None
    parts = _split_shell_like(inner)
    if not parts:
        return None
    base = _token_exe_base(_strip_wrapping_quotes(parts[0]))
    if base not in _NO_MATCH_FRIENDLY_TOOLS:
        return None
    return (
        f"`{base}` exited with code 1 because it found no matches. "
        "The tool ran successfully; this is the documented \"no matches\" exit code, "
        "not a real failure. Adjust the query or pattern if matches were expected."
    )


def parse_shell_invoked_script_path(agent: Any, command: str) -> Optional[Path]:
    s = _unwrap_shell_command_layers(command.strip())
    if not s:
        return None
    parts = _split_shell_like(s)
    if not parts:
        return None
    exe = _token_exe_base(parts[0])

    def _resolve_script_token(tok_raw: str) -> Optional[Path]:
        tok = _strip_wrapping_quotes(tok_raw).strip()
        if not tok:
            return None
        if tok.startswith(".\\") or tok.startswith("./"):
            tok = tok[2:]
        p = Path(tok)
        if not p.is_absolute():
            p_wd, p_temp, p_ws = agent._workspace_relative_script_triple(p)
            if p_wd.is_file():
                return p_wd
            if p_temp.is_file():
                return p_temp
            if p_ws.is_file():
                return p_ws
            return p_wd
        try:
            return p.resolve()
        except OSError:
            return p

    if exe in _SCRIPT_INTERPRETER_EXES and len(parts) >= 2:
        if exe == "deno":
            # deno run <script>
            if len(parts) >= 3 and parts[1].lower() == "run":
                i = 2
                while i < len(parts):
                    t = _strip_wrapping_quotes(parts[i]).lower()
                    if t == "--":
                        i += 1
                        break
                    if t.startswith("-"):
                        i += 1
                        continue
                    break
                if i >= len(parts):
                    return None
                return _resolve_script_token(parts[i])
            return None

        inline_flags = _INLINE_EXEC_FLAGS_BY_EXE.get(exe, set())
        value_flags = _OPTION_VALUE_FLAGS_BY_EXE.get(exe, set())
        i = 1
        while i < len(parts):
            t = _strip_wrapping_quotes(parts[i])
            tl = t.lower()
            if tl == "--":
                i += 1
                break
            if tl in inline_flags:
                return None
            if tl in value_flags:
                if tl in ("-file", "-f", "/f"):
                    if i + 1 >= len(parts):
                        return None
                    return _resolve_script_token(parts[i + 1])
                i += 2
                continue
            if t.startswith("-") or t.startswith("/"):
                i += 1
                continue
            break
        if i >= len(parts):
            return None
        return _resolve_script_token(parts[i])

    tok = _strip_wrapping_quotes(parts[0])
    if tok.lower().endswith(_SCRIPT_SUFFIXES):
        return _resolve_script_token(tok)
    return None


def rewrite_shell_command_script_arg_to_abs(agent: Any, command: str, resolved: Path) -> str:
    import subprocess

    s = str(command or "").strip()
    if not s:
        return command
    call_prefix = ""
    if s.lower().startswith("call "):
        call_prefix = "call "
        s = s[5:].strip()
    parts = _split_shell_like(s)
    if not parts:
        return command
    base0 = _token_exe_base(parts[0])
    if base0 in ("powershell", "pwsh"):
        payload = _find_option_value(parts, ("-command", "-c", "/c"))
        if payload is not None:
            inner_re = rewrite_shell_command_script_arg_to_abs(agent, payload, resolved)
            if inner_re == payload:
                return command
            new_parts = list(parts)
            for i in range(1, len(new_parts) - 1):
                if new_parts[i].lower() in ("-command", "-c", "/c"):
                    new_parts[i + 1] = inner_re
                    break
            if os.name == "nt":
                return call_prefix + subprocess.list2cmdline(new_parts)
            return call_prefix + shlex.join(new_parts)
    if len(parts) >= 3 and base0 == "cmd" and parts[1].lower() in ("/c", "/k"):
        inner = " ".join(parts[2:])
        inner_re = rewrite_shell_command_script_arg_to_abs(agent, inner, resolved)
        if inner_re == inner:
            return command
        if os.name == "nt":
            return call_prefix + subprocess.list2cmdline([parts[0], parts[1], inner_re])
        return f"{call_prefix}{parts[0]} {parts[1]} {inner_re}"

    exe = base0
    if exe not in ("python", "pythonw", "py"):
        return command
    i = 1
    while i < len(parts):
        t = parts[i].strip('"').strip("'")
        if t in ("-m", "-c"):
            return command
        if t.startswith("-") and len(t) > 1:
            i += 1
            continue
        break
    if i >= len(parts):
        return command
    tok = parts[i].strip('"').strip("'")
    if tok.startswith(".\\") or tok.startswith("./"):
        tok = tok[2:]
    p = Path(tok)
    if not p.is_absolute():
        p_wd, p_temp, p_ws = agent._workspace_relative_script_triple(p)
        if p_wd.is_file():
            cand = p_wd
        elif p_temp.is_file():
            cand = p_temp
        elif p_ws.is_file():
            cand = p_ws
        else:
            cand = p_wd
    else:
        try:
            cand = Path(tok).resolve()
        except OSError:
            return command
    if agent._ephemeral_path_key(cand) != agent._ephemeral_path_key(resolved):
        return command
    parts[i] = str(resolved.resolve())
    if os.name == "nt":
        return call_prefix + subprocess.list2cmdline(parts)
    return call_prefix + shlex.join(parts)


def ensure_absolute_script_for_shell_cwd(agent: Any, command: str) -> str:
    invoked = parse_shell_invoked_script_path(agent, command)
    if invoked is None or not invoked.is_file():
        return command
    try:
        invoked.resolve().relative_to(agent.workspace_config_dir.resolve())
    except ValueError:
        return command
    new_cmd = rewrite_shell_command_script_arg_to_abs(agent, command, invoked.resolve())
    if new_cmd != command:
        print("ℹ️ Shell cwd is the work directory; workspace script path has been expanded to an absolute path.")
    return new_cmd


def _workspace_rg_executable_path(agent: Any) -> Optional[Path]:
    roots: List[Path] = []
    raw_repo_root = getattr(agent, "_self_repo_root", None)
    if raw_repo_root:
        try:
            roots.append(Path(str(raw_repo_root)).expanduser().resolve())
        except Exception:
            pass
    try:
        roots.append(Path(__file__).resolve().parents[2])
    except Exception:
        pass

    dedup: List[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).casefold() if os.name == "nt" else str(root)
        if key in seen:
            continue
        seen.add(key)
        dedup.append(root)

    names = ("rg.exe", "rg.cmd", "rg.bat", "rg") if os.name == "nt" else ("rg",)
    for root in dedup:
        bin_dir = root / "bin"
        for name in names:
            candidate = bin_dir / name
            try:
                if not candidate.is_file():
                    continue
                if os.name != "nt" and not os.access(str(candidate), os.X_OK):
                    continue
                return candidate.resolve()
            except Exception:
                continue
    return None


def _rewrite_shell_command_head_executable(
    command: str,
    *,
    target_exe_bases: set[str],
    replacement: str,
) -> str:
    import subprocess

    s = str(command or "").strip()
    if not s:
        return command
    call_prefix = ""
    if s.lower().startswith("call "):
        call_prefix = "call "
        s = s[5:].strip()
    parts = _split_shell_like(s)
    if not parts:
        return command
    if len(parts) == 1:
        unwrapped_single = _strip_wrapping_quotes(parts[0])
        if unwrapped_single and unwrapped_single != parts[0]:
            reparsed = _split_shell_like(unwrapped_single)
            if len(reparsed) > 1:
                s = unwrapped_single
                parts = reparsed
    base0 = _token_exe_base(_strip_wrapping_quotes(parts[0]))

    if base0 in ("powershell", "pwsh"):
        payload = _find_option_value(parts, ("-command", "-c", "/c"))
        if payload is not None:
            inner_re = _rewrite_shell_command_head_executable(
                payload,
                target_exe_bases=target_exe_bases,
                replacement=replacement,
            )
            if inner_re != payload:
                new_parts = list(parts)
                for i in range(1, len(new_parts) - 1):
                    if new_parts[i].lower() in ("-command", "-c", "/c"):
                        new_parts[i + 1] = inner_re
                        break
                if os.name == "nt":
                    return call_prefix + subprocess.list2cmdline(new_parts)
                return call_prefix + shlex.join(new_parts)

    if len(parts) >= 3 and base0 == "cmd" and parts[1].lower() in ("/c", "/k"):
        inner = " ".join(parts[2:])
        inner_re = _rewrite_shell_command_head_executable(
            inner,
            target_exe_bases=target_exe_bases,
            replacement=replacement,
        )
        if inner_re != inner:
            if os.name == "nt":
                return call_prefix + subprocess.list2cmdline([parts[0], parts[1], inner_re])
            return f"{call_prefix}{parts[0]} {parts[1]} {inner_re}"

    if base0 not in target_exe_bases:
        return command
    parts[0] = replacement
    if os.name == "nt":
        return call_prefix + subprocess.list2cmdline(parts)
    return call_prefix + shlex.join(parts)


def enforce_workspace_rg_for_shell_command(agent: Any, command: str) -> str:
    rg_path = _workspace_rg_executable_path(agent)
    if rg_path is None:
        return command
    return _rewrite_shell_command_head_executable(
        command,
        target_exe_bases={"rg"},
        replacement=str(rg_path),
    )


def normalize_shell_command_for_summary(command: str) -> str:
    """Normalize command string for concise tool-call summary display."""
    return _rewrite_shell_command_head_executable(
        command,
        target_exe_bases={"rg"},
        replacement="rg",
    )


def tune_7z_output_for_piped_terminal(command: str, agent: Any = None) -> str:
    if not command.strip():
        return command
    if not re.search(r'(^|[\\/\s"])7z(?:\.exe)?(?=\s|"|$)', command, re.IGNORECASE):
        return command
    tuned = command
    appended: List[str] = []
    lower = command.lower()
    if " -bsp" not in lower:
        tuned += " -bsp1"
        appended.append("-bsp1")
    if " -bb" not in lower:
        tuned += " -bb1"
        appended.append("-bb1")
    if " -bso" not in lower:
        tuned += " -bso1"
        appended.append("-bso1")
    if " -bse" not in lower:
        tuned += " -bse2"
        appended.append("-bse2")
    if appended:
        lang = getattr(agent, "display_language", None) or "en"
        print(translate("info.seven_z_compat_flags", lang, flags=" ".join(appended)))
    return tuned


def parse_shell_invoked_executable(agent: Any, command: str) -> Optional[Path]:
    s = _unwrap_shell_command_layers(command.strip())
    if not s:
        return None
    parts = _split_shell_like(s)
    if not parts:
        return None
    token = _strip_wrapping_quotes(parts[0])
    if not token:
        return None
    p = Path(token)
    if not p.is_absolute():
        if any(sep in token for sep in ("/", "\\")) or token.startswith("."):
            p_wd, p_temp, p_ws = agent._workspace_relative_script_triple(p)
            if p_wd.is_file():
                return p_wd
            if p_temp.is_file():
                return p_temp
            if p_ws.is_file():
                return p_ws
            return p_wd
        return None
    try:
        return p.resolve()
    except OSError:
        return p


def is_dependency_install_command(command: str) -> bool:
    s = (command or "").strip().lower()
    if not s:
        return False
    install_patterns = [
        r"^(python(\d+(\.\d+)*)?\s+-m\s+pip)\s+install\b",
        r"^(pip(\d+(\.\d+)*)?)\s+install\b",
        r"^uv\s+pip\s+install\b",
        r"^poetry\s+add\b",
        r"^pipenv\s+install\b",
        r"^conda\s+install\b",
        r"^mamba\s+install\b",
        r"^npm\s+install\b",
        r"^pnpm\s+add\b",
        r"^yarn\s+add\b",
        r"^bun\s+add\b",
    ]
    return any(re.match(pat, s) for pat in install_patterns)


def is_ai_workspace_script_command(agent: Any, command: str) -> bool:
    invoked = parse_shell_invoked_script_path(agent, command or "")
    if invoked is None:
        return False
    return agent._is_path_under(invoked, agent.workspace_config_dir)


def try_remove_ephemeral_script_after_shell(agent: Any, command: str) -> Optional[str]:
    invoked = parse_shell_invoked_script_path(agent, command)
    if invoked is None:
        return None
    key = agent._ephemeral_path_key(invoked)
    if key not in agent._ephemeral_script_paths:
        return None
    lang = getattr(agent, "display_language", None) or "en"
    try:
        if invoked.is_file():
            name = invoked.name
            invoked.unlink()
            agent._ephemeral_script_paths.discard(key)
            agent._ai_created_path_keys.discard(key)
            print(translate("info.temp_script_auto_deleted", lang, name=name))
            return name
    except OSError as e:
        print(translate("warning.temp_script_delete_failed", lang, path=invoked, error=e))
    return None


def resolve_model_context_file_env(agent: Any, command: str) -> Optional[str]:
    invoked = parse_shell_invoked_script_path(agent, command or "")
    if invoked is None:
        return None
    try:
        ip = invoked.resolve()
    except OSError:
        ip = Path(invoked)
    best_len = -1
    best_env: Optional[str] = None
    for s in agent.skills or []:
        env = getattr(s, "model_context_file_env", None)
        if not env:
            continue
        try:
            root = Path(s.bundle_root).resolve()
            ip.relative_to(root)
        except (ValueError, OSError):
            continue
        ln = len(str(root))
        if ln > best_len:
            best_len = ln
            best_env = env
    return best_env


def append_shell_merge_output_path(stdout_text: str, return_code: int, merge_path: Optional[str]) -> str:
    if return_code != 0 or not merge_path:
        return stdout_text
    path = Path(merge_path)
    if not path.is_file():
        return stdout_text
    marker = "[Additional output (shell merge file)]"
    if marker in (stdout_text or ""):
        return stdout_text
    try:
        extra = path.read_text(encoding="utf-8")
    except OSError:
        return stdout_text
    if not extra.strip():
        return stdout_text
    head = (stdout_text or "").strip()
    if not head:
        return marker + "\n" + extra
    return head + "\n\n---\n" + marker + "\n" + extra


# ---------------------------------------------------------------------------
# File-deletion detection helpers
# ---------------------------------------------------------------------------

# Patterns that indicate a command may delete files.
# Group 1 captures the command keyword; group 2 captures the rest.
_DELETE_CMD_PATTERNS = [
    # Unix / Linux / macOS
    re.compile(r"(?<!\S)(rm)(?:\s+(-\S+(?:\s+-\S+)*)\s+)?(.+)", re.I),
    re.compile(r"(?<!\S)(unlink)(?:\s+)(.+)", re.I),
    # Windows CMD
    re.compile(r"(?<!\S)(del)(?:\s+(/\S+(?:\s+/\S+)*)\s+)?(.+)", re.I),
    re.compile(r"(?<!\S)(erase)(?:\s+(/\S+(?:\s+/\S+)*)\s+)?(.+)", re.I),
    # Windows / cross-platform PowerShell cmdlets
    re.compile(r"(?<!\S)(Remove-Item)(?:\s+(-\S+(?:\s+-\S+)*)\s+)?(.+)", re.I),
    # rmdir / rd (delete directory) — capture paths in case they point at files
    re.compile(r"(?<!\S)(rmdir|rd)(?:\s+(/\S+(?:\s+/\S+)*)\s+)?(.+)", re.I),
]


def _is_potential_delete_command(command: str) -> bool:
    """Quick check whether *command* may delete files so we can avoid the
    overhead of path extraction when the command is purely informational."""
    if not command:
        return False
    lowered = command.strip().lower()
    for keyword in ("rm ", "rm\t", "del ", "del\t", "erase ", "erase\t",
                    "remove-item ", "remove-item\t", "unlink ", "unlink\t",
                    "rmdir ", "rd "):
        if keyword in lowered:
            return True
    # Also match PowerShell encoded commands that may contain Remove-Item
    if "remove-item" in lowered:
        return True
    return False


def _extract_delete_file_paths(command: str, cwd: Path) -> List[Path]:
    """Parse *command* for file paths that are likely deletion targets.

    Handles plain shell commands and PowerShell ``-Command`` wrappers.
    Resolves relative paths against *cwd* and returns only paths that
    currently exist as regular files.
    """
    if not command:
        return []
    cwd = Path(cwd).resolve()
    paths: List[Path] = []
    cmd_stripped = command.strip()

    # If the command is wrapped in powershell -Command "...", unwrap one layer
    # so we can inspect the payload for Remove-Item / rm calls.
    ps_match = _WIN_POWERSHELL_COMMAND_RE.match(cmd_stripped)
    if ps_match:
        payload_raw = ps_match.group("payload").strip()
        payload, _ = _strip_powershell_payload_quotes(payload_raw)
        if payload:
            cmd_stripped = payload

    # Try each delete-command pattern against both the original command and
    # any unwrapped PowerShell payload.
    candidates = [cmd_stripped]
    if cmd_stripped != command.strip():
        candidates.append(command.strip())

    for candidate in candidates:
        for pattern in _DELETE_CMD_PATTERNS:
            m = pattern.search(candidate)
            if not m:
                continue
            rest = m.group(3) or ""
            # Split the rest by whitespace to get individual path tokens
            tokens = shlex.split(rest) if rest else []
            for token in tokens:
                # Skip flags/options
                if token.startswith("-") or token.startswith("/"):
                    continue
                # Strip surrounding quotes
                token = token.strip().strip("'\"")
                if not token or token in (".", ".."):
                    continue
                # Resolve path
                p = Path(token)
                if not p.is_absolute():
                    p = cwd / p
                try:
                    p = p.resolve()
                except OSError:
                    continue
                # Support glob expansion (e.g. rm *.log)
                if "*" in token or "?" in token:
                    parent = p.parent if not token.startswith("*") else cwd
                    glob_pattern = p.name if not token.startswith("*") else token
                    try:
                        for matched in parent.glob(glob_pattern):
                            if matched.is_file() and matched not in paths:
                                paths.append(matched)
                    except Exception:
                        pass
                elif p.is_file() and p not in paths:
                    paths.append(p)

    # Filter to only files within the workspace (or at least under cwd)
    workspace_paths = []
    for p in paths:
        try:
            p.relative_to(cwd)
        except ValueError:
            continue
        workspace_paths.append(p)

    return workspace_paths


def _snapshot_files_content(paths: List[Path]) -> Dict[str, str]:
    """Read the contents of *paths* into a dict mapping str(path) -> content."""
    snapshots: Dict[str, str] = {}
    for p in paths:
        try:
            if p.is_file():
                snapshots[str(p)] = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return snapshots


def _backup_deleted_file(
    content: str,
    original_path: Path,
    backups_dir: Path,
) -> Optional[str]:
    """Write *content* to a backup file under *backups_dir*.

    Returns the short backup filename (without the directory prefix) on
    success, or ``None`` on failure.  The filename includes a timestamp
    and random suffix to avoid collisions.
    """
    try:
        backups_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = secrets.token_hex(4)
        name = original_path.name
        backup_name = f"{name}_{ts}_{suffix}.bak"
        backup_path = backups_dir / backup_name
        tmp = backup_path.with_suffix(backup_path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
        tmp.replace(backup_path)
        return backup_name
    except Exception:
        return None


# ---------------------------------------------------------------------------

from .base import BaseTool  # noqa: E402
from ..core.security.git_guard import guard_git_clone_precheck  # noqa: E402


class ShellTool(BaseTool):
    name = "shell"
    description = "Run a shell command."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "force": {"type": "boolean"},
        },
        "required": ["command"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        shell_cmd = params.get("command")
        if not shell_cmd:
            return {"success": False, "error": "missing command"}

        lowered_shell = str(shell_cmd).lower()
        if agent._mcp_pending_user_input:
            promptish = (
                ("token" in lowered_shell)
                or ("auth" in lowered_shell)
                or ("credential" in lowered_shell)
                or ("set /p" in lowered_shell)
            )
            mcpish = ("mcp" in lowered_shell) or ("figma" in lowered_shell)
            echoish = ("echo " in lowered_shell) or ("set /p" in lowered_shell)
            if promptish and mcpish and echoish:
                waiting = ", ".join(sorted(agent._mcp_pending_user_input.keys()))
                return {
                    "success": False,
                    "retryable": False,
                    "blocked_by_guard": True,
                    "needs_user_input": True,
                    "input_type": "token",
                    "error": (
                        f"detected repeated token prompt loop for server={waiting}; "
                        "wait for fresh user token then retry /mcp reconnect"
                    ),
                }

        if " mcp start" in lowered_shell or ("helper.exe" in lowered_shell and " mcp " in lowered_shell):
            return {
                "success": False,
                "error": (
                    "manual MCP server start via shell is blocked; "
                    "use MCP tools directly (mcp__server__tool)"
                ),
            }

        shell_force = bool(params.get("force", False))
        shell_interactive = bool(params.get("interactive", False))
        clone_guard = guard_git_clone_precheck(agent.work_directory, str(shell_cmd), shell_force)
        if isinstance(clone_guard, dict):
            return clone_guard

        if not shell_force:
            for item in reversed(agent.operation_results[-6:]):
                prev_cmd = item.get("command") or {}
                prev_res = item.get("result") or {}
                if prev_cmd.get("action") != "shell":
                    continue
                prev_params = prev_cmd.get("params") or {}
                if str(prev_params.get("command", "")).strip() == str(shell_cmd).strip():
                    if prev_res.get("success", False):
                        msg = "duplicate shell command skipped; set force=true to rerun"
                        return {
                            "success": True,
                            "message": msg,
                            "skipped_duplicate": True,
                            "interactive": shell_interactive,
                            "output": "",
                            "stderr": "",
                            "return_code": 0,
                        }
                    break

        shell_cmd_dict = {
            "action": "shell",
            "params": {
                "command": shell_cmd,
                "interactive": shell_interactive,
                "force": shell_force,
                "input": params.get("input") if isinstance(params.get("input"), str) else None,
            },
        }
        confirmed = agent._freedom_auto_confirm(shell_cmd_dict)
        return agent.action_shell_command(
            shell_cmd,
            confirmed=confirmed,
            interactive=shell_interactive,
            input_data=None,
        )
