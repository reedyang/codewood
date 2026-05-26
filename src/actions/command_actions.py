import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.console_utils import (
    _WorkingStatusTicker,
    _ansi_gray,
    _decode_subprocess_output,
    _safe_console_write,
)

SHELL_OUTPUT_DISPLAY_TAIL_LINES = 30
SHELL_OUTPUT_DISPLAY_RESERVED_LINES = 3
SHELL_WORKING_STATUS_MARQUEE_FPS = 10.0
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ANSI_OSC_RE = re.compile(r"\x1b\][^\a\x1b]*(?:\a|\x1b\\)")


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
    visible_cap = min(max(1, int(rows)), max(1, int(max_tail_lines or 1)))
    return max(1, visible_cap - max(0, int(reserved_lines or 0)))


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


def _build_tail_output_for_display(text: str, stream: Any, tail_lines: int = SHELL_OUTPUT_DISPLAY_TAIL_LINES) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    trailing_newline = normalized.endswith("\n")
    lines = normalized.split("\n")
    if trailing_newline and lines and lines[-1] == "":
        lines = lines[:-1]
    limit = max(1, int(tail_lines or 1))
    width = _terminal_columns_for_tail_display(stream)
    rendered_lines: List[str] = []
    for line in lines:
        rendered_lines.extend(_wrap_line_for_display(line, width))
    if len(rendered_lines) <= limit:
        return raw
    omitted = len(rendered_lines) - limit
    tail = "\n".join(rendered_lines[-limit:])
    if trailing_newline:
        tail += "\n"
    return _format_omitted_lines_notice(omitted, stream) + tail


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
        err = ""
        displayed_out = ""
        displayed_err = ""
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
                stderr_chunks: List[str] = []
                stream_chunks_lock = threading.Lock()
                allow_realtime_echo = threading.Event()
                allow_realtime_echo.set()
                merge_stderr_for_interactive = (
                    os.environ.get("SMART_SHELL_SEPARATE_STDERR", "").strip().lower()
                    not in {"1", "true", "yes", "on"}
                )

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
                                    _safe_console_write(text_chunk, target, append_newline=False)
                        tail = decoder.decode(b"", final=True)
                        if tail:
                            with stream_chunks_lock:
                                bucket.append(tail)
                            _stop_status_ticker()
                            if allow_realtime_echo.is_set():
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
                        stderr=subprocess.STDOUT
                        if merge_stderr_for_interactive
                        else subprocess.PIPE,
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
                    t_err: Optional[threading.Thread] = None
                    if not merge_stderr_for_interactive:
                        t_err = threading.Thread(
                            target=_stream_and_capture,
                            args=(process.stderr, sys.stderr, stderr_chunks),  # type: ignore[arg-type]
                            daemon=True,
                        )
                        t_err.start()
                    return_code = process.wait()
                    # Stop direct stream echo immediately after process exit to
                    # prevent delayed raw chunks from appearing in later turns.
                    allow_realtime_echo.clear()
                    t_out.join(timeout=1.0)
                    if t_err is not None:
                        t_err.join(timeout=1.0)
                    # Give readers a brief extra window to capture residual bytes
                    # without writing them directly to the terminal.
                    t_out.join(timeout=0.2)
                    if t_err is not None:
                        t_err.join(timeout=0.2)
                    with stream_chunks_lock:
                        out = "".join(stdout_chunks)
                        err = "" if merge_stderr_for_interactive else "".join(stderr_chunks)
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
                run_input = None
                if input_data is not None:
                    run_input = str(input_data).encode("utf-8")
                completed = subprocess.run(
                    command,
                    shell=True,
                    cwd=str(execution_cwd.resolve()),
                    capture_output=True,
                    env=run_env,
                    input=run_input,
                    stdin=subprocess.DEVNULL if run_input is None else subprocess.PIPE,
                )
                return_code = completed.returncode
                raw_stdout = _decode_subprocess_output(completed.stdout)
                out = raw_stdout
                err = _decode_subprocess_output(completed.stderr)
            _stop_status_ticker()

            out = append_shell_merge_output_path(out, return_code, merge_path)
            out_tail_limit = _dynamic_tail_line_limit(sys.stdout)
            err_tail_limit = _dynamic_tail_line_limit(sys.stderr)
            displayed_out = _build_tail_output_for_display(out, sys.stdout, out_tail_limit)
            displayed_err = _build_tail_output_for_display(err, sys.stderr, err_tail_limit)
            displayed_out_plain = _strip_console_color_controls(displayed_out)
            displayed_err_plain = _strip_console_color_controls(displayed_err)
            should_replay_out = True
            should_replay_err = True
            if interactive:
                # Interactive mode already streamed raw output to console.
                # Skip replay when output fully fits within the tail limit and
                # no post-processing changed the displayed text.
                if displayed_out and (_count_output_lines(out) <= out_tail_limit) and (displayed_out == out):
                    should_replay_out = False
                if displayed_err and (_count_output_lines(err) <= err_tail_limit) and (displayed_err == err):
                    should_replay_err = False
            last_rendered_chunk = ""
            replay_out_text = displayed_out_plain if (displayed_out and should_replay_out) else ""
            replay_err_text = displayed_err_plain if (displayed_err and should_replay_err) else ""
            replay_rendered_lines = 0
            if replay_out_text or replay_err_text:
                replay_direct = getattr(agent, "_print_direct_shell_history_output", None)
                if callable(replay_direct):
                    try:
                        replay_rendered_lines = max(
                            0,
                            int(replay_direct(replay_out_text, replay_err_text) or 0),
                        )
                    except Exception:
                        replay_rendered_lines = 0
                else:
                    if replay_out_text:
                        _safe_console_write(_gray_shell_display_text(replay_out_text), sys.stdout, append_newline=False)
                    if replay_err_text:
                        _safe_console_write(_gray_shell_display_text(replay_err_text), sys.stderr, append_newline=False)
                    replay_rendered_lines = max(
                        0,
                        _count_output_lines(replay_out_text) + _count_output_lines(replay_err_text),
                    )
                last_rendered_chunk = replay_err_text if replay_err_text else replay_out_text
            if (not last_rendered_chunk) and interactive:
                if (not should_replay_err) and err:
                    last_rendered_chunk = err
                elif (not should_replay_out) and out:
                    last_rendered_chunk = out
            # Keep next assistant/status lines on a fresh line even when command
            # output does not end with newline. This avoids off-by-one over-clear
            # caused by mixing "正在思考..." into the output's last visual line.
            if last_rendered_chunk and not str(last_rendered_chunk).endswith("\n"):
                _safe_console_write("\n", sys.stdout, append_newline=False)
                if replay_err_text:
                    replay_err_text = str(replay_err_text) + "\n"
                else:
                    replay_out_text = str(replay_out_text) + "\n"
            try:
                agent._last_shell_output_visible_lines = 0
            except Exception:
                pass
            if aborted_by_user:
                try:
                    banner_fn = getattr(agent, "_print_conversation_interrupted_banner", None)
                    if callable(banner_fn):
                        banner_fn()
                except Exception:
                    pass
            base_out: Dict[str, Any] = {
                "output": out,
                "stderr": err,
                "return_code": return_code,
                "interactive": interactive,
                "display_output": replay_out_text,
                "display_stderr": replay_err_text,
                "display_rendered_lines": int(replay_rendered_lines),
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

        combo = f"{out}\n{err}"
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
        invoked.resolve().relative_to(agent.ai_workspace_dir.resolve())
    except ValueError:
        return command
    new_cmd = rewrite_shell_command_script_arg_to_abs(agent, command, invoked.resolve())
    if new_cmd != command:
        print("ℹ️ Shell cwd is the work directory; workspace script path has been expanded to an absolute path.")
    return new_cmd


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
    return agent._is_path_under(invoked, agent.ai_workspace_dir)


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


