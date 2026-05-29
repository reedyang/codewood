import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
import unicodedata
import contextlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config.app_info import get_app_runtime_attr_name
from ..core.console_utils import (
    _WorkingStatusTicker,
    _ansi_gray,
    _safe_console_write,
)
from ..core.console_title import restore_app_console_title

SHELL_OUTPUT_DISPLAY_TAIL_LINES = 30
SHELL_OUTPUT_DISPLAY_RESERVED_LINES = 3
SHELL_WORKING_STATUS_MARQUEE_FPS = 10.0
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ANSI_OSC_RE = re.compile(r"\x1b\][^\a\x1b]*(?:\a|\x1b\\)")
_STREAM_ATTR_TERMINAL_COLUMNS = get_app_runtime_attr_name("terminal_columns")
_STREAM_ATTR_OUTPUT_INDENT_WIDTH = get_app_runtime_attr_name("output_indent_width")


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


def _format_omitted_lines_notice(omitted_lines: int, stream: Any) -> str:
    msg = f"... omitted {int(omitted_lines)} lines ..."
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
) -> str:
    raw = str(text or "")
    if not raw:
        return ""
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
    return _format_omitted_lines_notice(omitted, stream) + tail


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
) -> str:
    tail, omitted = _select_logical_tail_output_for_live_replay(
        text,
        stream,
        tail_lines,
        display_indent_width=display_indent_width,
    )
    if omitted <= 0:
        return tail
    return f"... omitted {omitted} lines ...\n{tail}"


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
    if not re.match(r"(?i)^powershell(?:\.exe)?\b", cmd):
        return {"ok": True, "command": command}
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
    command = tune_7z_output_for_piped_terminal(command)
    enforce_res = _enforce_windows_powershell_command_prefix(command)
    if not enforce_res.get("ok", False):
        return {"success": False, "error": str(enforce_res.get("error", "PowerShell command format is invalid"))}
    command = str(enforce_res.get("command") or command)
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
        prompt_text = f"⚠️ Confirm executing system command: {command} ?"
        if force_manual_confirm_by_policy:
            prompt_text = f"⚠️ AI requires manual confirmation. Please confirm before continuing: {command} ?"
        ok = agent._prompt_confirm_yes_no_maybe_always(
            prompt_text,
            offer_always=agent._shell_confirm_should_offer_always(command),
            kind="shell",
            shell_command=command,
        )
        if not ok:
            return {"success": False, "error": "Operation cancelled by user"}

    import subprocess

    merge_path: Optional[str] = None
    execution_cwd = _resolve_shell_execution_cwd(agent)
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
            status_ticker = _WorkingStatusTicker(
                sys.stdout,
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
                                try:
                                    target.write(text_chunk)
                                    target.flush()
                                except Exception:
                                    _safe_console_write(text_chunk, target, append_newline=False)
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
                            try:
                                target.write(tail)
                                target.flush()
                            except Exception:
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
            displayed_out = _build_tail_output_for_display(out, sys.stdout, out_tail_limit)
            displayed_out_plain = _strip_console_color_controls(displayed_out)
            should_replay_out = True
            if interactive:
                # Interactive mode already streamed raw output to console.
                # Skip replay when output fully fits within the tail limit and
                # no post-processing changed the displayed text.
                if displayed_out and (_count_output_lines(out) <= out_tail_limit) and (displayed_out == out):
                    should_replay_out = False
            last_rendered_chunk = ""
            replay_out_text = displayed_out_plain if (displayed_out and should_replay_out) else ""
            if (not replay_out_text) and int(return_code) == 0 and (not aborted_by_user):
                replay_out_text = "(no output)\n"
            replay_rendered_lines = 0
            lock_obj = live_stream_state.get("_write_lock")
            lock_ctx = lock_obj if hasattr(lock_obj, "__enter__") and hasattr(lock_obj, "__exit__") else contextlib.nullcontext()
            with lock_ctx:
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
                # caused by mixing "正在思考..." into the output's last visual line.
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
            base_out: Dict[str, Any] = {
                "output": out,
                "return_code": return_code,
                "interactive": interactive,
                "aborted_by_user": bool(aborted_by_user),
                "display_output": replay_out_text,
                "display_rendered_lines": int(replay_rendered_lines) + int(banner_lines),
            }
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
        refresh_timeout_ms=2000,
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


def tune_7z_output_for_piped_terminal(command: str) -> str:
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
        print(f"ℹ️ Enabled compatibility output flags for 7z command: {' '.join(appended)}")
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
    try:
        if invoked.is_file():
            name = invoked.name
            invoked.unlink()
            agent._ephemeral_script_paths.discard(key)
            agent._ai_created_path_keys.discard(key)
            print(f"🗑️ Auto-deleted temporary script created in this session: {name}")
            return name
    except OSError as e:
        print(f"⚠️ Failed to auto-delete temporary script ({invoked}): {e}")
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

