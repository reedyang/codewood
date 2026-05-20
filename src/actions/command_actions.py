import os
import re
import shlex
import shutil
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.console_utils import _decode_subprocess_output, _safe_console_write

SHELL_OUTPUT_DISPLAY_TAIL_LINES = 30
SHELL_OUTPUT_DISPLAY_RESERVED_LINES = 3
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _count_output_lines(text: str) -> int:
    raw = str(text or "")
    if not raw:
        return 0
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    parts = normalized.split("\n")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return len(parts)


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
            "error": 'Windows 上调用 PowerShell 必须使用: powershell -ExecutionPolicy Bypass -Command "<command>"',
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
        return {"success": False, "error": "命令不能为空"}
    manual_confirm_from_ai = bool(getattr(agent, "_manual_confirm_required_shell_once", False))
    if manual_confirm_from_ai:
        agent._manual_confirm_required_shell_once = False
    command = ensure_absolute_script_for_shell_cwd(agent, command.strip())
    command = tune_7z_output_for_piped_terminal(command)
    enforce_res = _enforce_windows_powershell_command_prefix(command)
    if not enforce_res.get("ok", False):
        return {"success": False, "error": str(enforce_res.get("error", "PowerShell 命令格式不符合要求"))}
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
        prompt_text = f"⚠️ 确认执行系统命令: {command} ?"
        if force_manual_confirm_by_policy:
            prompt_text = f"⚠️ AI 判定需手动确认，继续执行前请确认: {command} ?"
        ok = agent._prompt_confirm_yes_no_maybe_always(
            prompt_text,
            offer_always=agent._shell_confirm_should_offer_always(command),
            kind="shell",
            shell_command=command,
        )
        if not ok:
            return {"success": False, "error": "用户取消了操作"}

    import subprocess

    merge_path: Optional[str] = None
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
        interactive = True
        return_code = -1
        out = ""
        err = ""
        displayed_out = ""
        displayed_err = ""
        try:
            if interactive:
                import threading
                import codecs

                stdout_chunks: List[str] = []
                stderr_chunks: List[str] = []
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
                                bucket.append(text_chunk)
                                _safe_console_write(text_chunk, target, append_newline=False)
                        tail = decoder.decode(b"", final=True)
                        if tail:
                            bucket.append(tail)
                            _safe_console_write(tail, target, append_newline=False)
                    except Exception:
                        pass
                    finally:
                        try:
                            pipe.close()
                        except Exception:
                            pass

                try:
                    process = subprocess.Popen(
                        command,
                        shell=True,
                        cwd=str(agent.work_directory.resolve()),
                        env=run_env,
                        stdin=sys.stdin,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT
                        if merge_stderr_for_interactive
                        else subprocess.PIPE,
                        text=False,
                    )
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
                    t_out.join(timeout=1.0)
                    if t_err is not None:
                        t_err.join(timeout=1.0)
                    out = "".join(stdout_chunks)
                    err = "" if merge_stderr_for_interactive else "".join(stderr_chunks)
                finally:
                    _restore_console_after_interactive()
            else:
                run_input = None
                if input_data is not None:
                    run_input = str(input_data).encode("utf-8")
                completed = subprocess.run(
                    command,
                    shell=True,
                    cwd=str(agent.work_directory.resolve()),
                    capture_output=True,
                    env=run_env,
                    input=run_input,
                )
                return_code = completed.returncode
                raw_stdout = _decode_subprocess_output(completed.stdout)
                out = raw_stdout
                err = _decode_subprocess_output(completed.stderr)

            out = append_shell_merge_output_path(out, return_code, merge_path)
            out_tail_limit = _dynamic_tail_line_limit(sys.stdout)
            err_tail_limit = _dynamic_tail_line_limit(sys.stderr)
            displayed_out = _build_tail_output_for_display(out, sys.stdout, out_tail_limit)
            displayed_err = _build_tail_output_for_display(err, sys.stderr, err_tail_limit)
            if displayed_out:
                _safe_console_write(displayed_out, sys.stdout, append_newline=False)
            if displayed_err:
                _safe_console_write(displayed_err, sys.stderr, append_newline=False)
            try:
                agent._register_shell_output_for_auto_hide(displayed_out, displayed_err)
            except Exception:
                pass
            base_out: Dict[str, Any] = {
                "output": out,
                "stderr": err,
                "return_code": return_code,
                "interactive": interactive,
            }
        finally:
            if merge_path:
                try:
                    os.unlink(merge_path)
                except OSError:
                    pass

        if return_code == 0:
            register_outputs_from_shell_command(agent, command)
            if agent._is_workspace_skill_path(agent.work_directory):
                agent._reload_skills_if_workspace_skill_changed([agent.work_directory])
            removed = try_remove_ephemeral_script_after_shell(agent, command)
            if removed:
                agent._last_auto_removed_ephemeral = removed
                return {
                    "success": True,
                    "message": (
                        f"命令执行成功；已自动删除临时脚本 «{removed}»。"
                        "请勿再对该文件执行 delete。"
                    ),
                    "auto_removed_ephemeral_script": removed,
                    **base_out,
                }
            if interactive:
                return {"success": True, "message": "命令执行成功（交互模式）", **base_out}
            return {"success": True, "message": "命令执行成功", **base_out}

        combo = f"{out}\n{err}"
        cmd_l = command.lower()
        is_skillhub_install = ("skillhub_installer.py" in cmd_l) and (" install " in f" {cmd_l} ")
        user_cancelled = ("installation aborted by user." in combo.lower()) or (return_code == 2)
        if is_skillhub_install and user_cancelled:
            return {
                "success": True,
                "cancelled": True,
                "terminal_state": "user_cancelled",
                "message": "安装已由用户取消，流程结束（不应自动重试）。",
                **base_out,
            }
        return {
            "success": False,
            "error": f"命令执行失败，退出码: {return_code}",
            **base_out,
        }

    except Exception as e:
        return {"success": False, "error": f"系统命令执行异常: {str(e)}"}


def action_project_context_search(agent: Any, params: Dict[str, Any]) -> dict:
    if not agent._project_context_tool_allowed():
        return {
            "success": False,
            "error": "Default workspace 不支持 project_context_search。请切换到非 Default workspace 后再使用。",
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
        st["message"] = "project context index 状态"
        return st
    if not query:
        return {"success": False, "error": "project_context_search 缺少 query 参数"}

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


def _unwrap_windows_powershell_command(command: str) -> str:
    s = str(command or "").strip()
    if not s:
        return s
    m = re.match(
        r"(?is)^powershell(?:\.exe)?\s+-ExecutionPolicy\s+Bypass\s+-Command\s+(.+)$",
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
    # Handle escaped quotes passed through wrapper construction.
    payload = payload.replace('\\"', '"')
    return payload.strip()


def parse_shell_invoked_script_path(agent: Any, command: str) -> Optional[Path]:
    s = _unwrap_windows_powershell_command(command.strip())
    if not s:
        return None
    if s.lower().startswith("call "):
        s = s[5:].strip()
    try:
        parts = shlex.split(s, posix=os.name != "nt")
    except ValueError:
        parts = s.split()
    if not parts:
        return None
    base0 = parts[0].replace("\\", "/").split("/")[-1].lower().rstrip(".exe")
    if len(parts) >= 3 and base0 == "cmd" and parts[1].lower() in ("/c", "/k"):
        return parse_shell_invoked_script_path(agent, " ".join(parts[2:]))
    exe = base0
    if exe in ("python", "pythonw", "py") and len(parts) >= 2:
        i = 1
        while i < len(parts):
            t = parts[i].strip('"').strip("'")
            if t in ("-c", "-m"):
                return None
            if t.startswith("-") and len(t) > 1:
                i += 1
                continue
            break
        if i >= len(parts):
            return None
        tok = parts[i].strip('"').strip("'")
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
    tok = parts[0].strip('"').strip("'")
    low = tok.lower()
    if low.endswith((".py", ".ps1", ".bat", ".cmd")):
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
    return None


def rewrite_shell_command_script_arg_to_abs(agent: Any, command: str, resolved: Path) -> str:
    import subprocess

    s = command.strip()
    call_prefix = ""
    if s.lower().startswith("call "):
        call_prefix = "call "
        s = s[5:].strip()
    try:
        parts = shlex.split(s, posix=os.name != "nt")
    except ValueError:
        return command
    if not parts:
        return command
    base0 = parts[0].replace("\\", "/").split("/")[-1].lower().rstrip(".exe")
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
        print("ℹ️ shell cwd 为工作目录，已将 workspace 内脚本展开为绝对路径执行。")
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
        print(f"ℹ️ 已为 7z 命令启用兼容输出参数: {' '.join(appended)}")
    return tuned


def parse_shell_invoked_executable(agent: Any, command: str) -> Optional[Path]:
    s = command.strip()
    if not s:
        return None
    if s.lower().startswith("call "):
        s = s[5:].strip()
    try:
        parts = shlex.split(s, posix=os.name != "nt")
    except ValueError:
        parts = s.split()
    if not parts:
        return None
    base0 = parts[0].replace("\\", "/").split("/")[-1].lower().rstrip(".exe")
    token = parts[2] if len(parts) >= 3 and base0 == "cmd" and parts[1].lower() in ("/c", "/k") else parts[0]
    token = token.strip('"').strip("'")
    if token.startswith(".\\") or token.startswith("./"):
        token = token[2:]
    p = Path(token)
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
            print(f"🗑️ 已自动删除本会话创建的临时脚本: {name}")
            return name
    except OSError as e:
        print(f"⚠️ 自动删除临时脚本失败 ({invoked}): {e}")
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
    marker = "【附加输出（shell merge file）】"
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


