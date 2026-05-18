import os
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .console_utils import _decode_subprocess_output, _safe_console_write


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
    command = ensure_absolute_script_for_shell_cwd(agent, command.strip())
    command = tune_7z_output_for_piped_terminal(command)
    policy = agent._get_path_policy()
    decision = policy.can_run_shell_in_workdir(
        is_dependency_install=is_dependency_install_command(command),
        is_ai_workspace_script=is_ai_workspace_script_command(agent, command),
    )
    if not decision.get("allowed", False):
        return {"success": False, "error": decision.get("error", "")}
    agent._load_confirm_allowlist()
    if not confirmed and not agent._shell_command_in_allowlist(command):
        ok = agent._prompt_confirm_yes_no_maybe_always(
            f"⚠️ 确认执行系统命令: {command} ?",
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
        try:
            if interactive:
                import threading
                import codecs

                print("⌨️ shell 交互模式已开启：请按命令提示在终端中输入。")
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

                def _stream_and_capture(pipe: Any, target: Any, bucket: List[str]) -> None:
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    try:
                        while True:
                            if hasattr(pipe, "read1"):
                                chunk = pipe.read1(1)
                            else:
                                chunk = pipe.read(1)
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
                if raw_stdout:
                    _safe_console_write(raw_stdout, sys.stdout)
                if err:
                    _safe_console_write(err, sys.stderr)

            out = append_shell_merge_output_path(out, return_code, merge_path)
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


def action_grep(agent: Any, params: Dict[str, Any]) -> dict:
    from tools.grep_tool import run_grep

    pattern = str(params.get("pattern") or "").strip()
    out_raw = str(params.get("output_path") or params.get("output_file") or "").strip()
    root_raw = str(params.get("root") or "").strip()
    files_raw = params.get("files")
    extensions = params.get("extensions")
    ignore_case = bool(params.get("ignore_case", False))
    multiline = bool(params.get("multiline", False))
    max_matches = int(params.get("max_matches", 100_000))
    max_file_bytes = int(params.get("max_file_bytes", 20 * 1024 * 1024))
    exclude_dir_names = params.get("exclude_dir_names")
    max_workers = params.get("max_workers")

    if not pattern:
        return {"success": False, "error": "grep 缺少 pattern（正则表达式）"}
    if not out_raw:
        return {"success": False, "error": "grep 缺少 output_path（结果输出文件路径）"}

    out_path = agent._resolve_user_path(out_raw)
    policy = agent._get_path_policy()
    decision = policy.can_write_grep_output(out_path)
    if not decision.get("allowed", False):
        return {"success": False, "error": decision.get("error", "")}

    root_path: Optional[Path] = None
    file_list: Optional[List[Path]] = None

    if isinstance(files_raw, list) and len(files_raw) > 0:
        file_list = []
        for fr in files_raw:
            p = agent._resolve_user_path(str(fr).strip())
            if not policy.can_read_for_grep(p):
                return {"success": False, "error": f"禁止检索该路径（超出允许范围）: {p}"}
            if p.is_file():
                file_list.append(p)
        if not file_list:
            return {"success": False, "error": "files 列表中没有有效的现有文件"}
    elif root_raw:
        root_path = agent._resolve_user_path(root_raw)
        if not root_path.is_dir():
            return {"success": False, "error": f"root 不是目录: {root_path}"}
        if not policy.can_read_for_grep(root_path):
            return {"success": False, "error": "禁止在该 root 下检索（超出允许范围）"}
    else:
        return {"success": False, "error": "必须提供 root（目录）或 files（文件路径列表）"}

    ext_arg = None
    if isinstance(extensions, list):
        ext_arg = [str(x) for x in extensions if str(x).strip()]
    excl_arg = None
    if isinstance(exclude_dir_names, list):
        excl_arg = [str(x) for x in exclude_dir_names if str(x).strip()]
    mw = int(max_workers) if max_workers is not None else None

    try:
        result = run_grep(
            root=root_path,
            files=file_list,
            output_file=out_path,
            pattern=pattern,
            extensions=ext_arg,
            ignore_case=ignore_case,
            multiline=multiline,
            max_matches=max_matches,
            max_file_bytes=max_file_bytes,
            exclude_dir_names=excl_arg,
            max_workers=mw,
        )
    except Exception as e:
        return {"success": False, "error": f"grep 执行失败: {e}"}

    if not result.get("success"):
        return dict(result)
    print(f"\n📎 grep: {result.get('message', '完成')} → {result.get('output_path', '')}")
    return {
        "success": True,
        "message": result.get("message", ""),
        "output_path": result.get("output_path"),
        "output_file": result.get("output_path"),
        "match_count": result.get("match_count"),
        "files_with_matches": result.get("files_with_matches"),
        "files_scanned": result.get("files_scanned"),
        "truncated": result.get("truncated", False),
    }


def action_project_context_search(agent: Any, params: Dict[str, Any]) -> dict:
    if not agent._project_context_tool_allowed():
        return {
            "success": False,
            "error": "Default workspace 不支持 project_context_search。请切换到非 Default workspace 后再使用。",
        }

    query = str(params.get("query") or "").strip()
    max_files = params.get("max_files", 12)
    refresh = params.get("refresh", None)
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

    result = agent._project_context_index.search(
        query=query,
        max_files=max_files_i,
        auto_refresh=(True if refresh is None else bool(refresh)) or force_rebuild,
    )
    return result


def register_outputs_from_shell_command(agent: Any, command: str) -> None:
    for pat in (
        r"to_excel\s*\(\s*['\"]([^'\"]+)['\"]",
        r"to_csv\s*\(\s*['\"]([^'\"]+)['\"]",
        r"ExcelWriter\s*\(\s*['\"]([^'\"]+)['\"]",
    ):
        for m in re.finditer(pat, command, re.I):
            agent._try_register_ai_output_literal(m.group(1))


def parse_shell_invoked_script_path(agent: Any, command: str) -> Optional[Path]:
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


def grep_read_path_allowed(agent: Any, path: Path) -> bool:
    return agent._get_path_policy().can_read_for_grep(path)


def grep_output_path_allowed(agent: Any, path: Path) -> bool:
    return bool(agent._get_path_policy().can_write_grep_output(path).get("allowed", False))
