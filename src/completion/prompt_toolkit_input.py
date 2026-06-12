#!/usr/bin/env python3
"""
Cross-platform input handling module built on prompt_toolkit.
Provides stable Tab completion, status bar rendering, and CJK input support.
"""

import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config.app_info import get_app_runtime_attr_name

_WIN_DRIVE_BANG = re.compile(r"^([A-Za-z]:)(/.*)?$")
_ANSI_SGR_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
MULTILINE_INDENT = "  "
# Cap the input area to behave like a GUI text box: it grows with the content
# up to this many visible rows, then stops growing and scrolls internally.
MAX_INPUT_VISIBLE_ROWS = 8
# Keep at least this many rows above the prompt symbol so the box never gets
# pushed flush against the top edge of a short terminal window.
INPUT_TOP_MARGIN_ROWS = 2
VK_SHIFT = 0x10
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
SHELL_MODE_PROMPT = "! "
SHELL_MODE_LABEL = "Shell mode"
SHELL_MODE_COLOR_HEX = "#e74856"
SHELL_MODE_COLOR_RGB = (231, 72, 86)
SHELL_MODE_RIGHT_PADDING = 2
SHIFT_ENTER_KEY_ALIASES: Tuple[Tuple[str, ...], ...] = (
    # xterm CSI-u: Shift+Enter -> ESC [ 13 ; 2 u
    ("escape", "[", "1", "3", ";", "2", "u"),
    # modifyOtherKeys variant seen in some terminals.
    ("escape", "[", "2", "7", ";", "2", ";", "1", "3", "~"),
    # Some terminal integrations map Shift+Enter to Esc+Enter.
    ("escape", "enter"),
    # tmux/terminal custom mappings may emit SS3 Enter (Esc O M).
    ("escape", "O", "M"),
)
_RESIZE_ATTR_DRAFT = get_app_runtime_attr_name("resize_draft", leading_underscore=True)
_RESIZE_ATTR_CURSOR = get_app_runtime_attr_name("resize_cursor_position", leading_underscore=True)
_RESIZE_ATTR_INTERRUPTED = get_app_runtime_attr_name("resize_interrupted", leading_underscore=True)
_SHELL_MODE_SYNC_HANDLER_ATTR = get_app_runtime_attr_name(
    "shell_mode_sync_handler", leading_underscore=True
)

from .builtin_slash_commands import (
    SLASH_BUILTIN_DISPLAY_OVERRIDES,
    slash_builtin_completions,
)
from ..config.i18n import DEFAULT_DISPLAY_LANGUAGE, language_display_name, normalize_display_language, translate
from ..core.console_utils import _ansi_gray, _ansi_rgb

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import (
        Completer,
        Completion,
        CompleteEvent,
        get_common_complete_suffix,
    )
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.styles import Style
    try:
        from prompt_toolkit.cursor_shapes import CursorShape
        from prompt_toolkit.cursor_shapes import SimpleCursorShapeConfig
    except Exception:
        CursorShape = None  # type: ignore[assignment]
        SimpleCursorShapeConfig = None  # type: ignore[assignment]
    PROMPT_TOOLKIT_AVAILABLE = True
except ImportError:
    PROMPT_TOOLKIT_AVAILABLE = False


def _install_shift_enter_ansi_aliases() -> None:
    """
    Make Shift+Enter / Ctrl+Enter insert a newline instead of submitting.

    On a terminal there is no way to tell ``Shift+Enter`` apart from a plain
    ``Enter`` unless the terminal is asked to report "other" keys (xterm
    modifyOtherKeys, or the kitty keyboard protocol). When that reporting is
    enabled, those key combos arrive as dedicated escape sequences. prompt_toolkit
    ships mappings for the modifyOtherKeys variants but collapses them back to
    ``ControlM`` (i.e. plain Enter), so they would still submit.

    Here we re-point those sequences (and the kitty CSI-u equivalents) to
    ``ControlJ`` which our key bindings treat as "insert newline". This is a
    no-op when prompt_toolkit is unavailable and is safe on Windows because the
    win32 input backend does not consult this table.
    """
    if not PROMPT_TOOLKIT_AVAILABLE:
        return
    try:
        from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
        from prompt_toolkit.keys import Keys as _PTKKeys
    except Exception:
        return
    newline_key = getattr(_PTKKeys, "ControlJ", None)
    if newline_key is None:
        return
    # 13 == CR (the Enter key), 10 == LF (Ctrl+J). The middle field is the
    # xterm modifier code: 2=Shift, 5=Ctrl, 6=Ctrl+Shift.
    sequences = (
        "\x1b[27;2;13~",  # modifyOtherKeys: Shift+Enter
        "\x1b[27;5;13~",  # modifyOtherKeys: Ctrl+Enter
        "\x1b[27;6;13~",  # modifyOtherKeys: Ctrl+Shift+Enter
        "\x1b[27;5;10~",  # modifyOtherKeys: Ctrl+J (keep newline working)
        "\x1b[13;2u",     # kitty/CSI-u: Shift+Enter
        "\x1b[13;5u",     # kitty/CSI-u: Ctrl+Enter
        "\x1b[13;6u",     # kitty/CSI-u: Ctrl+Shift+Enter
    )
    for seq in sequences:
        try:
            ANSI_SEQUENCES[seq] = newline_key
        except Exception:
            pass


_install_shift_enter_ansi_aliases()


def _is_vscode_terminal() -> bool:
    return str(os.environ.get("TERM_PROGRAM", "") or "").strip().lower() == "vscode"


# --- Diagnostic logging for status-overlay positioning ---------------------
# Enabled by setting codewood_OVERLAY_DEBUG=1 in the environment. When off
# this is a no-op so there is zero runtime cost on the rendering hot path.
# When on, every before/after_render fires one line into
# ~/.codewood/logs/prompt_overlay.log so we can reconstruct the real
# cursor_row / line_count / target_row sequence around a misrender without
# guessing from screenshots.
_OVERLAY_DEBUG_ENABLED: Optional[bool] = None
_OVERLAY_DEBUG_LOG_PATH: Optional[Path] = None
_OVERLAY_DEBUG_FRAME = [0]


def _overlay_debug_enabled() -> bool:
    global _OVERLAY_DEBUG_ENABLED
    if _OVERLAY_DEBUG_ENABLED is None:
        raw = str(os.environ.get("codewood_OVERLAY_DEBUG", "") or "").strip().lower()
        _OVERLAY_DEBUG_ENABLED = raw in ("1", "true", "yes", "on")
    return bool(_OVERLAY_DEBUG_ENABLED)


def _overlay_debug_path() -> Optional[Path]:
    global _OVERLAY_DEBUG_LOG_PATH
    if _OVERLAY_DEBUG_LOG_PATH is None:
        try:
            home = Path(os.path.expanduser("~"))
            log_dir = home / ".codewood" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            _OVERLAY_DEBUG_LOG_PATH = log_dir / "prompt_overlay.log"
        except Exception:
            _OVERLAY_DEBUG_LOG_PATH = None
    return _OVERLAY_DEBUG_LOG_PATH


def _overlay_debug_log(phase: str, **fields: Any) -> None:
    if not _overlay_debug_enabled():
        return
    path = _overlay_debug_path()
    if path is None:
        return
    try:
        import time as _time
        _OVERLAY_DEBUG_FRAME[0] += 1
        ts = _time.strftime("%H:%M:%S")
        parts = [f"frame={_OVERLAY_DEBUG_FRAME[0]}", f"ts={ts}", f"phase={phase}"]
        for k, v in fields.items():
            try:
                parts.append(f"{k}={v}")
            except Exception:
                parts.append(f"{k}=<unrepr>")
        line = " ".join(parts) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def _get_output_columns_from_obj(output: Any, default: int = 0) -> int:
    try:
        if output is not None and hasattr(output, "get_size"):
            size = output.get_size()
            cols = int(getattr(size, "columns", 0) or 0)
            if cols > 0:
                return cols
    except Exception:
        pass
    return int(default or 0)


def _get_output_rows_from_obj(output: Any, default: int = 0) -> int:
    try:
        if output is not None and hasattr(output, "get_size"):
            size = output.get_size()
            rows = int(getattr(size, "rows", 0) or 0)
            if rows > 0:
                return rows
    except Exception:
        pass
    return int(default or 0)


def _get_system_terminal_rows(default: int = 0) -> int:
    for stream in (getattr(sys, "__stdout__", None), getattr(sys, "stdout", None)):
        try:
            if stream is None or not hasattr(stream, "fileno"):
                continue
            rows = int(os.get_terminal_size(stream.fileno()).lines or 0)
            if rows > 0:
                return rows
        except Exception:
            continue
    return int(default or 0)


def _get_system_terminal_columns(default: int = 0) -> int:
    # Prefer probing real stdout streams to avoid stale width snapshots from
    # prompt_toolkit output wrappers during rapid terminal resize.
    for stream in (getattr(sys, "__stdout__", None), getattr(sys, "stdout", None)):
        try:
            if stream is None or not hasattr(stream, "fileno"):
                continue
            cols = int(os.get_terminal_size(stream.fileno()).columns or 0)
            if cols > 0:
                return cols
        except Exception:
            continue
    return int(default or 0)


def _wrapped_visual_row_count(text: str, columns: int) -> int:
    """How many visual rows a single logical line occupies in the terminal.

    The continuation prompt (`MULTILINE_INDENT`) is rendered on every wrapped
    row except the first, so the usable width for a wrapped row is
    `cols - len(MULTILINE_INDENT)`. We use that as the per-row capacity for
    *every* row of the logical line — that matches prompt_toolkit's own
    behaviour when ``prompt_continuation`` is configured, and matches what
    the terminal actually paints.
    """
    try:
        cols = int(columns or 0)
    except Exception:
        cols = 0
    if cols <= 0:
        return 1
    continuation_width = max(0, _display_width(MULTILINE_INDENT))
    content_columns = max(1, cols - continuation_width)
    width = _display_width(str(text or ""))
    if width <= 0:
        return 1
    return max(1, (width + content_columns - 1) // content_columns)


def _wrapped_buffer_cursor_row_and_line_count(
    app: Any, text: str, cursor_position: int
) -> Optional[Tuple[int, int]]:
    """Soft-wrap-aware version of cursor row / line count.

    Returns ``None`` when the terminal width is unknown so the caller can
    fall back to the logical (newline-only) calculation.
    """
    output = getattr(app, "output", None)
    cols = _get_output_columns_from_obj(output, default=0)
    if cols <= 0:
        cols = _get_system_terminal_columns(default=0)
    if cols <= 0:
        return None

    cursor_position = max(0, min(int(cursor_position or 0), len(text)))
    logical_lines = str(text or "").split("\n")
    before_cursor_lines = str(text[:cursor_position] or "").split("\n")

    line_count = sum(_wrapped_visual_row_count(line, cols) for line in logical_lines)
    cursor_row = 0
    for line in before_cursor_lines[:-1]:
        cursor_row += _wrapped_visual_row_count(line, cols)
    cursor_row += _wrapped_visual_row_count(before_cursor_lines[-1], cols) - 1
    return max(0, cursor_row), max(1, line_count)


def _get_buffer_cursor_row_and_line_count(app: Any) -> Tuple[int, int]:
    try:
        buf = getattr(app, "current_buffer", None)
        if buf is None:
            return 0, 1
        text = str(getattr(buf, "text", "") or "")
        cursor_position = int(getattr(buf, "cursor_position", len(text)) or 0)
        # Soft-wrap-aware path: prompt_toolkit's document.cursor_position_row
        # and document.line_count only count newline-separated logical lines,
        # so a long single-line input that the terminal soft-wraps into N
        # visual rows is still reported as 1 row. That causes the status
        # overlay to be painted right under the FIRST visual row of the
        # input instead of the LAST, producing the visible bug where the
        # status bar gets sandwiched in the middle of the input. We need to
        # count actual visual rows here.
        wrapped = _wrapped_buffer_cursor_row_and_line_count(app, text, cursor_position)
        if wrapped is not None:
            return wrapped
        doc = getattr(buf, "document", None)
        if doc is not None:
            try:
                cursor_row = int(getattr(doc, "cursor_position_row", 0) or 0)
                line_count_attr = getattr(doc, "line_count", None)
                line_count = (
                    int(line_count_attr())
                    if callable(line_count_attr)
                    else int(line_count_attr or 0)
                )
                if line_count > 0:
                    return max(0, cursor_row), max(1, line_count)
            except Exception:
                pass
        cursor_position = max(0, min(cursor_position, len(text)))
        cursor_row = text[:cursor_position].count("\n")
        line_count = text.count("\n") + 1
        return max(0, cursor_row), max(1, line_count)
    except Exception:
        return 0, 1


def _get_buffer_rows_below_cursor(app: Any) -> int:
    try:
        cursor_row, line_count = _get_buffer_cursor_row_and_line_count(app)
        return max(0, line_count - cursor_row - 1)
    except Exception:
        return 0


def _get_status_overlay_position(
    app: Any,
    window: Any = None,
    max_visible: int = 0,
) -> Tuple[int, int, int]:
    cursor_row, line_count = _get_buffer_cursor_row_and_line_count(app)
    try:
        max_visible = int(max_visible or 0)
    except Exception:
        max_visible = 0
    if max_visible > 0 and line_count > max_visible:
        # The input box is capped to a fixed height and scrolls internally.
        # Anchor the status line 2 rows below the *visible* bottom edge of the
        # box instead of below the (now off-screen) last logical line. We read
        # the renderer's window info to find where the cursor actually sits
        # inside the scrolled box.
        window_height = max_visible
        cursor_visible_row = max_visible - 1
        info = getattr(window, "render_info", None) if window is not None else None
        if info is not None:
            try:
                window_height = max(1, min(int(info.window_height), max_visible))
            except Exception:
                window_height = max_visible
            try:
                cursor_visible_row = int(info.cursor_position.y)
            except Exception:
                cursor_visible_row = window_height - 1
        cursor_visible_row = max(0, min(cursor_visible_row, window_height - 1))
        target_row = (window_height - 1) + 2
        rows_down = max(0, target_row - cursor_visible_row)
        return rows_down, target_row, cursor_visible_row
    target_row = max(0, line_count - 1) + 2
    rows_down = max(0, target_row - cursor_row)
    return rows_down, target_row, cursor_row


def _trace_writes(output: Any, label: str) -> None:
    """Wrap output.write AND output.write_raw to log every emitted byte.

    Only active when codewood_OVERLAY_DEBUG is set. The first time we see a
    given output object we monkey-patch both ``write`` and ``write_raw``;
    subsequent calls are no-ops.

    Why both methods: prompt_toolkit's Win32Output.write_raw is a thin
    wrapper that just calls write(); the main rendering pipeline goes
    through write() directly. Wrapping only write_raw misses everything
    ptk emits during its own render pass, including the cursor-motion
    sequences that move the cursor from the previous frame's resting
    position to the new one across a soft-wrap boundary — which is
    exactly what we need to see to diagnose ghost overlays.
    """
    if not _overlay_debug_enabled():
        return
    if output is None:
        return
    if getattr(output, "_codewood_traced", False):
        return

    def _wrap(method_name: str) -> None:
        original = getattr(output, method_name, None)
        if not callable(original):
            return

        def _traced(text: str, _original=original, _name=method_name) -> None:
            try:
                preview = (
                    text
                    .replace("\x1b", "\\x1b")
                    .replace("\r", "\\r")
                    .replace("\n", "\\n")
                )
                if len(preview) > 120:
                    preview = preview[:120] + "...(truncated)"
                _overlay_debug_log(
                    "raw_write", from_=label, method=_name, payload=preview
                )
            except Exception:
                pass
            _original(text)

        try:
            setattr(output, method_name, _traced)
        except Exception:
            pass

    _wrap("write_raw")
    _wrap("write")
    try:
        output._codewood_traced = True  # type: ignore[attr-defined]
    except Exception:
        pass


def _write_overlay_line(
    output: Any,
    text: str,
    row_delta: int,
    cursor_x: Optional[int] = None,
    term_cols: Optional[int] = None,
) -> None:
    """Paint (or clear) an overlay row ``row_delta`` rows away from the cursor.

    When ``cursor_x`` is provided we restore the cursor with explicit relative
    moves + CHA (cursor horizontal absolute, ``\\x1b[<col>G``) instead of the
    DECSC/DECRC pair (``\\x1b7`` / ``\\x1b8``). DECSC/DECRC has a single save
    slot at the terminal level; prompt_toolkit and Windows Terminal have been
    observed to clobber it under sustained input.

    For the "clear" branch we ALSO overwrite the row with literal spaces
    instead of relying solely on ``\\x1b[2K`` (EL2). Real-world runs on
    Windows Terminal have shown ``\\x1b[2K`` failing to erase a previously
    drawn overlay even when the cursor is verified to be on that row — the
    space overwrite is a belt-and-suspenders fallback that ALWAYS visibly
    overwrites the row.

    The legacy DECSC/DECRC path is kept as a fallback so callers that do not
    yet know the cursor column (e.g. external prints in chat history) still
    work the way they used to.
    """
    row_delta = int(row_delta or 0)
    _overlay_debug_log(
        "_write_overlay_line",
        row_delta=row_delta,
        text_len=len(str(text or "")),
        text_kind=("draw" if text else "clear"),
        cursor_x=("none" if cursor_x is None else int(cursor_x)),
        term_cols=("none" if term_cols is None else int(term_cols)),
    )
    if cursor_x is not None:
        # First: bracket the entire overlay write with DECRST/DECSET DECAWM
        # (\x1b[?7l … \x1b[?7h). Disabling autowrap on entry has the side
        # effect of unconditionally clearing the "delayed EOL wrap" / LCF
        # flag. Without this, prompt_toolkit can leave the cursor in
        # pending-wrap state at the right margin, and our subsequent LF
        # and CUU moves land one physical row off from where we expect.
        # Re-enabling autowrap before returning leaves the terminal in
        # the same mode prompt_toolkit and the user expect for normal
        # input — autowrap stays on, but with LCF reset to 0.
        output.write_raw("\x1b[?7l")
        # Move to the overlay row.
        #
        # IMPORTANT: We use LF ("\n") to move DOWN rather than CSI CUD
        # ("\x1b[<n>B"). After prompt_toolkit's render the cursor may be
        # parked at the right edge in "pending wrap" / DECAWM-armed state.
        # Windows Terminal (and several other VT emulators) handle CUD
        # inconsistently in that state, sometimes treating the cursor as
        # if it is already on the next row and skipping a line — which is
        # exactly the failure mode behind the persistent "two status bars
        # stacked" symptom observed in real sessions even though our
        # \x1b[2K and space-fill erase did fire. LF is the most primitive
        # vertical-move primitive and is uniformly defined to clear any
        # pending wrap before advancing one line, so use it instead.
        # Moving UP cannot trigger wrap, so CUU ("\x1b[<n>A") is still
        # fine for the return move.
        if row_delta > 0:
            # Emit LFs (which can scroll) but the caller's pre-render
            # checks ensured we have enough headroom below the cursor for
            # the overlay row, so this never actually scrolls in practice.
            output.write_raw("\n" * row_delta)
            output.write_raw("\r")
        elif row_delta < 0:
            output.write_raw(f"\x1b[{abs(row_delta)}A\r")
        else:
            output.write_raw("\r")
        # Belt-and-suspenders erase: \x1b[2K (EL2) plus an explicit row of
        # spaces. Real Windows Terminal sessions have shown \x1b[2K being
        # silently dropped after sustained prompt_toolkit activity; the
        # space overwrite guarantees the cell contents are replaced.
        output.write_raw("\x1b[2K")
        if term_cols and term_cols > 0:
            # Write spaces across the row and \r back to col 0. Cap at
            # term_cols - 1 so we never push the cursor off the edge and
            # accidentally trigger a wrap on the next IND/LF.
            blank_width = max(0, int(term_cols) - 1)
            if blank_width > 0:
                output.write_raw(" " * blank_width)
                output.write_raw("\r")
        if text:
            output.write_raw(text)
        # Return to the row prompt_toolkit left us on. CUU is safe here
        # because moving up doesn't interact with pending-wrap state.
        if row_delta > 0:
            output.write_raw(f"\x1b[{row_delta}A")
        elif row_delta < 0:
            output.write_raw(f"\x1b[{abs(row_delta)}B")
        # CHA is 1-based: column 0 maps to col 1.
        target_col = max(0, int(cursor_x)) + 1
        output.write_raw(f"\x1b[{target_col}G")
        # Re-enable autowrap so the next ptk render frame and any user
        # input continue to wrap normally at the right margin. The
        # \x1b[?7h sets DECAWM back on; LCF stays 0 until the next
        # character is written at the right margin.
        output.write_raw("\x1b[?7h")
        return
    # Legacy DECSC/DECRC fallback (no cursor_x supplied).
    output.write_raw("\x1b7")
    if row_delta > 0:
        output.write_raw(f"\x1b[{row_delta}B\r")
    elif row_delta < 0:
        output.write_raw(f"\x1b[{abs(row_delta)}A\r")
    else:
        output.write_raw("\r")
    output.write_raw("\x1b[2K")
    if text:
        output.write_raw(text)
    output.write_raw("\x1b8")


def _clear_two_rows(
    output: Any,
    row_delta: int,
    cursor_x: Optional[int] = None,
    term_cols: Optional[int] = None,
) -> None:
    """Wipe both ``row_delta`` AND ``row_delta + 1`` rows away from the cursor.

    This is a defensive belt-and-suspenders erase intended to mask a
    ptk-model-vs-physical cursor row drift that we have observed under
    Windows Terminal during soft-wrap transitions: when the buffer adds a
    new visual row through autowrap, prompt_toolkit's internal cursor row
    can advance one line ahead of where the physical cursor actually is,
    so our relative ``row_delta`` lands one row short of the real stale
    overlay. We don't yet have a way to detect that drift cheaply, but we
    DO know the stale overlay is always on row N or N+1 (never further
    away — it's adjacent to where it was painted last frame), so we just
    wipe both rows. Wiping a blank row is harmless; not wiping the right
    row leaves a ghost overlay on screen.

    The double-clear only applies to the CHA path (``cursor_x is not
    None``). The legacy DECSC/DECRC path is taken by callers that do not
    interact with prompt_toolkit's render hooks (e.g. external chat-history
    prints), where the row drift bug does not occur. For those callers we
    fall back to a single clear so we don't disturb adjacent screen rows.
    """
    _write_overlay_line(
        output,
        "",
        row_delta,
        cursor_x=cursor_x,
        term_cols=term_cols,
    )
    if cursor_x is not None:
        _write_overlay_line(
            output,
            "",
            row_delta + 1,
            cursor_x=cursor_x,
            term_cols=term_cols,
        )


def _attach_blink_after_render_hook(
    session,
    status_provider: Optional[Callable[[], str]] = None,
    terminal_resize_callback: Optional[Callable[[int, int], bool]] = None,
    max_input_rows_provider: Optional[Callable[[], int]] = None,
    buffer_window_provider: Optional[Callable[[], Any]] = None,
) -> None:
    """
    `Application.after_render` is an `Event` object — handlers must be added via
    `+=`/`add_handler` rather than reassignment, otherwise `Event.fire()` breaks.
    """
    if session is None:
        return
    app = getattr(session, "app", None)
    if app is None:
        return

    # `desired`: Status bar text the current render cycle wants to display (empty string means hidden).
    output = getattr(app, "output", None)
    initial_cols = _get_system_terminal_columns(default=0)
    if initial_cols <= 0:
        initial_cols = _get_output_columns_from_obj(output, default=0)
    state = {
        "visible": False,
        "text": "",
        "desired": "",
        "menu_open": False,
        "last_cols": max(0, initial_cols),
        "rows_down": 2,
        "target_row": 2,
        "cursor_row": 0,
        "pending_rows_down": 2,
        "pending_target_row": 2,
        "pending_cursor_row": 0,
        "prev_line_count": None,
    }

    def _overlay_pos(_app) -> Tuple[int, int, int]:
        win = None
        mv = 0
        try:
            if callable(buffer_window_provider):
                win = buffer_window_provider()
        except Exception:
            win = None
        try:
            if callable(max_input_rows_provider):
                mv = int(max_input_rows_provider() or 0)
        except Exception:
            mv = 0
        return _get_status_overlay_position(_app, window=win, max_visible=mv)

    def _on_before_render(_app) -> None:
        try:
            # Install the write/write_raw tracer as early as possible so we
            # can also capture prompt_toolkit's own render output. Calling
            # this every frame is safe — _trace_writes is idempotent per
            # output object.
            try:
                _trace_writes(getattr(_app, "output", None), "before_render")
            except Exception:
                pass
            prev_menu_open = bool(state.get("menu_open"))
            menu_open = False
            try:
                buf = getattr(_app, "current_buffer", None)
                menu_open = (
                    buf is not None and getattr(buf, "complete_state", None) is not None
                )
            except Exception:
                menu_open = False
            state["menu_open"] = bool(menu_open)
            desired = str(status_provider() or "") if callable(status_provider) else ""
            state["desired"] = desired
            output = getattr(_app, "output", None)
            rows_down, target_row, cursor_row = _overlay_pos(_app)
            state["pending_rows_down"] = rows_down
            state["pending_target_row"] = target_row
            state["pending_cursor_row"] = cursor_row
            # Detect a large jump in the input's visual height (e.g. arrow-key
            # history navigation swapping a 1-line draft for a multi-line one,
            # or pasting several lines at once). Our status line is painted
            # outside prompt_toolkit's screen model, so a big jump can strand
            # the previous status bar inside the freshly drawn content where
            # ptk's incremental diff won't erase it (the "duplicate status bar"
            # artifact). Force a full repaint for this frame: ptk then erases
            # the whole input region before redrawing and the stranded overlay
            # disappears cleanly. Small (+/-1) changes keep the lightweight
            # incremental path to avoid flicker on ordinary typing.
            try:
                _, _line_count = _get_buffer_cursor_row_and_line_count(_app)
                prev_line_count = state.get("prev_line_count")
                if (
                    bool(state.get("visible"))
                    and prev_line_count is not None
                    and abs(int(_line_count) - int(prev_line_count)) >= 2
                ):
                    renderer = getattr(_app, "renderer", None)
                    if renderer is not None:
                        try:
                            renderer._last_screen = None
                        except Exception:
                            pass
                state["prev_line_count"] = int(_line_count)
            except Exception:
                pass
            if _overlay_debug_enabled():
                buf = getattr(_app, "current_buffer", None)
                text = str(getattr(buf, "text", "") or "") if buf is not None else ""
                cpos = int(getattr(buf, "cursor_position", 0) or 0) if buf is not None else 0
                doc = getattr(buf, "document", None) if buf is not None else None
                doc_row = -1
                doc_lc = -1
                if doc is not None:
                    try:
                        doc_row = int(getattr(doc, "cursor_position_row", 0) or 0)
                        lc_attr = getattr(doc, "line_count", None)
                        doc_lc = (
                            int(lc_attr())
                            if callable(lc_attr)
                            else int(lc_attr or 0)
                        )
                    except Exception:
                        pass
                sys_cols = _get_system_terminal_columns(default=0)
                out_cols = _get_output_columns_from_obj(output, default=0)
                try:
                    size = output.get_size() if output is not None and hasattr(output, "get_size") else None
                    total_rows = int(getattr(size, "rows", 0) or 0) if size is not None else -1
                except Exception:
                    total_rows = -1
                try:
                    rows_below = int(output.get_rows_below_cursor_position()) if output is not None and hasattr(output, "get_rows_below_cursor_position") else -1
                except Exception:
                    rows_below = -2
                _overlay_debug_log(
                    "before",
                    text_len=len(text),
                    cpos=cpos,
                    doc_row=doc_row,
                    doc_lc=doc_lc,
                    sys_cols=sys_cols,
                    out_cols=out_cols,
                    total_rows=total_rows,
                    rows_below=rows_below,
                    cursor_row=cursor_row,
                    line_count_from_pos=target_row - 2 + 1,
                    target_row=target_row,
                    rows_down=rows_down,
                    state_visible=bool(state.get("visible")),
                    state_target_row=int(state.get("target_row", -1) or -1),
                    state_cursor_row=int(state.get("cursor_row", -1) or -1),
                    desired_len=len(desired),
                    menu_open=menu_open,
                )
            if menu_open and (not prev_menu_open) and bool(state.get("visible")):
                try:
                    old_target_row = int(state.get("target_row", target_row) or target_row)
                    if (
                        output is not None
                        and hasattr(output, "write_raw")
                        and hasattr(output, "flush")
                    ):
                        cursor_x: Optional[int] = None
                        try:
                            ptk_renderer = getattr(_app, "renderer", None)
                            ptk_cursor_pos = (
                                getattr(ptk_renderer, "_cursor_pos", None)
                                if ptk_renderer is not None
                                else None
                            )
                            if ptk_cursor_pos is not None:
                                cursor_x = int(getattr(ptk_cursor_pos, "x", 0) or 0)
                        except Exception:
                            cursor_x = None
                        term_cols = _get_system_terminal_columns(default=0)
                        if term_cols <= 0:
                            term_cols = _get_output_columns_from_obj(output, default=0)
                        _clear_two_rows(
                            output,
                            old_target_row - cursor_row,
                            cursor_x=cursor_x,
                            term_cols=term_cols,
                        )
                        output.flush()
                except Exception:
                    pass
                state["visible"] = False
                state["text"] = ""
            try:
                cols = _get_system_terminal_columns(default=0)
                if cols <= 0:
                    cols = _get_output_columns_from_obj(output, default=0)
                if cols > 0:
                    prev_cols = int(state.get("last_cols", 0) or 0)
                    if prev_cols > 0 and cols != prev_cols:
                        should_interrupt = False
                        if callable(terminal_resize_callback):
                            try:
                                should_interrupt = bool(terminal_resize_callback(prev_cols, cols))
                            except Exception:
                                should_interrupt = False
                        state["last_cols"] = cols
                        if should_interrupt:
                            # Preserve unsent draft so caller can restore it after
                            # terminal-resize reload.
                            draft_text = ""
                            draft_cursor = 0
                            try:
                                buf = getattr(_app, "current_buffer", None)
                                draft_text = str(getattr(buf, "text", "") or "")
                                draft_cursor = int(getattr(buf, "cursor_position", 0) or 0)
                            except Exception:
                                draft_text = ""
                                draft_cursor = 0
                            try:
                                setattr(session, _RESIZE_ATTR_DRAFT, draft_text)
                                setattr(session, _RESIZE_ATTR_CURSOR, draft_cursor)
                                setattr(session, _RESIZE_ATTR_INTERRUPTED, True)
                            except Exception:
                                pass
                            try:
                                _app.exit(result="")
                            except Exception:
                                pass
                    else:
                        state["last_cols"] = cols
            except Exception:
                pass
        except Exception:
            pass

    def _on_after_render(_app) -> None:
        try:
            output = getattr(_app, "output", None)
            _trace_writes(output, "after_render")
            if output is not None and hasattr(output, "write_raw") and hasattr(output, "flush"):
                try:
                    desired = str(state.get("desired") or "")
                    menu_open = bool(state.get("menu_open"))
                    # Recompute the overlay anchor from the *just-rendered*
                    # window info. For the capped/scrolling input box the
                    # cursor's visible row and the box height are only known
                    # after the render, so this keeps the status line glued to
                    # the box's bottom edge even while the user scrolls through
                    # an overflowing draft with the arrow keys.
                    try:
                        rows_down, target_row, cursor_row = _overlay_pos(_app)
                    except Exception:
                        rows_down = int(
                            state.get(
                                "pending_rows_down",
                                2 + _get_buffer_rows_below_cursor(_app),
                            )
                            or 0
                        )
                        target_row = int(
                            state.get("pending_target_row", rows_down) or rows_down
                        )
                        cursor_row = int(state.get("pending_cursor_row", 0) or 0)
                    old_target_row = int(state.get("target_row", target_row) or target_row)
                    # Use prompt_toolkit's bookkept cursor column as the
                    # restore target. This lets _write_overlay_line use a
                    # DECSC/DECRC-free path (which has been observed to be
                    # unreliable under Windows Terminal + sustained input)
                    # while still landing the cursor exactly where the
                    # next render expects it.
                    cursor_x: Optional[int] = None
                    try:
                        ptk_renderer = getattr(_app, "renderer", None)
                        ptk_cursor_pos = (
                            getattr(ptk_renderer, "_cursor_pos", None)
                            if ptk_renderer is not None
                            else None
                        )
                        if ptk_cursor_pos is not None:
                            cursor_x = int(getattr(ptk_cursor_pos, "x", 0) or 0)
                    except Exception:
                        cursor_x = None
                    # term_cols feeds the "write a full row of spaces" belt-
                    # and-suspenders erase inside _write_overlay_line. Prefer
                    # the OS-level width because prompt_toolkit's snapshot
                    # can lag a resize by one render cycle.
                    term_cols = _get_system_terminal_columns(default=0)
                    if term_cols <= 0:
                        term_cols = _get_output_columns_from_obj(output, default=0)
                    # Real rows available below the resting cursor. Used to
                    # gate the "clear the row below the status bar" pass so
                    # the extra line-feed it emits can never scroll the input
                    # area when the status bar already sits at the bottom of
                    # the screen.
                    rows_below = -1
                    try:
                        if hasattr(output, "get_rows_below_cursor_position"):
                            rows_below = int(output.get_rows_below_cursor_position())
                    except Exception:
                        rows_below = -1
                    if not menu_open:
                        if bool(state.get("visible")):
                            if desired == "":
                                if old_target_row != target_row and old_target_row > max(
                                    0, target_row - 2
                                ):
                                    # Defensive double-clear: wipe both the
                                    # row we *think* had the stale overlay
                                    # AND the row immediately below it.
                                    # Under Windows Terminal we have seen the
                                    # ptk-model cursor row drift one row off
                                    # from the physical cursor (the delayed
                                    # EOL wrap / LCF mismatch), which makes
                                    # our relative row_delta land one row
                                    # short. Clearing both rows guarantees
                                    # the stale overlay disappears whether
                                    # ptk-model was right or off-by-one.
                                    _clear_two_rows(
                                        output,
                                        old_target_row - cursor_row,
                                        cursor_x=cursor_x,
                                        term_cols=term_cols,
                                    )
                                state["visible"] = False
                                state["text"] = ""
                            elif (
                                desired
                                and old_target_row != target_row
                                and old_target_row > max(0, target_row - 2)
                            ):
                                _clear_two_rows(
                                    output,
                                    old_target_row - cursor_row,
                                    cursor_x=cursor_x,
                                    term_cols=term_cols,
                                )
                        # Always redraw when desired text exists: external prints (e.g.
                        # /chat switch history) may scroll previous overlay away, while
                        # state still says "visible".
                        if desired:
                            # Pre-clear the gap row (the empty row between the
                            # input area's last line and the status bar). Real-
                            # world Windows Terminal sessions occasionally
                            # leave a stale status overlay sitting on that gap
                            # row after a soft-wrap transition — the row drift
                            # bug we have tried to fix from several angles
                            # (LF vs CUD, DECAWM bracketing, two-row clear).
                            # Pre-clearing the gap row every frame is cheap
                            # (one extra EL2 + space-fill per render) and
                            # guarantees no ghost overlay survives there.
                            # It does NOT touch the input area or scroll
                            # history, since the gap row is by design always
                            # blank when the input + status bar layout is
                            # correct.
                            if rows_down >= 2 and cursor_x is not None:
                                _write_overlay_line(
                                    output,
                                    "",
                                    rows_down - 1,
                                    cursor_x=cursor_x,
                                    term_cols=term_cols,
                                )
                            _write_overlay_line(
                                output,
                                desired,
                                rows_down,
                                cursor_x=cursor_x,
                                term_cols=term_cols,
                            )
                            # Symmetric belt-and-suspenders: also wipe the row
                            # immediately BELOW the status bar every frame.
                            # When the input shrinks by one visual row (e.g.
                            # the user backspaces a soft-wrapped line away),
                            # the previously drawn status bar ends up exactly
                            # one physical row below where the new one lands.
                            # The transition-frame _clear_two_rows pass above
                            # is supposed to erase it, but under the Windows
                            # Terminal ptk-model/physical row drift that clear
                            # can miss by a row; and once old_target_row ==
                            # target_row again on the next steady frame we
                            # never revisit that row, leaving a stale "second
                            # status bar" stacked under the live one. Clearing
                            # rows_down + 1 unconditionally on every draw wipes
                            # that orphan. Guarded by rows_below so the extra
                            # line-feed can never scroll the input when the bar
                            # already sits at the bottom of the screen (in
                            # which case there is no row below to go stale).
                            if cursor_x is not None and rows_below > rows_down:
                                _write_overlay_line(
                                    output,
                                    "",
                                    rows_down + 1,
                                    cursor_x=cursor_x,
                                    term_cols=term_cols,
                                )
                            state["visible"] = True
                            state["text"] = desired
                        state["rows_down"] = rows_down
                        state["target_row"] = target_row
                        state["cursor_row"] = cursor_row
                    if _overlay_debug_enabled():
                        _overlay_debug_log(
                            "after",
                            cursor_row=cursor_row,
                            target_row=target_row,
                            rows_down=rows_down,
                            old_target_row=old_target_row,
                            desired_len=len(desired),
                            menu_open=menu_open,
                            state_visible=bool(state.get("visible")),
                        )
                except Exception:
                    pass
                if not _is_vscode_terminal():
                    output.write_raw("\x1b[?12h\x1b[?25h")
                output.flush()
                return
        except Exception:
            pass
        try:
            if not _is_vscode_terminal():
                sys.stdout.write("\x1b[?12h\x1b[?25h")
                sys.stdout.flush()
        except Exception:
            pass

    try:
        evt_before = getattr(app, "before_render", None)
        if evt_before is not None and hasattr(evt_before, "add_handler"):
            evt_before.add_handler(_on_before_render)
        evt = getattr(app, "after_render", None)
        if evt is not None and hasattr(evt, "add_handler"):
            evt.add_handler(_on_after_render)
    except Exception:
        pass


def _get_output_columns(session: Any, default: int = 80) -> int:
    """
    Get the current output-window column count from prompt_toolkit (it changes with the window).
    """
    try:
        output = getattr(session, "output", None)
        if output is None:
            app = getattr(session, "app", None)
            output = getattr(app, "output", None) if app is not None else None
        cols = _get_output_columns_from_obj(output, default=0)
        if cols > 0:
            return cols
    except Exception:
        pass
    return _get_system_terminal_columns(default=int(default or 80))


def _sanitize_prompt_pollution(text: str, work_directory: Optional[Path] = None) -> str:
    """
    Best-effort cleanup for rare cases where prompt fragments leak into input.
    Example: 'D:\\tmp\\builds>>>>/exit' -> '/exit'
    """
    s = str(text or "")
    if not s:
        return s
    cleaned = s.replace("\r", "").strip()
    if not cleaned:
        return ""

    wd = str(work_directory or "").strip()
    if wd:
        prompt_prefix = f"{wd}>"
        while cleaned.startswith(prompt_prefix):
            cleaned = cleaned[len(prompt_prefix):].lstrip()
    while cleaned.startswith("›"):
        cleaned = cleaned[1:].lstrip()

    if re.match(r"^>{2,}\s*\S", cleaned):
        cleaned = re.sub(r"^>+\s*", "", cleaned, count=1)

    return cleaned


def _normalize_newlines(text: str) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


def _strip_ansi_sgr(text: str) -> str:
    return _ANSI_SGR_RE.sub("", str(text or ""))


def _display_width(text: str) -> int:
    s = str(text or "")
    if not s:
        return 0
    try:
        from wcwidth import wcswidth  # type: ignore

        w = int(wcswidth(s))
        if w >= 0:
            return w
    except Exception:
        pass
    width = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        east = unicodedata.east_asian_width(ch)
        width += 2 if east in ("W", "F") else 1
    return width


def _truncate_to_display_width(text: str, max_width: int) -> str:
    s = str(text or "")
    cap = max(0, int(max_width or 0))
    if cap <= 0 or not s:
        return ""
    out: List[str] = []
    used = 0
    for ch in s:
        ch_w = _display_width(ch)
        if ch_w <= 0:
            out.append(ch)
            continue
        if used + ch_w > cap:
            break
        out.append(ch)
        used += ch_w
    return "".join(out)


def _shell_mode_effective_right_padding() -> int:
    # On Windows terminals (Windows Terminal / Cursor / VS Code integrated),
    # the right edge often appears with one extra visual cell. Render one fewer
    # space so users see exactly two trailing spaces after "Shell mode".
    if os.name == "nt":
        return max(0, SHELL_MODE_RIGHT_PADDING - 1)
    return SHELL_MODE_RIGHT_PADDING


def _windows_get_async_key_state(vk_code: int) -> int:
    if os.name != "nt":
        return 0
    try:
        import ctypes

        return int(ctypes.windll.user32.GetAsyncKeyState(int(vk_code)))
    except Exception:
        return 0


def _windows_get_key_state(vk_code: int) -> int:
    if os.name != "nt":
        return 0
    try:
        import ctypes

        return int(ctypes.windll.user32.GetKeyState(int(vk_code)))
    except Exception:
        return 0


def _is_windows_shift_pressed() -> bool:
    """
    Detect Shift modifier from native Windows keyboard state.
    This allows distinguishing Shift+Enter even when terminal input bytes
    are the same as plain Enter.
    """
    if os.name != "nt":
        return False
    for vk in (VK_SHIFT, VK_LSHIFT, VK_RSHIFT):
        try:
            async_state = int(_windows_get_async_key_state(vk))
            key_state = int(_windows_get_key_state(vk))
        except Exception:
            async_state = 0
            key_state = 0
        if (async_state & 0x8000) != 0 or (key_state & 0x8000) != 0:
            return True
    return False


class FileCompleter(Completer):
    """File completer."""
    
    def __init__(
        self,
        work_directory: Path,
        workspace_directory: Optional[Path] = None,
        slash_skill_commands: Optional[List[str]] = None,
        slash_mcp_commands: Optional[List[str]] = None,
        slash_dynamic_rules: Optional[List[Dict[str, Any]]] = None,
        shell_mode_provider: Optional[Callable[[], bool]] = None,
    ):
        self.work_directory = work_directory
        self.workspace_directory = workspace_directory or work_directory
        self.slash_skill_commands = slash_skill_commands or []
        self.slash_mcp_commands = slash_mcp_commands or []
        self.slash_dynamic_rules = slash_dynamic_rules or []
        self.shell_mode_provider = shell_mode_provider

    def _is_shell_mode_active(self) -> bool:
        provider = getattr(self, "shell_mode_provider", None)
        if not callable(provider):
            return False
        try:
            return bool(provider())
        except Exception:
            return False

    def _matching_base_directory(self) -> Path:
        base = self.workspace_directory or self.work_directory
        try:
            return Path(base).resolve()
        except Exception:
            return Path(base)

    def _resolve_dynamic_groups(self) -> List[Tuple[str, List[str]]]:
        """
        Unified delayed-dynamic completion groups.
        Declarative source: `slash_dynamic_rules`.
        """
        out: List[Tuple[str, List[str]]] = []

        def _to_groups(raw: Any) -> None:
            if not isinstance(raw, list):
                return
            for item in raw:
                if not (isinstance(item, tuple) and len(item) == 2):
                    continue
                trigger, cands = item
                trig = str(trigger or "")
                if not trig:
                    continue
                if not isinstance(cands, list):
                    continue
                normalized = [str(x) for x in cands if isinstance(x, str)]
                out.append((trig, normalized))

        rules = self.slash_dynamic_rules or []
        if isinstance(rules, list) and rules:
            for rule in rules:
                if not isinstance(rule, dict):
                    continue

                groups_provider = rule.get("groups_provider")
                if callable(groups_provider):
                    try:
                        _to_groups(groups_provider() or [])
                    except Exception:
                        pass
                    continue

                groups_raw = rule.get("groups")
                if isinstance(groups_raw, list):
                    _to_groups(groups_raw)
                    continue

                trigger = str(rule.get("trigger") or "")
                if not trigger:
                    continue
                candidates: List[str] = []
                cands_provider = rule.get("candidates_provider")
                if callable(cands_provider):
                    try:
                        raw = cands_provider() or []
                        if isinstance(raw, list):
                            candidates = [str(x) for x in raw if isinstance(x, str)]
                    except Exception:
                        candidates = []
                else:
                    raw = rule.get("candidates", [])
                    if isinstance(raw, list):
                        candidates = [str(x) for x in raw if isinstance(x, str)]
                out.append((trigger, candidates))
            return out
        return []

    @staticmethod
    def _slash_fragment_for_completion(text: str) -> Tuple[int, str]:
        """
        Return the slash-command fragment being edited at cursor tail.
        Matches either:
        - line starts with '/...' (supports spaces in slash command)
        - or last token after whitespace is '/...'
        Returns (index_of_slash, fragment_from_slash_to_cursor), or (-1, '').
        """
        # Prefer the trailing slash token near cursor.
        # Token may include nested slashes, e.g. "/mcp/windbg/" or "/mcp/windbg/list_xxx".
        # Strict boundary: if there is a previous character, it must be whitespace.
        m = re.search(r"/[^\s]*$", text)
        if m:
            frag = m.group(0) or ""
            if frag.startswith("/"):
                slash_idx = m.start()
                if slash_idx > 0:
                    prev = text[slash_idx - 1]
                    is_boundary = prev.isspace()
                    if is_boundary:
                        return slash_idx, frag
                else:
                    return slash_idx, frag

        # Full-line slash built-in command (e.g. "/mcp ", "/memory search q")
        stripped = text.lstrip()
        if stripped.startswith("/"):
            slash_idx = len(text) - len(stripped)
            return slash_idx, text[slash_idx:]
        return -1, ""

    @staticmethod
    def _bang_fragment_for_completion(text: str) -> Tuple[int, str]:
        """
        Return the '!...' path fragment at cursor tail (workspace-relative path after '!').
        Matches either:
        - line starts with '!...'
        - or last token after whitespace is '!...'
        Returns (index_of_bang, fragment_from_bang_to_cursor), or (-1, '').
        """
        stripped = text.lstrip()
        if stripped.startswith("!"):
            bang_idx = len(text) - len(stripped)
            return bang_idx, text[bang_idx:]
        m = re.search(r"(^|\s)(![^\s]*)$", text)
        if not m:
            return -1, ""
        frag = m.group(2) or ""
        if not frag.startswith("!"):
            return -1, ""
        bang_idx = len(text) - len(frag)
        return bang_idx, frag

    @staticmethod
    def _dynamic_completion_display(
        slash_part: str,
        candidate: str,
        delayed_dynamic_groups: List[Tuple[str, List[str]]],
    ) -> str:
        """
        For delayed dynamic slash completions, display only the incremental part
        (e.g. "/workspace switch code-review" -> "code-review" when user already
        typed "/workspace switch ").
        """
        sp = str(slash_part or "")
        c = str(candidate or "")
        if not sp or not c:
            return c
        sp_l = sp.lower()
        c_l = c.lower()
        for trigger_prefix, _ in delayed_dynamic_groups or []:
            trig = str(trigger_prefix or "")
            if not trig:
                continue
            trig_l = trig.lower()
            if not sp_l.startswith(trig_l):
                continue
            if not c_l.startswith(trig_l):
                continue
            if trig_l == "/language ":
                lang_code = c[len(trig):].strip()
                if lang_code:
                    return language_display_name(lang_code)
            rest = c[len(trig):]
            # For '/mcp/' second-layer completion, display only server name.
            if trig_l == "/mcp/" and rest.endswith("/"):
                rest = rest[:-1]
            # If no visible incremental part, keep full display text.
            return rest if rest else c
        return c

    def get_completions(self, document, complete_event):
        """Get completion options."""
        text = document.text_before_cursor

        # Token-based path completion from the last whitespace boundary.
        # This enables completion while typing commands like: "open src/win"
        # where only the trailing token should be replaced.
        token_start, token = self._extract_last_token(text)

        # Windows: '!' + workspace-relative path -> path completion only
        if os.name == "nt":
            bidx, bang_part = self._bang_fragment_for_completion(text)
            if bidx >= 0 and bang_part:
                path_matches = self._get_workspace_path_completions_for_bang(bang_part)
                if path_matches:
                    spos = -len(bang_part)
                    seen = set()
                    for mc in path_matches:
                        if mc in seen:
                            continue
                        seen.add(mc)
                        yield Completion(
                            mc,
                            start_position=spos,
                            display=self._path_leaf_name(mc),
                        )
                    return

        shell_mode_active = self._is_shell_mode_active()
        if not shell_mode_active:
            # Slash built-ins: always provide command completion when applicable.
            # Special-case lone "/" on non-Windows to avoid enumerating root files.
            idx, slash_part = self._slash_fragment_for_completion(text)
            if idx >= 0 and slash_part:
                delayed_groups = self._resolve_dynamic_groups()
                builtin_matches = slash_builtin_completions(
                    slash_part,
                    dynamic_commands=(
                        self.slash_skill_commands
                        + self.slash_mcp_commands
                    ),
                    delayed_dynamic_groups=delayed_groups,
                )
                if builtin_matches:
                    # When delayed dynamic completion is active, hide the trigger
                    # command itself from menu (e.g. "/workspace switch ").
                    active_trigger_norms = {
                        str(trigger or "").rstrip().lower()
                        for trigger, _ in delayed_groups
                        if str(trigger or "").strip()
                        and slash_part.lower().startswith(str(trigger or "").lower())
                    }
                    if active_trigger_norms:
                        builtin_matches = [
                            mc
                            for mc in builtin_matches
                            if str(mc or "").rstrip().lower() not in active_trigger_norms
                        ]
                    if builtin_matches:
                        spos = -len(slash_part)
                        seen = set()
                        all_delayed_groups = delayed_groups
                        for mc in builtin_matches:
                            if mc in seen:
                                continue
                            seen.add(mc)
                            display_text = self._dynamic_completion_display(
                                slash_part, mc, all_delayed_groups
                            )
                            if display_text == mc:
                                display_text = SLASH_BUILTIN_DISPLAY_OVERRIDES.get(
                                    mc, display_text
                                )
                            yield Completion(
                                mc,
                                start_position=spos,
                                display=display_text,
                            )
                        return
                if slash_part == "/":
                    return
                # Keep Windows behavior: slash prefix is command namespace, not file path.
                if os.name == "nt":
                    return

        # Generic token-based file/path completion (from last whitespace boundary)
        if token:
            if "/" in token or "\\" in token:
                token_matches = self._get_path_completions(token)
            else:
                token_matches = self._get_local_completions(token)

            if token_matches:
                seen = set()
                for mc in token_matches:
                    if mc in seen:
                        continue
                    seen.add(mc)
                    if "/" in token or "\\" in token:
                        yield Completion(
                            mc,
                            start_position=-len(token),
                            display=self._path_leaf_name(mc),
                        )
                    else:
                        yield Completion(mc, start_position=-len(token))
                return
        
        # If input becomes empty, hide completion menu.
        if not text or text.strip() == "":
            return
        
        # Smartly detect the filename portion.
        file_part, prefix, suffix = self._extract_file_part(text)
        
        # Get file-completion options.
        if '/' in file_part or '\\' in file_part:
            # Path completion.
            completions = self._get_path_completions(file_part)
        else:
            # Complete files/folders in the current directory.
            completions = self._get_local_completions(file_part)
        
        # Ensure each completion option appears only once.
        seen = set()
        for completion in completions:
            if completion not in seen:
                seen.add(completion)
                # Build the full completion result.
                full_completion = prefix + completion + suffix
                yield Completion(full_completion, start_position=-len(text))
    
    def _extract_file_part(self, text: str) -> tuple:
        """
        Smartly extract the filename portion from the input text.
        Args:
            text: Input text
        Returns:
            (file_part, prefix, suffix) - filename portion, prefix, suffix
        """
        # Path completion: extract path part so "cd C:\Users\re" is completed as path "C:\Users\re"
        if '/' in text or '\\' in text:
            stripped = text.strip()
            if stripped.lower().startswith("cd ") or stripped.lower().startswith("cd\t"):
                idx = text.lower().find("cd ")
                if idx < 0:
                    idx = text.lower().find("cd\t")
                if idx >= 0:
                    prefix = text[: idx + 3]
                    path_part = text[idx + 3 :].strip()
                    return path_part, prefix, ""
            return text, "", ""
        
        # Get all file names in the current directory.
        try:
            base_dir = self._matching_base_directory()
            current_files = [item.name for item in base_dir.iterdir() if not item.name.startswith('.')]
        except Exception:
            current_files = []
        
        # Smart detection: find the portion that may match a file name in the current directory.
        words = text.split()
        if not words:
            return "", "", ""
        
        # Strategy 1: check whether the last word matches the start of a file name.
        last_word = words[-1]
        for filename in current_files:
            if filename.lower().startswith(last_word.lower()):
                prefix = " ".join(words[:-1])
                if prefix:
                    prefix += " "
                return last_word, prefix, ""
        
        # Strategy 2: check whether a combination of the last few words matches a file name.
        for i in range(len(words), 0, -1):
            candidate = " ".join(words[i-1:])
            for filename in current_files:
                if filename.lower().startswith(candidate.lower()):
                    prefix = " ".join(words[:i-1])
                    if prefix:
                        prefix += " "
                    return candidate, prefix, ""
        
        # Strategy 3: check whether the full file name (including extension) is present.
        for filename in current_files:
            if filename.lower() in text.lower():
                # Find the file name position in the text.
                filename_lower = filename.lower()
                text_lower = text.lower()
                start_pos = text_lower.find(filename_lower)
                if start_pos != -1:
                    prefix = text[:start_pos]
                    suffix = text[start_pos + len(filename):]
                    return filename, prefix, suffix
        
        # Strategy 4: if nothing matched, use the last word as the candidate.
        prefix = " ".join(words[:-1])
        if prefix:
            prefix += " "
        return last_word, prefix, ""
    
    def _get_directory_contents(self) -> List[str]:
        """Get the current directory contents."""
        try:
            items = []
            for item in self._matching_base_directory().iterdir():
                # Show only visible files (those not starting with .).
                if not item.name.startswith('.'):
                    items.append(item.name)
            return sorted(items)
        except Exception:
            return []
    
    def _get_local_completions(self, text: str) -> List[str]:
        """Get local completions in the current directory."""
        try:
            # Avoid noisy completion when fragment is exactly "."
            if text == ".":
                return []
            matches = []
            for item in self._matching_base_directory().iterdir():
                if item.name.lower().startswith(text.lower()):
                    matches.append(item.name)
            
            # If nothing matched, try smart completion.
            if not matches and text:
                matches = self._smart_local_completion(text)
            
            # If there is only one match, return it directly.
            if len(matches) == 1:
                return matches
            
            # If there are multiple matches, return them all for the user to choose from.
            return sorted(matches)
        except Exception:
            return []
    
    def _smart_local_completion(self, text: str) -> List[str]:
        """
        Smart local completion, including automatic addition of common file extensions.
        Args:
            text: Text to complete
        Returns:
            List of files/folders for smart completion
        """
        matches = []

        # Avoid fuzzy matching all dot-containing filenames for a single dot fragment.
        if text == ".":
            return matches
        
        # Common file extensions.
        common_extensions = ['.txt', '.py', '.js', '.html', '.css', '.json', '.xml', '.md', '.log', '.ini', '.cfg', '.conf']
        
        # 1. Try a direct match (case-insensitive).
        base_dir = self._matching_base_directory()
        for item in base_dir.iterdir():
            if item.name.lower().startswith(text.lower()):
                matches.append(item.name)
        
        # 2. If there is no direct match, try adding common extensions.
        if not matches:
            for ext in common_extensions:
                potential_file = base_dir / (text + ext)
                if potential_file.exists() and potential_file.is_file():
                    matches.append(text + ext)
        
        # 3. If still nothing matches, try fuzzy matching (substring match).
        if not matches:
            for item in base_dir.iterdir():
                if text.lower() in item.name.lower():
                    matches.append(item.name)
        
        # 4. If the file-name fragment looks like an incomplete extension, try to complete it.
        if not matches and '.' in text:
            # Example: when input is "test.t", try matching "test.txt".
            base_name, partial_ext = text.rsplit('.', 1)
            for ext in common_extensions:
                if ext.startswith('.' + partial_ext):
                    potential_file = base_dir / (base_name + ext)
                    if potential_file.exists() and potential_file.is_file():
                        matches.append(base_name + ext)
        
        return matches
    
    def _get_root_directory_completions(self, separator: str, file_part: str = "") -> List[str]:
        """
        Get root-directory completion.
        Args:
            separator: Path separator
            file_part: File-name fragment (optional)
        Returns:
            List of files/folders under the root directory
        """
        try:
            # Drive-root completion: use current drive root.
            current_drive = self._matching_base_directory().anchor  # e.g. 'C:\\'
            root_dir = Path(current_drive)
            
            if not root_dir.exists() or not root_dir.is_dir():
                return []
            
            matches = []
            try:
                for item in root_dir.iterdir():
                    # Skip hidden and system files.
                    if item.name.startswith('.'):
                        continue
                    
                    # If file_part is specified, return only matching files.
                    if file_part and not item.name.lower().startswith(file_part.lower()):
                        continue
                    
                    # Build a backslash-style path.
                    path = f"\\{item.name}"
                    
                    matches.append(path)
                    
            except PermissionError:
                # Return an empty list if the root directory is not accessible.
                return []
            
            return sorted(matches)
        except Exception:
            return []

    @staticmethod
    def _drive_letter_bang_base(rel: str) -> Optional[Tuple[Path, str]]:
        """
        If rel is a Windows path starting with X: (absolute drive), return (directory to
        list, filename prefix). pathlib's (workdir / 'd:') is not drive root when cwd is
        on the same letter — use Path('d:\\') instead.
        """
        norm = rel.replace("\\", "/")
        m = _WIN_DRIVE_BANG.match(norm)
        if not m:
            return None
        drive = m.group(1)
        rest_raw = m.group(2)
        tail = rest_raw.lstrip("/") if rest_raw else ""
        root = Path(drive + "\\")
        if not tail:
            return root, ""
        if "/" in tail:
            dir_rel, file_part = tail.rsplit("/", 1)
            base_dir = root / dir_rel.replace("/", "\\")
        else:
            base_dir = root
            file_part = tail
        return base_dir, file_part

    def _get_workspace_path_completions_for_bang(self, bang_part: str) -> List[str]:
        """
        Build workspace-relative path completions for leading '!...'.
        Example: '!src/p' -> '!src\\completion\\prompt_toolkit_input.py'
        On Windows, '!d:\\' lists D:\\ root (not the workspace when it is on D:).
        """
        try:
            if not bang_part.startswith("!"):
                return []
            rel = bang_part[1:]
            # '!\\...' or '!//' style: not workspace-relative
            if rel.startswith("\\") or rel.startswith("/"):
                return []
            # Lone "!" only: no completions (avoid popping up the full workspace list)
            if not rel:
                return []

            if os.name == "nt":
                nd = rel.replace("\\", "/")
                # Bare "x:" only — do not list drive root until user types !x:\ or !x:/
                if re.fullmatch(r"[A-Za-z]:", nd):
                    return []
                drive_pair = self._drive_letter_bang_base(rel)
                if drive_pair is not None:
                    base_dir, file_part = drive_pair
                    if not base_dir.exists() or not base_dir.is_dir():
                        return []
                    matches = []
                    for item in base_dir.iterdir():
                        if item.name.startswith("."):
                            continue
                        if file_part and not item.name.lower().startswith(file_part.lower()):
                            continue
                        candidate = "!" + os.path.normpath(str((base_dir / item.name)))
                        matches.append(candidate)
                    return sorted(matches)

            normalized = rel.replace("\\", "/")
            if "/" in normalized:
                dir_part, file_part = normalized.rsplit("/", 1)
                base_dir = (self._matching_base_directory() / dir_part).resolve()
            else:
                dir_part, file_part = "", normalized
                base_dir = self._matching_base_directory()

            if not base_dir.exists() or not base_dir.is_dir():
                return []

            matches = []
            for item in base_dir.iterdir():
                if item.name.startswith("."):
                    continue
                if file_part and not item.name.lower().startswith(file_part.lower()):
                    continue
                if dir_part:
                    win_dir = dir_part.replace("/", "\\")
                    candidate = f"!{win_dir}\\{item.name}"
                else:
                    candidate = f"!{item.name}"
                matches.append(candidate)
            return sorted(matches)
        except Exception:
            return []

    @staticmethod
    def _extract_last_token(text: str) -> Tuple[int, str]:
        """
        Extract the trailing token after the last whitespace before cursor.
        Returns (start_index, token), or (-1, "") when unavailable.
        """
        if not text:
            return -1, ""
        m = re.search(r"(^|\s)([^\s]+)$", text)
        if not m:
            return -1, ""
        token = m.group(2) or ""
        if not token:
            return -1, ""
        start = len(text) - len(token)
        return start, token

    @staticmethod
    def _path_leaf_name(candidate: str) -> str:
        """Display only leaf name for path-like completion candidates."""
        cleaned = candidate.rstrip("\\/")
        if not cleaned:
            return candidate
        return cleaned.replace("/", "\\").split("\\")[-1]
    
    def _get_path_completions(self, text: str) -> List[str]:
        """Get path completion."""
        try:
            # Normalize to backslash separator for workspace-style suggestions.
            text = text.replace("/", "\\")
            separator = '\\'
            
            # Root trigger: one or more pure separators should list root entries.
            if text and set(text) == {"\\"}:
                return self._get_root_directory_completions(separator)
            
            parts = text.split(separator)
            if len(parts) == 1:
                return self._get_local_completions(text)
            
            # Build the directory path.
            dir_part = separator.join(parts[:-1])
            file_part = parts[-1]
            
            # Special case: if dir_part is empty, it means the root directory.
            if dir_part == '':
                return self._get_root_directory_completions(separator, file_part)
            
            # Resolve the directory path.
            if dir_part.startswith("\\") or (len(dir_part) > 1 and dir_part[1] == ':'):
                # Absolute path with drive letter:
                # - leading "\" is drive-root relative (map to current drive root)
                # - "d:" should be normalized to "d:\\" for drive root
                if dir_part.startswith("\\"):
                    current_drive = Path.cwd().anchor  # e.g. "D:\\"
                    base_dir = (Path(current_drive) / dir_part.lstrip("\\")).resolve()
                elif len(dir_part) == 2 and dir_part[0].isalpha() and dir_part[1] == ':':
                    base_dir = Path(dir_part + "\\")
                else:
                    base_dir = Path(dir_part)
            else:
                # Relative path.
                base_dir = self._matching_base_directory() / dir_part
            
            if not base_dir.exists() or not base_dir.is_dir():
                return []
            
            # Find matching files/folders in the specified directory.
            matches = []
            for item in base_dir.iterdir():
                if item.name.lower().startswith(file_part.lower()):
                    # Build a backslash-style path.
                    relative_path = f"{dir_part}\\{item.name}"
                    
                    # Only append separator for directories when input ends with separator
                    if text.endswith(separator) and item.is_dir():
                        matches.append(relative_path + separator)
                    else:
                        matches.append(relative_path)
            
            # If no match is found, try smart completion.
            if not matches and file_part:
                smart_matches = self._smart_path_completion(base_dir, file_part, separator, dir_part)
                matches.extend(smart_matches)
            
            # If there is only one match, return it directly.
            if len(matches) == 1:
                return matches
            
            # If there are multiple matches, return them all for the user to choose from.
            return sorted(matches)
        except Exception:
            return []
    
    def _smart_path_completion(self, base_dir: Path, file_part: str, separator: str, dir_part: str) -> List[str]:
        """
        Smart path completion, including automatic addition of common file extensions.
        Args:
            base_dir: Base directory
            file_part: File-name fragment
            separator: Path separator
            dir_part: Current directory path fragment
        Returns:
            List of paths produced by smart completion
        """
        matches = []
        
        # Common file extensions.
        common_extensions = ['.txt', '.py', '.js', '.html', '.css', '.json', '.xml', '.md', '.log', '.ini', '.cfg', '.conf']
        
        # 1. Try a direct match (case-insensitive).
        for item in base_dir.iterdir():
            if item.name.lower().startswith(file_part.lower()):
                relative_path = f"{dir_part}\\{item.name}"
                matches.append(relative_path)
        
        # 2. If there is no direct match, try adding common extensions.
        if not matches:
            for ext in common_extensions:
                potential_file = base_dir / (file_part + ext)
                if potential_file.exists() and potential_file.is_file():
                    relative_path = f"{dir_part}\\{file_part}{ext}"
                    matches.append(relative_path)
        
        # 3. If still nothing matches, try fuzzy matching (substring match).
        if not matches:
            for item in base_dir.iterdir():
                if file_part.lower() in item.name.lower():
                    relative_path = f"{dir_part}\\{item.name}"
                    matches.append(relative_path)
        
        # 4. If the file-name fragment looks like an incomplete extension, try to complete it.
        if not matches and '.' in file_part:
            # Example: when input is "test.t", try matching "test.txt".
            base_name, partial_ext = file_part.rsplit('.', 1)
            for ext in common_extensions:
                if ext.startswith('.' + partial_ext):
                    potential_file = base_dir / (base_name + ext)
                    if potential_file.exists() and potential_file.is_file():
                        relative_path = f"{dir_part}\\{base_name}{ext}"
                        matches.append(relative_path)
        
        return matches
    
    def _find_common_prefix(self, strings: List[str]) -> str:
        """Find the common prefix for a list of strings."""
        if not strings:
            return ""
        
        # Find the length of the shortest string.
        min_len = min(len(s) for s in strings)
        
        # Compare character by character.
        for i in range(min_len):
            char = strings[0][i]
            for s in strings[1:]:
                if s[i] != char:
                    return strings[0][:i]
        
        return strings[0][:min_len]


class PromptToolkitInputHandler:
    """prompt_toolkit input handler (cross-platform)."""
    
    def __init__(
        self,
        work_directory: Path,
        workspace_directory: Optional[Path] = None,
        initial_history: Optional[List[str]] = None,
        slash_skill_commands: Optional[List[str]] = None,
        slash_mcp_commands: Optional[List[str]] = None,
        slash_dynamic_rules: Optional[List[Dict[str, Any]]] = None,
        terminal_resize_callback: Optional[Callable[[int, int], bool]] = None,
        language_provider: Optional[Callable[[], Any]] = None,
        transcript_mode_callback: Optional[Callable[[], None]] = None,
    ):
        """
        Initialize the input handler.
        Args:
            work_directory: Current working directory
            initial_history: Preloaded history command list (usually from the persistent HistoryManager)
        """
        self.work_directory = work_directory
        self.workspace_directory = workspace_directory or work_directory
        self._transcript_mode_callback = transcript_mode_callback
        self._transcript_mode_requested = False
        self.history = []
        self._status_bar_text = ""
        self._status_bar_fragments = []
        self._status_bar_enabled = True
        self._shell_mode_active = False
        self._shell_mode_auto_by_history = False
        self._shell_mode_last_working_index = None
        self._shell_mode_history_indices = set()
        self._shell_mode_sync_guard = False
        self._prompt_line = ""
        self._pending_prefill_text = ""
        self._pending_prefill_cursor_position = 0
        self._pending_shell_mode_active = False
        self.renders_prompt_separator_inline = False
        self._terminal_resize_callback = terminal_resize_callback
        self._language_provider = language_provider
        self._default_buffer_window = None
        self._pt_style = None
        self._pt_cursor_shape = None
        if (
            PROMPT_TOOLKIT_AVAILABLE
            and CursorShape is not None
            and SimpleCursorShapeConfig is not None
        ):
            try:
                self._pt_cursor_shape = SimpleCursorShapeConfig(CursorShape.BLINKING_BEAM)
            except Exception:
                self._pt_cursor_shape = None
        
        if PROMPT_TOOLKIT_AVAILABLE:
            try:
                self._pt_style = Style.from_dict(
                    {
                        # Keep toolbar with no background color.
                        "bottom-toolbar": "",
                    }
                )
            except Exception:
                self._pt_style = None
            # Use prompt_toolkit and inject history into the session.
            self.completer = FileCompleter(
                work_directory,
                self.workspace_directory,
                slash_skill_commands,
                slash_mcp_commands,
                slash_dynamic_rules,
                shell_mode_provider=lambda: bool(
                    getattr(self, "_shell_mode_active", False)
                ),
            )
            self._key_bindings = self._create_key_bindings()
            self._pt_history = InMemoryHistory()
            if initial_history:
                for entry in initial_history:
                    try:
                        cleaned = _sanitize_prompt_pollution(entry, work_directory)
                        if cleaned:
                            self._pt_history.append_string(cleaned)
                    except Exception:
                        pass
            session_kwargs = dict(
                completer=self.completer,
                history=self._pt_history,
                key_bindings=self._key_bindings,
                enable_system_prompt=True,
                enable_suspend=True,
                complete_in_thread=True,
                complete_while_typing=True,
            )
            if self._pt_style is not None:
                session_kwargs["style"] = self._pt_style
            if self._pt_cursor_shape is not None:
                session_kwargs["cursor"] = self._pt_cursor_shape
            self.session = PromptSession(**session_kwargs)
            self._install_input_box_height_limit()
            _attach_blink_after_render_hook(
                self.session,
                status_provider=self._status_line_for_overlay,
                terminal_resize_callback=self._terminal_resize_callback,
                max_input_rows_provider=self._compute_max_input_rows,
                buffer_window_provider=lambda: getattr(
                    self, "_default_buffer_window", None
                ),
            )
        else:
            # Fall back to standard input.
            self.session = None

    def set_pending_prefill(
        self, text: str, cursor_position: Optional[int] = None
    ) -> None:
        """Pre-fill the next interactive prompt with ``text``.

        Used by commands such as ``/chat edit`` that want to drop a previous
        message back into the input box so the user can edit and resubmit it.
        When ``cursor_position`` is omitted the cursor is placed at the end of
        the text.
        """
        normalized = _normalize_newlines(str(text or ""))
        self._pending_prefill_text = normalized
        if cursor_position is None:
            self._pending_prefill_cursor_position = len(normalized)
        else:
            self._pending_prefill_cursor_position = max(
                0, min(int(cursor_position), len(normalized))
            )

    def _get_terminal_rows(self, default: int = 0) -> int:
        rows = 0
        try:
            session = getattr(self, "session", None)
            if session is not None:
                output = getattr(session, "output", None)
                if output is None:
                    app = getattr(session, "app", None)
                    output = getattr(app, "output", None) if app is not None else None
                rows = _get_output_rows_from_obj(output, default=0)
        except Exception:
            rows = 0
        if rows <= 0:
            rows = _get_system_terminal_rows(default=0)
        return rows if rows > 0 else int(default or 0)

    def _compute_max_input_rows(self) -> int:
        """Maximum number of visible rows the input box may occupy.

        Defaults to ``MAX_INPUT_VISIBLE_ROWS`` but shrinks on short terminals so
        the prompt symbol keeps at least ``INPUT_TOP_MARGIN_ROWS`` rows above it
        (plus room below for the gap + status line).
        """
        base = MAX_INPUT_VISIBLE_ROWS
        rows = self._get_terminal_rows(default=0)
        if rows > 0:
            below_reserved = 2 if getattr(self, "_status_bar_enabled", True) else 1
            limit = rows - INPUT_TOP_MARGIN_ROWS - below_reserved
            if limit < 1:
                limit = 1
            return max(1, min(base, limit))
        return base

    def _menu_reserve_rows(self, app: Any, max_visible: int) -> int:
        """Rows to reserve below the cursor so the completion menu has room.

        Only reserves while a completion menu is actually open, so the box can
        otherwise grow naturally with the typed content.
        """
        try:
            session = getattr(self, "session", None)
            if session is None:
                return 0
            if getattr(session, "completer", None) is None:
                return 0
            if app is None or bool(getattr(app, "is_done", False)):
                return 0
            buf = getattr(app, "current_buffer", None)
            if buf is None or getattr(buf, "complete_state", None) is None:
                return 0
            space = int(getattr(session, "reserve_space_for_menu", 8) or 8)
            return max(0, min(space, int(max_visible or 0)))
        except Exception:
            return 0

    def _input_window_height_dimension(self):
        """Height for the input window: grow with content, cap, then scroll."""
        app = None
        try:
            from prompt_toolkit.application.current import get_app
            app = get_app()
        except Exception:
            session = getattr(self, "session", None)
            app = getattr(session, "app", None) if session is not None else None
        max_visible = self._compute_max_input_rows()
        if max_visible < 1:
            max_visible = 1
        reserve = self._menu_reserve_rows(app, max_visible) if app is not None else 0
        if reserve > 0:
            return Dimension(min=reserve, max=max_visible)
        return Dimension(min=1, max=max_visible)

    def _install_input_box_height_limit(self) -> None:
        """Cap the default buffer window so the input behaves like a GUI box."""
        self._default_buffer_window = None
        session = getattr(self, "session", None)
        if session is None:
            return
        try:
            from prompt_toolkit.layout.containers import Window
        except Exception:
            return
        try:
            layout = getattr(session, "layout", None)
            if layout is None:
                return
            for win in layout.walk():
                if not isinstance(win, Window):
                    continue
                content = getattr(win, "content", None)
                buf = getattr(content, "buffer", None) if content is not None else None
                if buf is not None and getattr(buf, "name", "") == "DEFAULT_BUFFER":
                    win.height = self._input_window_height_dimension
                    self._default_buffer_window = win
                    self._install_first_visible_prompt(win)
                    break
        except Exception:
            self._default_buffer_window = None

    def _install_first_visible_prompt(self, win: Any) -> None:
        """Keep the prompt symbol pinned to the first *visible* row.

        prompt_toolkit normally renders the prompt only on logical line 0. Once
        the capped input box scrolls, line 0 moves out of view and the prompt
        would disappear. We wrap the window's ``get_line_prefix`` so the prompt
        rides whatever row is currently first visible, while every other row
        keeps the continuation indent. The prompt (``"› "``) and the
        continuation (``MULTILINE_INDENT``) are both 2 columns wide, so this
        swap never shifts the text or the cursor math.
        """
        orig = getattr(win, "get_line_prefix", None)
        if orig is None:
            return

        def _line_prefix(line_number: int, wrap_count: int):
            try:
                vs = int(getattr(win, "vertical_scroll", 0) or 0)
                vs2 = int(getattr(win, "vertical_scroll_2", 0) or 0)
                if line_number == vs and wrap_count == vs2:
                    # First visible row -> show the prompt symbol.
                    return orig(0, 0)
                # Any other row -> force the continuation indent (never the
                # prompt, which must appear exactly once).
                return orig(1, 0)
            except Exception:
                return orig(line_number, wrap_count)

        try:
            win.get_line_prefix = _line_prefix
        except Exception:
            pass

    def _render_bottom_toolbar(self):
        try:
            if not self._status_bar_enabled:
                return ""
            text = str(self._status_bar_text or "")
            frags = self._status_bar_fragments if isinstance(self._status_bar_fragments, list) else []
            if (not text.strip()) and (not frags):
                return ""
            if self.session is None:
                return ""
            # Keep one blank line between prompt and status line.
            if frags:
                return [("", "\n")] + frags
            return [("", "\n" + text)]
        except Exception:
            return ""

    def _status_line_for_overlay(self) -> str:
        try:
            if not self._status_bar_enabled:
                return ""
            if self.session is not None:
                app = getattr(self.session, "app", None)
                if app is not None:
                    buf = getattr(app, "current_buffer", None)
                    if (
                        buf is not None
                        and getattr(buf, "complete_state", None) is not None
                    ):
                        # Completion menus occupy the same visual area as the
                        # overlay status line; hide only while the menu is open.
                        return ""
            status_line = str(self._status_bar_text or "")
            if bool(getattr(self, "_shell_mode_active", False)):
                status_line = self._compose_shell_mode_status_line(status_line)
            return status_line
        except Exception:
            return ""

    def _shell_mode_prompt_message(self):
        if bool(getattr(self, "_shell_mode_active", False)):
            return [(f"fg:{SHELL_MODE_COLOR_HEX}", SHELL_MODE_PROMPT)]
        return str(getattr(self, "_prompt_line", "") or "")

    @staticmethod
    def _strip_one_leading_bang(text: str) -> str:
        s = str(text or "")
        if not s:
            return s
        idx = 0
        n = len(s)
        while idx < n and s[idx].isspace():
            idx += 1
        if idx >= n or s[idx] != "!":
            return s
        # Keep left indentation, remove one leading bang, and trim spaces after it.
        return s[:idx] + s[idx + 1 :].lstrip()

    def _sync_shell_mode_from_buffer(self, buf: Any) -> bool:
        if buf is None:
            return False
        if bool(getattr(self, "_shell_mode_sync_guard", False)):
            return False

        text = str(getattr(buf, "text", "") or "")
        trimmed = text.lstrip()
        starts_with_bang = bool(trimmed.startswith("!"))

        working_index = getattr(buf, "working_index", None)
        prev_working_index = getattr(self, "_shell_mode_last_working_index", None)
        history_navigated = (
            prev_working_index is not None and working_index != prev_working_index
        )
        self._shell_mode_last_working_index = working_index

        active = bool(getattr(self, "_shell_mode_active", False))
        auto_by_history = bool(getattr(self, "_shell_mode_auto_by_history", False))
        shell_history_indices = getattr(self, "_shell_mode_history_indices", None)
        if not isinstance(shell_history_indices, set):
            shell_history_indices = set()
            self._shell_mode_history_indices = shell_history_indices
        known_shell_index = working_index in shell_history_indices
        changed = False

        if trimmed.startswith("/") and known_shell_index:
            shell_history_indices.discard(working_index)
            known_shell_index = False

        if starts_with_bang and working_index is not None:
            shell_history_indices.add(working_index)
            known_shell_index = True

        shell_history_match = bool(
            starts_with_bang
            or known_shell_index
            or (
                bool(history_navigated)
                and self._buffer_matches_shell_history_entry(text)
            )
        )

        if shell_history_match:
            if (not active) or bool(history_navigated):
                self._shell_mode_active = True
                self._shell_mode_auto_by_history = True
                active = True
                auto_by_history = True
                changed = True

            if starts_with_bang and auto_by_history:
                normalized = self._strip_one_leading_bang(text)
                if normalized != text:
                    try:
                        old_cursor = int(getattr(buf, "cursor_position", len(text)) or 0)
                    except Exception:
                        old_cursor = len(text)
                    removed = max(0, len(text) - len(normalized))
                    self._shell_mode_sync_guard = True
                    try:
                        buf.text = normalized
                        try:
                            max_pos = len(normalized)
                            buf.cursor_position = max(0, min(max_pos, old_cursor - removed))
                        except Exception:
                            pass
                    finally:
                        self._shell_mode_sync_guard = False
                    changed = True
        else:
            # When history navigation lands on a non-shell entry, sync back to
            # normal mode when the new buffer contents actually look like a
            # recalled history entry. Fresh manual typing can also move the
            # working index on first edit, and should stay in shell mode.
            if bool(history_navigated) and active and (
                auto_by_history or self._buffer_matches_history_entry(text)
            ):
                self._shell_mode_active = False
                self._shell_mode_auto_by_history = False
                changed = True

        return changed

    def _buffer_matches_history_entry(self, text: str) -> bool:
        candidate = str(text or "").strip()
        if not candidate:
            return False

        for entry in getattr(self, "history", []) or []:
            normalized = str(entry or "").strip()
            if not normalized:
                continue
            if normalized == candidate:
                return True
            if self._strip_one_leading_bang(normalized).strip() == candidate:
                return True
        return False

    def _buffer_matches_shell_history_entry(self, text: str) -> bool:
        candidate = str(text or "").strip()
        if not candidate:
            return False

        for entry in getattr(self, "history", []) or []:
            normalized = str(entry or "").strip()
            if not normalized or not normalized.lstrip().startswith("!"):
                continue
            if self._strip_one_leading_bang(normalized).strip() == candidate:
                return True
        return False

    def _install_shell_mode_sync_handler(self, buf: Any) -> None:
        if buf is None:
            return
        if getattr(buf, _SHELL_MODE_SYNC_HANDLER_ATTR, None) is not None:
            return

        def _on_text_changed(_ev: Any = None) -> None:
            changed = self._sync_shell_mode_from_buffer(buf)
            if not changed:
                return
            try:
                if self.session is not None:
                    app = getattr(self.session, "app", None)
                    if app is not None:
                        app.invalidate()
            except Exception:
                pass

        try:
            evt = getattr(buf, "on_text_changed", None)
            if evt is None:
                return
            if hasattr(evt, "add_handler"):
                evt.add_handler(_on_text_changed)
            else:
                evt += _on_text_changed
            setattr(buf, _SHELL_MODE_SYNC_HANDLER_ATTR, _on_text_changed)
        except Exception:
            pass

    def _enhanced_keyboard_reporting_enabled(self) -> bool:
        """
        Whether to ask the terminal to report Shift/Ctrl+Enter as distinct keys.

        Only meaningful on POSIX VT100 terminals: on Windows we already detect
        Shift via native key state, and VS Code's terminal does not handle the
        sequence cleanly. Can be disabled with codewood_DISABLE_ENHANCED_KEYS=1.
        """
        if not PROMPT_TOOLKIT_AVAILABLE:
            return False
        if os.name == "nt":
            return False
        raw = str(os.environ.get("codewood_DISABLE_ENHANCED_KEYS", "") or "").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return False
        if _is_vscode_terminal():
            return False
        try:
            if not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()):
                return False
        except Exception:
            return False
        return True

    def _write_terminal_control(self, data: str) -> None:
        try:
            output = None
            if self.session is not None:
                app = getattr(self.session, "app", None)
                if app is not None:
                    output = getattr(app, "output", None)
                if output is None:
                    output = getattr(self.session, "output", None)
            if output is not None and hasattr(output, "write_raw") and hasattr(output, "flush"):
                output.write_raw(data)
                output.flush()
                return
            sys.stdout.write(data)
            sys.stdout.flush()
        except Exception:
            pass

    def _begin_enhanced_keyboard_reporting(self) -> None:
        """Enable xterm modifyOtherKeys so Shift+Enter becomes a distinct key."""
        if not self._enhanced_keyboard_reporting_enabled():
            return
        # CSI > 4 ; 1 m -> modifyOtherKeys level 1 (only "ambiguous" modified keys,
        # e.g. Shift+Enter; leaves plain typing/arrows/backspace untouched).
        self._write_terminal_control("\x1b[>4;1m")

    def _end_enhanced_keyboard_reporting(self) -> None:
        if not self._enhanced_keyboard_reporting_enabled():
            return
        # Restore the terminal default (modifyOtherKeys off).
        self._write_terminal_control("\x1b[>4;0m")

    def _clear_status_overlay_line_if_possible(self) -> None:
        try:
            if not bool(getattr(self, "_status_bar_enabled", False)):
                return
            output = None
            if self.session is not None:
                app = getattr(self.session, "app", None)
                if app is not None:
                    output = getattr(app, "output", None)
                if output is None:
                    output = getattr(self.session, "output", None)
            if output is not None and hasattr(output, "write_raw") and hasattr(output, "flush"):
                rows_down = 2
                try:
                    app = getattr(self.session, "app", None)
                    if app is not None:
                        rows_down, _, _ = _get_status_overlay_position(app)
                except Exception:
                    rows_down = 2
                _write_overlay_line(output, "", rows_down)
                output.flush()
                return
            if hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
                sys.stdout.write("\x1b7\x1b[2B\r\x1b[2K\x1b8")
                sys.stdout.flush()
        except Exception:
            pass

    def _compose_shell_mode_status_line(self, base_status_line: str) -> str:
        base_colored = str(base_status_line or "")
        base_plain = _strip_ansi_sgr(base_colored)
        label_plain = SHELL_MODE_LABEL
        label_colored = _ansi_rgb(label_plain, *SHELL_MODE_COLOR_RGB)
        right_padding = _shell_mode_effective_right_padding()
        if self.session is None:
            return f"{base_colored}  {label_colored}"

        cols = _get_output_columns(self.session, default=80)
        right_len = _display_width(label_plain)
        start_col = max(1, int(cols) - right_len - right_padding + 1)
        left_cap = max(0, start_col - 1)
        left_len = _display_width(base_plain)
        if left_len <= left_cap:
            base_render = base_colored
        else:
            # When left area overflows, trim plain text to prevent overlap with the
            # right-aligned shell-mode marker.
            base_render = _truncate_to_display_width(base_plain, left_cap)
        return f"{base_render}\x1b[{start_col}G{label_colored}{' ' * right_padding}"

    @staticmethod
    def _erase_previous_prompt_line_if_tty() -> None:
        try:
            if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
                return
            sys.stdout.write("\x1b[1A\r\x1b[2K\r")
            sys.stdout.flush()
        except Exception:
            pass

    def _ui_language(self) -> str:
        provider = getattr(self, "_language_provider", None)
        if not callable(provider):
            return DEFAULT_DISPLAY_LANGUAGE
        try:
            return normalize_display_language(provider()) or DEFAULT_DISPLAY_LANGUAGE
        except Exception:
            return DEFAULT_DISPLAY_LANGUAGE

    def _print_shell_mode_empty_command_hint(self) -> None:
        bullet = _ansi_gray("•")
        lang = self._ui_language()
        example = _ansi_gray(translate("input.shell_mode_example", lang))
        print(f"{bullet} {translate('input.shell_mode_hint', lang)} {example}\n")

    def get_terminal_columns(self, default: int = 80) -> int:
        if self.session is not None:
            return _get_output_columns(self.session, default=default)
        return int(default or 80)

    @staticmethod
    def _multiline_prompt_continuation(*_args: Any) -> str:
        # Keep all wrapped/continued lines visually aligned with a fixed 2-space indent.
        return MULTILINE_INDENT

    def get_input_with_completion(
        self,
        prompt: str,
        status_bar_text: str = "",
        status_bar_fragments: Optional[List[Tuple[str, str]]] = None,
        show_status_bar: bool = True,
        show_separator: bool = True,
    ) -> str:
        """
        Get user input with auto-completion.
        Args:
            prompt: Input prompt
            status_bar_text: Status bar text (shown at the bottom of the console)
            status_bar_fragments: Status bar fragment style text (prompt_toolkit formatted text)
            show_status_bar: Whether to show the status bar
        Returns:
            The user-entered text
        """
        if not hasattr(self, "_shell_mode_active"):
            self._shell_mode_active = False
        if not hasattr(self, "_prompt_line"):
            self._prompt_line = ""
        if not hasattr(self, "_pending_shell_mode_active"):
            self._pending_shell_mode_active = False
        if not hasattr(self, "_pending_prefill_cursor_position"):
            self._pending_prefill_cursor_position = 0
        if not hasattr(self, "_shell_mode_auto_by_history"):
            self._shell_mode_auto_by_history = False
        if not hasattr(self, "_shell_mode_last_working_index"):
            self._shell_mode_last_working_index = None
        if not hasattr(self, "_shell_mode_history_indices"):
            self._shell_mode_history_indices = set()
        if not hasattr(self, "_shell_mode_sync_guard"):
            self._shell_mode_sync_guard = False
        if not hasattr(self, "_transcript_mode_requested"):
            self._transcript_mode_requested = False

        self._status_bar_text = str(status_bar_text or "")
        self._status_bar_fragments = (
            list(status_bar_fragments)
            if isinstance(status_bar_fragments, list)
            else []
        )
        self._status_bar_enabled = bool(show_status_bar)
        self._shell_mode_active = bool(getattr(self, "_pending_shell_mode_active", False))
        try:
            if self.session:
                # Avoid bottom_toolbar (it is pinned to terminal bottom). Render a
                # status line 2 lines below prompt while keeping prompt_toolkit in
                # charge of prompt/cursor layout to avoid corruption.
                if "\n" in prompt:
                    first, rest = prompt.split("\n", 1)
                    sys.stdout.write(f"\x1b[90m{first}\x1b[0m\n")
                    prompt_line = rest
                else:
                    prompt_line = prompt
                self._prompt_line = str(prompt_line or "")
                prefill_text = str(getattr(self, "_pending_prefill_text", "") or "")
                prompt_kwargs: Dict[str, Any] = dict(
                    multiline=True,
                    prompt_continuation=self._multiline_prompt_continuation,
                )
                pending_cursor: Optional[int] = None
                if prefill_text:
                    prompt_kwargs["default"] = prefill_text
                    pending_cursor = int(
                        getattr(self, "_pending_prefill_cursor_position", 0) or 0
                    )

                def _pre_run_setup() -> None:
                    try:
                        app = getattr(self.session, "app", None)
                        if app is None:
                            return
                        buf = getattr(app, "current_buffer", None)
                        if buf is None:
                            return
                        self._install_shell_mode_sync_handler(buf)
                        self._shell_mode_last_working_index = getattr(
                            buf, "working_index", None
                        )
                        self._sync_shell_mode_from_buffer(buf)
                        if pending_cursor is not None:
                            max_pos = len(str(getattr(buf, "text", "") or ""))
                            cursor = max(0, min(pending_cursor, max_pos))
                            buf.cursor_position = cursor
                    except Exception:
                        pass

                prompt_kwargs["pre_run"] = _pre_run_setup

                self._begin_enhanced_keyboard_reporting()
                try:
                    user_input = self.session.prompt(
                        self._shell_mode_prompt_message,
                        **prompt_kwargs,
                    ).strip()
                finally:
                    self._end_enhanced_keyboard_reporting()
                self._clear_status_overlay_line_if_possible()
                if bool(getattr(self, "_transcript_mode_requested", False)):
                    # Shift+Alt+T exited the prompt to open transcript mode.
                    # Run the callback (which may delete history and pre-fill a
                    # message for editing), then re-show the input prompt.
                    self._transcript_mode_requested = False
                    callback = getattr(self, "_transcript_mode_callback", None)
                    if callable(callback):
                        try:
                            callback()
                        except Exception:
                            pass
                    return self.get_input_with_completion(
                        prompt,
                        status_bar_text=status_bar_text,
                        status_bar_fragments=status_bar_fragments,
                        show_status_bar=show_status_bar,
                        show_separator=show_separator,
                    )
                if bool(getattr(self.session, _RESIZE_ATTR_INTERRUPTED, False)):
                    draft = str(getattr(self.session, _RESIZE_ATTR_DRAFT, "") or "")
                    draft_cursor = int(
                        getattr(self.session, _RESIZE_ATTR_CURSOR, 0) or 0
                    )
                    self._pending_prefill_text = _normalize_newlines(draft)
                    self._pending_prefill_cursor_position = max(0, draft_cursor)
                    self._pending_shell_mode_active = bool(getattr(self, "_shell_mode_active", False))
                    try:
                        setattr(self.session, _RESIZE_ATTR_INTERRUPTED, False)
                        setattr(self.session, _RESIZE_ATTR_DRAFT, "")
                        setattr(self.session, _RESIZE_ATTR_CURSOR, 0)
                    except Exception:
                        pass
                    return ""
                self._pending_prefill_text = ""
                self._pending_prefill_cursor_position = 0
                self._pending_shell_mode_active = False
            else:
                # Fall back to standard input.
                user_input = input(prompt).strip()

            user_input = _normalize_newlines(user_input)
            user_input = _sanitize_prompt_pollution(user_input, self.work_directory)
            if bool(getattr(self, "_shell_mode_active", False)):
                shell_text = str(user_input or "").strip()
                self._shell_mode_active = False
                self._shell_mode_auto_by_history = False
                if not shell_text:
                    self._erase_previous_prompt_line_if_tty()
                    self._print_shell_mode_empty_command_hint()
                    return ""
                if shell_text.startswith("!"):
                    user_input = shell_text
                else:
                    user_input = f"!{shell_text}"
            
            # Save to history.
            if user_input:
                self.history.append(user_input)

            return user_input
            
        except KeyboardInterrupt:
            sys.stdout.write("^C\n")
            raise
        except EOFError:
            print()
            raise KeyboardInterrupt
        except Exception as e:
            lang = self._ui_language()
            print(f"\n{translate('input.error', lang, error=e)}")
            return ""

    def _create_key_bindings(self):
        """
        Keep completion menu updated while deleting characters.
        """
        kb = KeyBindings()

        def _safe_bind(keys: Tuple[str, ...], handler: Callable[[Any], None], *, eager: bool = False) -> bool:
            try:
                kb.add(*keys, eager=eager)(handler)
                return True
            except Exception:
                return False

        def _insert_newline(event) -> None:
            event.current_buffer.insert_text("\n")

        def _accept_input(event) -> None:
            buf = event.current_buffer
            if not bool(getattr(self, "_shell_mode_active", False)):
                # Ignore plain Enter on an empty prompt in normal mode.
                if not str(getattr(buf, "text", "") or "").strip():
                    return
            if _is_windows_shift_pressed():
                _insert_newline(event)
                return
            buf.validate_and_handle()

        def _enter_shell_mode(event) -> None:
            buf = event.current_buffer
            text = str(getattr(buf, "text", "") or "")
            if text:
                # If "!" is inserted at the very beginning of an existing draft,
                # treat it as switching into Shell mode instead of inserting a literal
                # leading bang (which would otherwise become "!!<cmd>" on submit).
                cursor_pos = int(getattr(buf, "cursor_position", len(text)) or 0)
                if cursor_pos <= 0:
                    self._shell_mode_active = True
                    self._shell_mode_auto_by_history = False
                    try:
                        event.app.invalidate()
                    except Exception:
                        pass
                    return
                buf.insert_text("!")
                return
            self._shell_mode_active = True
            self._shell_mode_auto_by_history = False
            try:
                event.app.invalidate()
            except Exception:
                pass

        def _on_bracketed_paste(event) -> None:
            pasted = _normalize_newlines(getattr(event, "data", ""))
            if pasted:
                event.current_buffer.insert_text(pasted)

        def _enter_transcript_mode(event) -> None:
            # Shift+Alt+T opens the read-only transcript view. The current
            # draft is preserved and restored on return.
            if not callable(getattr(self, "_transcript_mode_callback", None)):
                return
            buf = event.current_buffer
            draft = str(getattr(buf, "text", "") or "")
            draft_cursor = int(getattr(buf, "cursor_position", len(draft)) or 0)
            self._pending_prefill_text = _normalize_newlines(draft)
            self._pending_prefill_cursor_position = max(0, draft_cursor)
            self._pending_shell_mode_active = bool(
                getattr(self, "_shell_mode_active", False)
            )
            self._transcript_mode_requested = True
            try:
                event.app.exit(result="")
            except Exception:
                pass

        # Enter always submits in multiline mode.
        # Keep Enter as non-eager so multi-key Shift+Enter sequences (starting with
        # ESC in some terminals) have a chance to match first.
        _safe_bind(("enter",), _accept_input, eager=False)
        _safe_bind(("!",), _enter_shell_mode, eager=True)

        # Shift+Enter is not a standard VT100 key. We support common terminals that emit
        # CSI-u/modifyOtherKeys sequences, and keep Ctrl+J as a reliable newline fallback.
        _safe_bind(("c-j",), _insert_newline, eager=True)
        for alias in SHIFT_ENTER_KEY_ALIASES:
            _safe_bind(alias, _insert_newline, eager=True)
        _safe_bind(("<bracketed-paste>",), _on_bracketed_paste, eager=True)

        # Shift+Alt+T enters transcript (full-screen read-only) mode. Terminals
        # send Alt as an ESC prefix; the shifted letter arrives as uppercase 'T'.
        # Bind the lowercase Alt+t too for convenience.
        _safe_bind(("escape", "T"), _enter_transcript_mode, eager=True)
        _safe_bind(("escape", "t"), _enter_transcript_mode, eager=True)

        @kb.add("backspace")
        def _on_backspace(event):
            buf = event.current_buffer
            shell_mode_active = bool(getattr(self, "_shell_mode_active", False))
            text_before = str(getattr(buf, "text", "") or "")
            try:
                cursor_pos = int(getattr(buf, "cursor_position", len(text_before)) or 0)
            except Exception:
                cursor_pos = len(text_before)

            def _is_bang_only_marker(value: str) -> bool:
                s = str(value or "")
                if not s:
                    return False
                if not s.lstrip().startswith("!"):
                    return False
                return not str(self._strip_one_leading_bang(s) or "").strip()

            if shell_mode_active and cursor_pos <= 0:
                self._shell_mode_active = False
                self._shell_mode_auto_by_history = False
                try:
                    event.app.invalidate()
                except Exception:
                    pass
                return

            if shell_mode_active and not text_before:
                self._shell_mode_active = False
                self._shell_mode_auto_by_history = False
                try:
                    event.app.invalidate()
                except Exception:
                    pass
                return

            bang_only_before_delete = bool(
                shell_mode_active and _is_bang_only_marker(text_before)
            )
            buf.delete_before_cursor(count=1)
            if bang_only_before_delete:
                text_after = str(getattr(buf, "text", "") or "")
                if (not text_after.strip()) or _is_bang_only_marker(text_after):
                    try:
                        buf.text = ""
                        if hasattr(buf, "cursor_position"):
                            buf.cursor_position = 0
                    except Exception:
                        pass
                    self._shell_mode_active = False
                    self._shell_mode_auto_by_history = False
                    try:
                        event.app.invalidate()
                    except Exception:
                        pass
                    return
            # Recompute completions immediately after deletion.
            buf.start_completion(select_first=False)

        @kb.add("delete")
        def _on_delete(event):
            buf = event.current_buffer
            buf.delete(count=1)
            # Recompute completions immediately after deletion.
            buf.start_completion(select_first=False)

        @kb.add("tab")
        def _on_tab(event):
            """
            Trigger completion and, for '/mcp/<mcp-server-name>/' roots, immediately
            trigger a second completion so tools/prompts show without extra typing.
            """
            buf = event.current_buffer
            try:
                if not hasattr(self, "completer"):
                    buf.start_completion(select_first=False)
                    return

                def _prepare_delayed_trigger(text_before_cursor: str) -> bool:
                    _, slash_part = FileCompleter._slash_fragment_for_completion(
                        text_before_cursor
                    )
                    if not slash_part:
                        return False
                    sl = slash_part.lower()

                    delayed_groups = []
                    try:
                        delayed_groups = self.completer._resolve_dynamic_groups()
                    except Exception:
                        delayed_groups = []
                    triggers = [
                        str(trig or "")
                        for trig, _ in delayed_groups
                        if str(trig or "").strip()
                    ]
                    for trig in triggers:
                        if sl == trig.lower():
                            return True
                    # If a completion inserted the command without trailing space,
                    # add one so delayed dynamic candidates can be shown immediately.
                    for trig in triggers:
                        if sl == trig.rstrip().lower():
                            try:
                                buf.insert_text(" ")
                            except Exception:
                                return False
                            return True

                    return False

                # Always compute candidates from current document first; if there is
                # exactly one candidate, force-apply it even when a completion menu
                # was previously open.
                complete_event = CompleteEvent(completion_requested=True)
                candidates = list(
                    self.completer.get_completions(buf.document, complete_event)
                )
                if len(candidates) == 1:
                    try:
                        buf.apply_completion(candidates[0])
                    except Exception:
                        buf.start_completion(select_first=False)
                    if _prepare_delayed_trigger(buf.document.text_before_cursor):
                        try:
                            if hasattr(buf, "cancel_completion"):
                                buf.cancel_completion()
                        except Exception:
                            pass
                        buf.start_completion(select_first=False)
                    return

                # Multiple candidates: insert common suffix first (e.g. 'ski' -> 'skill/').
                if len(candidates) > 1:
                    common_suffix = ""
                    try:
                        common_suffix = get_common_complete_suffix(
                            buf.document, candidates
                        )
                    except Exception:
                        common_suffix = ""
                    if common_suffix:
                        try:
                            buf.insert_text(common_suffix)
                        except Exception:
                            pass
                        if _prepare_delayed_trigger(buf.document.text_before_cursor):
                            try:
                                if hasattr(buf, "cancel_completion"):
                                    buf.cancel_completion()
                            except Exception:
                                pass
                        buf.start_completion(select_first=False)
                        return

                    # Slash command mode: when there are multiple candidates but
                    # no common suffix, Tab should still commit the first
                    # candidate (historical behavior users expect), instead of
                    # only moving menu selection.
                    _, slash_part = FileCompleter._slash_fragment_for_completion(
                        buf.document.text_before_cursor
                    )
                    if slash_part:
                        try:
                            buf.apply_completion(candidates[0])
                        except Exception:
                            pass
                        if _prepare_delayed_trigger(buf.document.text_before_cursor):
                            try:
                                if hasattr(buf, "cancel_completion"):
                                    buf.cancel_completion()
                            except Exception:
                                pass
                            buf.start_completion(select_first=False)
                        return

                # Multiple/no candidates without common suffix: usual menu behavior.
                if buf.complete_state:
                    before_text = buf.document.text_before_cursor
                    buf.complete_next()
                    # Some prompt_toolkit states only move selection without
                    # inserting text immediately. Force-apply current completion
                    # so delayed trigger detection can work in the same Tab press.
                    try:
                        after_text = buf.document.text_before_cursor
                        if after_text == before_text:
                            # Preferred: apply the currently selected completion
                            # from completion state when available.
                            if (
                                getattr(buf, "complete_state", None) is not None
                                and getattr(
                                    buf.complete_state, "current_completion", None
                                )
                                is not None
                            ):
                                buf.apply_completion(buf.complete_state.current_completion)
                            # Fallback: if still unchanged, apply first candidate
                            # from the completion snapshot we computed for this Tab.
                            if (
                                buf.document.text_before_cursor == before_text
                                and len(candidates) > 0
                            ):
                                buf.apply_completion(candidates[0])
                    except Exception:
                        pass
                    if _prepare_delayed_trigger(buf.document.text_before_cursor):
                        try:
                            if hasattr(buf, "cancel_completion"):
                                buf.cancel_completion()
                        except Exception:
                            pass
                        buf.start_completion(select_first=False)
                else:
                    buf.start_completion(select_first=False)
            except Exception:
                buf.start_completion(select_first=False)
                return

        return kb
    
    def update_work_directory(self, new_directory: Path):
        """Update the working directory."""
        self.work_directory = new_directory
        if self.session and hasattr(self, 'completer'):
            self.completer.work_directory = new_directory

    def update_workspace_directory(self, new_directory: Path):
        """Update the workspace root used for path matching/completion."""
        self.workspace_directory = new_directory
        if self.session and hasattr(self, "completer"):
            self.completer.workspace_directory = new_directory

    def set_slash_skill_commands(self, slash_skill_commands: Optional[List[str]] = None) -> None:
        if hasattr(self, "completer"):
            self.completer.slash_skill_commands = slash_skill_commands or []

    def set_slash_mcp_commands(self, slash_mcp_commands: Optional[List[str]] = None) -> None:
        if hasattr(self, "completer"):
            self.completer.slash_mcp_commands = slash_mcp_commands or []

    def set_slash_dynamic_rules(
        self, slash_dynamic_rules: Optional[List[Dict[str, Any]]] = None
    ) -> None:
        if hasattr(self, "completer"):
            self.completer.slash_dynamic_rules = slash_dynamic_rules or []

    def reset_command_history(self, entries: Optional[List[str]] = None) -> None:
        """
        Rebuild prompt_toolkit InMemoryHistory from entries (e.g. after HistoryManager.clear_history()).
        Must be called when disk history is cleared; otherwise arrow-key history stays stale in RAM.
        """
        self.history = []
        self._shell_mode_active = False
        self._shell_mode_auto_by_history = False
        self._shell_mode_last_working_index = None
        self._shell_mode_history_indices = set()
        self._shell_mode_sync_guard = False
        if not PROMPT_TOOLKIT_AVAILABLE or not getattr(self, "session", None):
            return
        entries = entries if entries is not None else []
        self._pt_history = InMemoryHistory()
        for entry in entries:
            try:
                cleaned = _sanitize_prompt_pollution(entry, self.work_directory)
                if cleaned:
                    self._pt_history.append_string(cleaned)
            except Exception:
                pass
        session_kwargs = dict(
            completer=self.completer,
            history=self._pt_history,
            key_bindings=self._key_bindings,
            enable_system_prompt=True,
            enable_suspend=True,
            complete_in_thread=True,
            complete_while_typing=True,
        )
        if getattr(self, "_pt_style", None) is not None:
            session_kwargs["style"] = self._pt_style
        if getattr(self, "_pt_cursor_shape", None) is not None:
            session_kwargs["cursor"] = self._pt_cursor_shape
        self.session = PromptSession(**session_kwargs)
        self._install_input_box_height_limit()
        _attach_blink_after_render_hook(
            self.session,
            status_provider=self._status_line_for_overlay,
            terminal_resize_callback=self._terminal_resize_callback,
            max_input_rows_provider=self._compute_max_input_rows,
            buffer_window_provider=lambda: getattr(
                self, "_default_buffer_window", None
            ),
        )


def create_prompt_toolkit_input_handler(
    work_directory: Path,
    workspace_directory: Optional[Path] = None,
    initial_history: Optional[List[str]] = None,
    slash_skill_commands: Optional[List[str]] = None,
    slash_mcp_commands: Optional[List[str]] = None,
    slash_dynamic_rules: Optional[List[Dict[str, Any]]] = None,
    terminal_resize_callback: Optional[Callable[[int, int], bool]] = None,
    language_provider: Optional[Callable[[], Any]] = None,
    transcript_mode_callback: Optional[Callable[[], None]] = None,
) -> PromptToolkitInputHandler:
    """Create a prompt_toolkit input handler."""
    return PromptToolkitInputHandler(
        work_directory,
        workspace_directory,
        initial_history,
        slash_skill_commands,
        slash_mcp_commands,
        slash_dynamic_rules,
        terminal_resize_callback,
        language_provider,
        transcript_mode_callback,
    )
