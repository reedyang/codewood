import json
import logging
import os
import platform
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class McpError(Exception):
    pass


def _parse_content_length(header_blob: bytes) -> int:
    text = header_blob.decode("utf-8", errors="replace").replace("\r\n", "\n")
    lines = text.split("\n")
    for line in lines:
        if line.lower().startswith("content-length:"):
            raw = line.split(":", 1)[1].strip()
            return int(raw)
    raise McpError("MCP 响应缺少 Content-Length 头")


@dataclass
class McpServerClient:
    name: str
    config: Dict[str, Any]
    process: Optional[subprocess.Popen] = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    next_id: int = 1
    initialized: bool = False
    negotiated_protocol: Optional[str] = None
    response_queue: Any = field(default_factory=queue.Queue)
    stderr_lines: List[str] = field(default_factory=list)
    stdout_reader_started: bool = False
    stderr_reader_started: bool = False
    stop_event: threading.Event = field(default_factory=threading.Event)
    _stdout_buffer: bytes = b""
    wire_mode: str = "framed_crlf"  # framed_crlf / framed_lf / line
    process_started_ts: float = 0.0
    _hs_last_raw_log_ts: float = 0.0

    def _handshake_debug_enabled(self) -> bool:
        """Enable handshake debug by env or per-server config."""
        env_flag = str(os.environ.get("SMART_SHELL_MCP_HANDSHAKE_DEBUG", "")).strip().lower()
        if env_flag in ("1", "true", "yes", "on"):
            return True
        return bool(self.config.get("debug_handshake", False))

    @staticmethod
    def _safe_msg_summary(msg: Dict[str, Any]) -> Dict[str, Any]:
        """Only keep non-sensitive MCP envelope keys."""
        return {
            "id": msg.get("id"),
            "method": msg.get("method"),
            "has_error": "error" in msg,
            "error": str(msg.get("error"))[:240] if "error" in msg else "",
        }

    def _trace_handshake_msg(self, event: str, msg: Dict[str, Any]) -> None:
        if not self._handshake_debug_enabled():
            return
        try:
            summary = self._safe_msg_summary(msg)
            logging.getLogger("smart_shell.mcp").info(
                f"[HSDBG] server={self.name} event={event} summary={json.dumps(summary, ensure_ascii=False)}"
            )
        except Exception:
            pass

    def _trace_handshake_raw(self, note: str, payload: bytes) -> None:
        """Low-risk raw channel diagnostics: never log full payload/body."""
        if not self._handshake_debug_enabled():
            return
        now = time.time()
        # Rate-limit raw logs to avoid flooding.
        if now - float(self._hs_last_raw_log_ts or 0.0) < 2.0:
            return
        self._hs_last_raw_log_ts = now
        try:
            sample = payload[:80].decode("utf-8", errors="replace").replace("\r", "\\r").replace("\n", "\\n")
            has_len = b"content-length:" in payload.lower()
            has_sep = (b"\r\n\r\n" in payload) or (b"\n\n" in payload)
            logging.getLogger("smart_shell.mcp").info(
                f"[HSDBG] server={self.name} event=raw_stdout note={note} bytes={len(payload)} has_len={has_len} has_sep={has_sep} sample={sample}"
            )
        except Exception:
            pass

    def _build_env(self) -> Dict[str, str]:
        env = dict(**os.environ)
        extra = self.config.get("env", {})
        if isinstance(extra, dict):
            for k, v in extra.items():
                env[str(k)] = str(v)
        return env

    @staticmethod
    def _resolve_command(command_str: str) -> str:
        """
        Resolve command path with Windows-specific preference for .cmd/.exe over .ps1.
        PowerShell wrapper scripts (e.g. npx.ps1) may break MCP stdio framing.
        """
        cmd = str(command_str or "").strip()
        if not cmd:
            return cmd
        resolved = shutil.which(cmd) or cmd
        if platform.system().lower().startswith("win"):
            lower = cmd.lower()
            if "." not in os.path.basename(lower):
                preferred = (
                    shutil.which(f"{cmd}.cmd")
                    or shutil.which(f"{cmd}.exe")
                    or shutil.which(f"{cmd}.bat")
                )
                if preferred:
                    return preferred
            if str(resolved).lower().endswith(".ps1"):
                base, _ = os.path.splitext(str(resolved))
                for ext in (".cmd", ".exe", ".bat"):
                    cand = base + ext
                    if os.path.exists(cand):
                        return cand
        return resolved

    @staticmethod
    def _normalize_npx_args(args: List[Any]) -> List[str]:
        """
        Keep npx args as-is.
        NOTE: inserting '--' may break some package CLIs (including mcp servers)
        by turning option flags into positional arguments.
        """
        return [str(a) for a in (args or [])]

    def _start_if_needed(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        if "url" in self.config:
            raise McpError("当前最小实现仅支持 stdio MCP server，暂不支持 url 远端传输")
        command = self.config.get("command")
        if not command:
            raise McpError("MCP server 缺少 command")
        command_str = str(command).strip()
        resolved_command = self._resolve_command(command_str)
        if not Path(resolved_command).is_absolute() and shutil.which(command_str) is None:
            raise McpError(f"未找到可执行文件: {command_str}")
        args = self.config.get("args", [])
        if not isinstance(args, list):
            raise McpError("MCP server args 必须是数组")
        fallback_argv = [resolved_command, *self._normalize_npx_args(args)]
        spawn_argv = fallback_argv
        if self._handshake_debug_enabled():
            try:
                exec_path = str(spawn_argv[0]) if spawn_argv else ""
                logging.getLogger("smart_shell.mcp").info(
                    f"[HSDBG] server={self.name} event=spawn argv={{\"spawn_exec\": {json.dumps(exec_path, ensure_ascii=False)}, \"arg_count\": {max(0, len(spawn_argv) - 1)}}}"
                )
            except Exception:
                pass
        self.process = subprocess.Popen(
            spawn_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._build_env(),
            text=False,
        )
        self.process_started_ts = time.time()
        self.initialized = False
        self.negotiated_protocol = None
        self.stop_event.clear()
        self.response_queue = queue.Queue()
        self.stderr_lines = []
        self._stdout_buffer = b""
        self._start_background_readers()

    def _start_background_readers(self) -> None:
        if self.process is None:
            return
        if not self.stdout_reader_started and self.process.stdout is not None:
            self.stdout_reader_started = True
            t = threading.Thread(target=self._stdout_reader_loop, daemon=True)
            t.start()
        if not self.stderr_reader_started and self.process.stderr is not None:
            self.stderr_reader_started = True
            t = threading.Thread(target=self._stderr_reader_loop, daemon=True)
            t.start()

    def _stdout_reader_loop(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        s = self.process.stdout
        try:
            while not self.stop_event.is_set():
                if self.wire_mode == "line":
                    line = s.readline()
                    if not line:
                        break
                    txt = line.decode("utf-8", errors="replace").strip()
                    if not txt:
                        continue
                    try:
                        msg = json.loads(txt)
                        if isinstance(msg, dict):
                            self._trace_handshake_msg("rx_line", msg)
                            self.response_queue.put(msg)
                    except Exception:
                        continue
                else:
                    # Read byte-by-byte to avoid buffered blocking waiting for large chunks.
                    chunk = s.read(1)
                    if not chunk:
                        break
                    self._stdout_buffer += chunk
                    if self._handshake_debug_enabled() and len(self._stdout_buffer) >= 16:
                        # Diagnostic snapshot of unread buffer shape (no full payload dump).
                        self._trace_handshake_raw("buffer_snapshot", self._stdout_buffer)
                    while True:
                        sep = b"\r\n\r\n"
                        sep_idx = self._stdout_buffer.find(sep)
                        if sep_idx < 0:
                            sep = b"\n\n"
                            sep_idx = self._stdout_buffer.find(sep)
                        if sep_idx >= 0:
                            header = self._stdout_buffer[:sep_idx]
                            if self._handshake_debug_enabled():
                                self._trace_handshake_raw("frame_header", header)
                            try:
                                content_length = _parse_content_length(header)
                            except Exception:
                                # Drop garbage before next possible frame.
                                self._stdout_buffer = self._stdout_buffer[sep_idx + len(sep) :]
                                continue
                            frame_end = sep_idx + len(sep) + content_length
                            if len(self._stdout_buffer) < frame_end:
                                break
                            body = self._stdout_buffer[sep_idx + len(sep) : frame_end]
                            self._stdout_buffer = self._stdout_buffer[frame_end:]
                            try:
                                msg = json.loads(body.decode("utf-8", errors="replace"))
                                if isinstance(msg, dict):
                                    self._trace_handshake_msg("rx_framed", msg)
                                    self.response_queue.put(msg)
                            except Exception:
                                continue
                            continue

                        # Compatibility fallback: some servers may emit newline-delimited JSON
                        # without Content-Length framing. To avoid breaking normal framed streams,
                        # only consume a line when current buffer clearly starts with a JSON object.
                        stripped = self._stdout_buffer.lstrip()
                        if not stripped.startswith(b"{"):
                            break
                        lf = self._stdout_buffer.find(b"\n")
                        if lf < 0:
                            break
                        line = self._stdout_buffer[:lf].strip()
                        self._stdout_buffer = self._stdout_buffer[lf + 1 :]
                        if not line:
                            continue
                        if line.startswith(b"{") and line.endswith(b"}"):
                            try:
                                msg = json.loads(line.decode("utf-8", errors="replace"))
                                if isinstance(msg, dict):
                                    self._trace_handshake_msg("rx_jsonl", msg)
                                    self.response_queue.put(msg)
                            except Exception:
                                pass
                        continue
        finally:
            self.stop_event.set()

    def _stderr_reader_loop(self) -> None:
        if self.process is None or self.process.stderr is None:
            return
        s = self.process.stderr
        buf = b""
        try:
            while not self.stop_event.is_set():
                chunk = s.read(1)
                if not chunk:
                    break
                buf += chunk
                if chunk in (b"\n", b"\r"):
                    txt = buf.decode("utf-8", errors="replace").strip()
                    buf = b""
                    if txt:
                        self.stderr_lines.append(txt)
                        if len(self.stderr_lines) > 200:
                            self.stderr_lines = self.stderr_lines[-200:]
            # flush trailing partial line
            if buf:
                txt = buf.decode("utf-8", errors="replace").strip()
                if txt:
                    self.stderr_lines.append(txt)
                    if len(self.stderr_lines) > 200:
                        self.stderr_lines = self.stderr_lines[-200:]
        finally:
            # Do not stop stdout reader when stderr stream ends.
            # Some MCP servers may close stderr early while keeping stdout active.
            if self._handshake_debug_enabled():
                try:
                    tail = self._tail_stderr(6)
                    if tail:
                        logging.getLogger("smart_shell.mcp").info(
                            f"[HSDBG] server={self.name} event=stderr_tail tail={json.dumps(tail, ensure_ascii=False)}"
                        )
                except Exception:
                    pass

    def _tail_stderr(self, n: int = 8) -> str:
        if not self.stderr_lines:
            return ""
        return " | ".join(self.stderr_lines[-n:])

    def _write_message(self, payload: Dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise McpError("MCP server 未启动")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self.wire_mode == "line":
            self.process.stdin.write(body + b"\n")
        elif self.wire_mode == "framed_lf":
            head = f"Content-Length: {len(body)}\n\n".encode("ascii")
            self.process.stdin.write(head + body)
        else:
            head = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            self.process.stdin.write(head + body)
        self.process.stdin.flush()

    def _request(self, method: str, params: Optional[Dict[str, Any]], timeout_s: float) -> Dict[str, Any]:
        with self.lock:
            self._start_if_needed()
            # Some npx-based MCP servers need extra warm-up time before first initialize.
            if method == "initialize":
                grace = self._startup_grace_seconds()
                elapsed = time.time() - float(self.process_started_ts or 0.0)
                if grace > elapsed:
                    time.sleep(grace - elapsed)
            req_id = self.next_id
            self.next_id += 1
            req: Dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
            }
            if params is not None:
                req["params"] = params
            self._trace_handshake_msg("tx_request", req)
            if self.process is None or self.process.poll() is not None:
                err_tail = self._tail_stderr()
                suffix = f"；stderr: {err_tail}" if err_tail else ""
                code = self.process.returncode if self.process is not None else "unknown"
                raise McpError(f"MCP server 已退出(method={method}, code={code}){suffix}")
            try:
                self._write_message(req)
            except OSError as e:
                err_tail = self._tail_stderr()
                suffix = f"；stderr: {err_tail}" if err_tail else ""
                raise McpError(f"MCP 写入失败(method={method}, id={req_id}): {e}{suffix}")
            deadline = time.time() + max(0.2, timeout_s)
            while True:
                left = deadline - time.time()
                if left <= 0:
                    err_tail = self._tail_stderr()
                    suffix = f"；stderr: {err_tail}" if err_tail else ""
                    raise McpError(f"MCP 请求超时(method={method}, id={req_id}){suffix}")
                if self.process is not None and self.process.poll() is not None:
                    err_tail = self._tail_stderr()
                    suffix = f"；stderr: {err_tail}" if err_tail else ""
                    raise McpError(f"MCP server 已退出(method={method}, code={self.process.returncode}){suffix}")
                try:
                    msg = self.response_queue.get(timeout=min(0.5, left))
                except queue.Empty:
                    continue
                # notifications / requests from server are ignored in minimal client
                msg_id = msg.get("id")
                # Be tolerant to JSON-RPC id type differences (e.g. "2" vs 2).
                if not (msg_id == req_id or str(msg_id) == str(req_id)):
                    continue
                if "error" in msg:
                    err_tail = self._tail_stderr()
                    suffix = f"；stderr: {err_tail}" if err_tail else ""
                    raise McpError(f"{msg.get('error')}{suffix}")
                return msg.get("result", {})

    def _startup_grace_seconds(self) -> float:
        # Do not delay initialize for npx-based MCP servers.
        # Some servers enforce a short idle timeout before first request.
        return 0.0

    def initialize(self, timeout_s: float = 8.0) -> None:
        if self.initialized:
            return
        # Align with modern MCP clients (e.g. Playwright's bundled MCP client).
        protocol_candidates = ["2025-11-25", "2024-11-05", "2024-10-07", "2024-06-01"]
        last_error: Optional[str] = None
        deadline = time.time() + max(0.5, timeout_s)
        # Avoid over-slicing timeout budget for slow-to-start MCP servers (e.g. npx + browser startup).
        # Keep each initialize attempt sufficiently long while still respecting overall deadline.
        per_try_floor = max(3.0, timeout_s * 0.8)
        # Newer MCP SDK stacks (including current Playwright bundle) use JSONL over stdio.
        # Keep framed modes as fallback for older servers.
        wire_modes = ["line", "framed_crlf", "framed_lf"]
        for mode in wire_modes:
            self.wire_mode = mode
            for pv in protocol_candidates:
            # Retry initialize multiple times on the SAME process first.
            # npx startup chain may be slow; restarting too eagerly can starve readiness.
                for attempt in range(4):
                    left = deadline - time.time()
                    if left <= 0:
                        break
                    try:
                        self._request(
                            "initialize",
                            {
                                "protocolVersion": pv,
                                "clientInfo": {"name": "smart-shell", "version": "0.1.0"},
                                "capabilities": {},
                            },
                            timeout_s=min(left, per_try_floor),
                        )
                        self.negotiated_protocol = pv
                        last_error = None
                        break
                    except Exception as e:
                        last_error = str(e)
                        is_exit = "MCP server 已退出" in last_error or "MCP 写入失败" in last_error
                        if is_exit:
                            # Recreate process only when process is actually gone/broken.
                            self._shutdown_unlocked()
                            self._start_if_needed()
                            min_retry_window = max(5.0, min(12.0, timeout_s * 0.4))
                            deadline = max(deadline, time.time() + min_retry_window)
                        else:
                            # Keep process and retry after a short delay.
                            time.sleep(1.2)
                if last_error is None:
                    break
            if last_error is None:
                break
        if last_error is not None:
            raise McpError(f"initialize 失败（协议回退后仍失败）: {last_error}")
        # Best-effort initialized notification.
        try:
            with self.lock:
                msg = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
                self._trace_handshake_msg("tx_notify_initialized", msg)
                self._write_message(msg)
        except Exception:
            pass
        self.initialized = True

    def _shutdown_unlocked(self) -> None:
        self.stop_event.set()
        p = self.process
        self.process = None
        self.initialized = False
        self.stdout_reader_started = False
        self.stderr_reader_started = False
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
        except Exception:
            pass
        try:
            if p.stdout:
                p.stdout.close()
        except Exception:
            pass
        try:
            if p.stderr:
                p.stderr.close()
        except Exception:
            pass
        try:
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=1.0)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def list_tools(self, timeout_s: float = 8.0) -> List[Dict[str, Any]]:
        self.initialize(timeout_s=timeout_s)
        result = self._request("tools/list", {}, timeout_s=timeout_s)
        tools = result.get("tools", [])
        return tools if isinstance(tools, list) else []

    def call_tool(self, tool_name: str, arguments: Dict[str, Any], timeout_s: float = 20.0) -> Dict[str, Any]:
        self.initialize(timeout_s=timeout_s)
        return self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
            timeout_s=timeout_s,
        )


class McpManager:
    def __init__(self, config_dir: Path, mcp_config: Dict[str, Any], workspace_dir: Optional[Path] = None):
        self.config_dir = Path(config_dir)
        self.workspace_dir = Path(workspace_dir) if workspace_dir else self.config_dir / "workspace"
        self.mcp_config = mcp_config or {}
        self._clients: Dict[str, McpServerClient] = {}
        self._tools_cache: Dict[str, Dict[str, Any]] = {}
        self._status: Dict[str, Dict[str, Any]] = {}
        self._active_ops: Dict[str, int] = {}
        self._preload_thread: Optional[threading.Thread] = None
        self._preload_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._logger = self._build_logger()
        self._recent_logs: "deque[str]" = deque(maxlen=200)
        self._init_server_status()

    def _build_logger(self) -> logging.Logger:
        logs_dir = self.workspace_dir / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        logger = logging.getLogger("smart_shell.mcp")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            log_file = logs_dir / "mcp_manager.log"
            handler = logging.FileHandler(log_file, encoding="utf-8")
            fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            handler.setFormatter(fmt)
            logger.addHandler(handler)
        return logger

    def _init_server_status(self) -> None:
        servers = self.mcp_config.get("mcpServers", {})
        if not isinstance(servers, dict):
            return
        now = time.time()
        with self._status_lock:
            for name in servers.keys():
                self._status[name] = {
                    "state": "pending",
                    "last_error": "",
                    "last_updated_ts": now,
                    "loading_since_ts": 0.0,
                    "tool_count": 0,
                    "source": "",
                    "is_cached": False,
                    "failure_type": "",
                    "suggestion": "",
                }
                self._active_ops[str(name)] = 0

    def _mark_op(self, server: str, delta: int) -> None:
        with self._status_lock:
            cur = int(self._active_ops.get(server, 0))
            nxt = cur + int(delta)
            if nxt < 0:
                nxt = 0
            self._active_ops[server] = nxt

    def _force_reset_active_ops(self, server: str) -> None:
        with self._status_lock:
            self._active_ops[server] = 0

    def _set_status(
        self,
        server: str,
        state: str,
        *,
        last_error: str = "",
        tool_count: Optional[int] = None,
        source: Optional[str] = None,
        is_cached: Optional[bool] = None,
        failure_type: Optional[str] = None,
        suggestion: Optional[str] = None,
    ) -> None:
        with self._status_lock:
            st = self._status.setdefault(
                server,
                {
                    "state": "pending",
                    "last_error": "",
                    "last_updated_ts": time.time(),
                    "loading_since_ts": 0.0,
                    "tool_count": 0,
                    "source": "",
                    "is_cached": False,
                    "failure_type": "",
                    "suggestion": "",
                },
            )
            st["state"] = state
            st["last_error"] = last_error
            st["last_updated_ts"] = time.time()
            if state == "loading":
                st["loading_since_ts"] = st["last_updated_ts"]
            elif state in ("success", "failed", "pending", "skipped"):
                st["loading_since_ts"] = 0.0
            if tool_count is not None:
                st["tool_count"] = int(tool_count)
            if source is not None:
                st["source"] = source
            if is_cached is not None:
                st["is_cached"] = bool(is_cached)
            if failure_type is not None:
                st["failure_type"] = failure_type
            if suggestion is not None:
                st["suggestion"] = suggestion

    def _classify_failure(self, server: str, err: str) -> Tuple[str, str]:
        e = (err or "").lower()
        conf = self._server_conf(server)
        if "url" in conf or "暂不支持 url" in e:
            return (
                "unsupported",
                "当前实现暂不支持 URL transport；可改用 stdio server，或在 mcp.json 设置 skip_preload=true。",
            )
        if "winerror 2" in e or "cannot find the file specified" in e or "no such file or directory" in e:
            cmd = str(conf.get("command", "")).strip() or "<unknown>"
            return (
                "missing_dependency",
                f"未找到可执行文件 `{cmd}`；请安装依赖并确保 PATH 可见，或在 mcp.json 设置绝对路径。",
            )
        return (
            "connect_failed",
            "连接/握手失败：可调大 timeout、执行 mcp_reconnect，或在 mcp.json 设置 skip_preload=true 跳过启动预加载。",
        )

    def _log(self, level: str, message: str) -> None:
        line = f"[{level}] {message}"
        self._recent_logs.append(line)
        if level == "WARNING":
            self._logger.warning(message)
        elif level == "ERROR":
            self._logger.error(message)
        else:
            self._logger.info(message)

    def _server_conf(self, server: str) -> Dict[str, Any]:
        servers = self.mcp_config.get("mcpServers", {})
        if not isinstance(servers, dict) or server not in servers:
            raise McpError(f"未配置 MCP server: {server}")
        conf = servers[server]
        if not isinstance(conf, dict):
            raise McpError(f"MCP server 配置无效: {server}")
        return conf

    def _effective_timeout(self, server: str, requested_timeout: float) -> float:
        """Apply server-specific timeout floor to reduce false startup timeouts."""
        t = max(0.5, float(requested_timeout))
        conf = self._server_conf(server)
        cmd = str(conf.get("command", "")).strip().lower()
        args = conf.get("args", [])
        args_l = [str(a).strip().lower() for a in args] if isinstance(args, list) else []
        if cmd.endswith("npx") or cmd == "npx":
            # npx cold start can be slow; @latest is slower.
            if any("@latest" in a for a in args_l):
                return max(t, 45.0)
            return max(t, 25.0)
        return t

    def _client(self, server: str) -> McpServerClient:
        if server not in self._clients:
            self._clients[server] = McpServerClient(name=server, config=self._server_conf(server))
        return self._clients[server]

    def _try_cli_schemas_fallback(self, server: str, timeout_s: float) -> Optional[List[Dict[str, Any]]]:
        """
        Fallback for DevHelper-like CLIs that support `mcp schemas`.
        Trigger only when configured as `... mcp start`.
        """
        conf = self._server_conf(server)
        command = str(conf.get("command", "")).strip()
        args = conf.get("args", [])
        if not command or not isinstance(args, list):
            return None
        norm = [str(a).strip().lower() for a in args]
        if len(norm) < 2 or norm[0] != "mcp" or norm[1] != "start":
            return None
        try:
            cli_args = [str(a) for a in args]
            cli_args[1] = "schemas"
            proc = subprocess.run(
                [command, *cli_args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(3.0, timeout_s),
            )
            if proc.returncode != 0:
                return None
            payload = json.loads(proc.stdout or "{}")
            tools = payload.get("tools", [])
            if isinstance(tools, list):
                return tools
        except Exception:
            return None
        return None

    def list_tools(self, server: str, timeout_s: float = 8.0, use_cache: bool = True) -> Tuple[List[Dict[str, Any]], bool]:
        if use_cache and server in self._tools_cache:
            cached = self._tools_cache[server]
            tools = cached.get("tools", [])
            self._set_status(
                server,
                "success",
                tool_count=len(tools) if isinstance(tools, list) else 0,
                source=str(cached.get("source", "cache")),
                is_cached=True,
                failure_type="",
                suggestion="",
            )
            return tools, True
        if use_cache and server not in self._tools_cache:
            # Fast return for cache-only query mode; do not block on live handshake.
            self._set_status(
                server,
                "loading",
                last_error="cache_miss",
                failure_type="connect_failed",
                suggestion="缓存未就绪；请先查看 mcp_status，或用 use_cache=false 主动拉取。",
            )
            raise McpError("缓存未命中（use_cache=true），已跳过实时连接")
        self._mark_op(server, +1)
        self._set_status(server, "loading")
        timeout_s = self._effective_timeout(server, timeout_s)
        self._log("INFO", f"start list_tools server={server}, timeout={timeout_s:.1f}, use_cache={use_cache}")
        last_error: Optional[str] = None
        for _ in range(2):
            try:
                tools = self._client(server).list_tools(timeout_s=timeout_s)
                self._tools_cache[server] = {"tools": tools, "ts": time.time(), "source": "stdio"}
                self._set_status(
                    server,
                    "success",
                    tool_count=len(tools) if isinstance(tools, list) else 0,
                    source="stdio",
                    is_cached=False,
                    failure_type="",
                    suggestion="",
                )
                self._log("INFO", f"list_tools success server={server}, source=stdio, tool_count={len(tools) if isinstance(tools, list) else 0}")
                self._mark_op(server, -1)
                return tools, False
            except Exception as e:
                last_error = str(e)
                self._log("WARNING", f"list_tools failed for {server}: {last_error}")
                # force reconnect once
                if server in self._clients:
                    try:
                        self._clients[server]._shutdown_unlocked()
                    except Exception:
                        pass
        fallback_tools = self._try_cli_schemas_fallback(server, timeout_s=timeout_s)
        if fallback_tools is not None:
            self._tools_cache[server] = {"tools": fallback_tools, "ts": time.time(), "source": "cli_schemas"}
            self._set_status(
                server,
                "success",
                tool_count=len(fallback_tools),
                source="cli_schemas",
                is_cached=False,
                failure_type="",
                suggestion="",
            )
            self._log("INFO", f"list_tools success server={server}, source=cli_schemas, tool_count={len(fallback_tools)}")
            self._mark_op(server, -1)
            return fallback_tools, False
        ft, sugg = self._classify_failure(server, last_error or "tools/list 失败")
        self._set_status(
            server,
            "failed",
            last_error=last_error or "tools/list 失败",
            failure_type=ft,
            suggestion=sugg,
        )
        self._mark_op(server, -1)
        raise McpError(last_error or "tools/list 失败")

    def call_tool(self, server: str, tool_name: str, arguments: Dict[str, Any], timeout_s: float = 20.0) -> Dict[str, Any]:
        # Refresh cache on first tool call if absent; failure is non-fatal.
        if server not in self._tools_cache:
            try:
                self.list_tools(server, timeout_s=min(8.0, timeout_s), use_cache=False)
            except Exception:
                pass
        self._set_status(server, "loading")
        timeout_s = self._effective_timeout(server, timeout_s)
        try:
            res = self._client(server).call_tool(tool_name, arguments, timeout_s=timeout_s)
            self._set_status(server, "success", failure_type="", suggestion="")
            return res
        except Exception as e:
            ft, sugg = self._classify_failure(server, str(e))
            self._set_status(server, "failed", last_error=str(e), failure_type=ft, suggestion=sugg)
            self._log("WARNING", f"call_tool failed for {server}/{tool_name}: {e}")
            raise

    def preload_all_async(self, timeout_s: float = 12.0, force: bool = False) -> bool:
        with self._preload_lock:
            if (
                self._preload_thread is not None
                and self._preload_thread.is_alive()
                and not force
            ):
                return False
            self._preload_thread = threading.Thread(
                target=self._preload_worker,
                args=(timeout_s, force),
                daemon=True,
            )
            self._preload_thread.start()
            return True

    def _preload_worker(self, timeout_s: float, force: bool) -> None:
        servers = self.mcp_config.get("mcpServers", {})
        if not isinstance(servers, dict):
            return
        self._log("INFO", f"preload start total_servers={len(servers)} timeout={timeout_s}")

        def _one(server: str, conf: Dict[str, Any]) -> None:
            if isinstance(conf, dict) and bool(conf.get("skip_preload", False)):
                self._set_status(
                    str(server),
                    "skipped",
                    last_error="skip_preload=true",
                    failure_type="",
                    suggestion="该 server 已配置 skip_preload=true；如需预加载请改为 false。",
                )
                self._log("INFO", f"preload skip server={server} reason=skip_preload")
                return
            if not force and server in self._tools_cache:
                self._set_status(server, "success", is_cached=True)
                self._log("INFO", f"preload hit_cache server={server}")
                return
            try:
                self.list_tools(str(server), timeout_s=timeout_s, use_cache=False)
            except Exception as e:
                self._log("WARNING", f"preload failed for {server}: {e}")
            return

        threads: List[threading.Thread] = []
        for server, conf in servers.items():
            t = threading.Thread(target=_one, args=(str(server), conf if isinstance(conf, dict) else {}), daemon=True)
            t.start()
            threads.append(t)
        # Join with soft upper bound; avoid hanging forever.
        deadline = time.time() + max(5.0, timeout_s + 5.0)
        for t in threads:
            left = deadline - time.time()
            if left <= 0:
                break
            t.join(timeout=left)
        self._log("INFO", "preload end")

    def get_recent_logs(self, limit: int = 20) -> List[str]:
        if limit <= 0:
            return []
        if limit > 200:
            limit = 200
        logs = list(self._recent_logs)
        return logs[-limit:]

    def get_status(self, log_limit: int = 20) -> Dict[str, Any]:
        """
        Cache-only status snapshot.
        This method never performs MCP I/O and only reads in-memory state/cache.
        """
        servers = self.mcp_config.get("mcpServers", {})
        total = len(servers) if isinstance(servers, dict) else 0
        with self._status_lock:
            items = {k: dict(v) for k, v in self._status.items()}
            active_snapshot = {k: int(v) for k, v in self._active_ops.items()}
        # Resolve stale loading states to failed to avoid endless "loading".
        now = time.time()
        stale_threshold_s = 90.0
        hard_loading_timeout_s = 75.0
        no_active_threshold_s = 8.0
        for name, v in items.items():
            active_ops = int(active_snapshot.get(name, 0))
            v["active_ops"] = active_ops
            # If tools are already cached, reflect that in status view even during transient loading.
            cached = self._tools_cache.get(name)
            if isinstance(cached, dict):
                cached_tools = cached.get("tools", [])
                cached_count = len(cached_tools) if isinstance(cached_tools, list) else 0
                if cached_count > 0:
                    if int(v.get("tool_count", 0) or 0) <= 0:
                        v["tool_count"] = cached_count
                    if not str(v.get("source", "") or "").strip():
                        v["source"] = str(cached.get("source", "cache"))
                    # Self-heal stale loading state when cache already exists and no op is running.
                    if v.get("state") == "loading" and active_ops <= 0:
                        self._set_status(
                            name,
                            "success",
                            tool_count=cached_count,
                            source=str(cached.get("source", "cache")),
                            failure_type="",
                            suggestion="",
                        )
            if v.get("state") == "loading":
                since = float(v.get("loading_since_ts", 0.0) or 0.0)
                elapsed = (now - since) if since > 0 else 0.0
                if since > 0 and elapsed > hard_loading_timeout_s:
                    self._set_status(
                        name,
                        "failed",
                        last_error=f"loading_hard_timeout>{int(hard_loading_timeout_s)}s",
                        failure_type="connect_failed",
                        suggestion="加载长时间未完成，已自动回收状态；请执行 mcp_reconnect 或 mcp_status_refresh(force=true) 重试。",
                    )
                    self._force_reset_active_ops(name)
                elif since > 0 and active_ops <= 0 and elapsed > no_active_threshold_s:
                    self._set_status(
                        name,
                        "failed",
                        last_error="loading_state_stuck_without_active_op",
                        failure_type="connect_failed",
                        suggestion="检测到无活跃加载任务但状态仍为 loading；请执行 mcp_status_refresh(force=true) 或 mcp_reconnect。",
                    )
                elif since > 0 and elapsed > stale_threshold_s:
                    self._set_status(
                        name,
                        "failed",
                        last_error=f"loading_timeout>{int(stale_threshold_s)}s",
                        failure_type="connect_failed",
                        suggestion="加载超时；请执行 mcp_reconnect 或 mcp_status_refresh(force=true) 重试。",
                    )
        with self._status_lock:
            items = {k: dict(v) for k, v in self._status.items()}
            active_snapshot = {k: int(v) for k, v in self._active_ops.items()}
            for name, v in items.items():
                v["active_ops"] = int(active_snapshot.get(name, 0))
        loaded = sum(1 for v in items.values() if v.get("state") in ("success", "failed", "skipped"))
        success = sum(1 for v in items.values() if v.get("state") == "success")
        failed = sum(1 for v in items.values() if v.get("state") == "failed")
        loading_servers = [k for k, v in items.items() if v.get("state") == "loading"]
        fix_suggestions: Dict[str, List[str]] = {
            "unsupported": [],
            "missing_dependency": [],
            "connect_failed": [],
        }
        for name, v in items.items():
            if v.get("state") != "failed":
                continue
            ft = str(v.get("failure_type", "connect_failed") or "connect_failed")
            msg = f"{name}: {v.get('suggestion', '')}".strip()
            if ft not in fix_suggestions:
                ft = "connect_failed"
            fix_suggestions[ft].append(msg)
        return {
            "total": total,
            "loaded": loaded,
            "success": success,
            "failed": failed,
            "all_loaded": total > 0 and loaded >= total,
            "loading_servers": loading_servers,
            "loading_count": len(loading_servers),
            "recent_logs": self.get_recent_logs(log_limit),
            "fix_suggestions": fix_suggestions,
            "servers": items,
        }

    def reconnect_server(self, server: str, timeout_s: float = 15.0) -> List[Dict[str, Any]]:
        if server in self._clients:
            try:
                self._clients[server]._shutdown_unlocked()
            except Exception:
                pass
        self._tools_cache.pop(server, None)
        tools, _ = self.list_tools(server, timeout_s=timeout_s, use_cache=False)
        return tools

    def refresh_status_sync(
        self,
        servers: Optional[List[str]] = None,
        timeout_s: float = 12.0,
        force: bool = True,
    ) -> Dict[str, Any]:
        """
        Synchronously refresh MCP status/tools for specified servers.
        If servers is None, refresh all configured servers.
        """
        all_servers = self.mcp_config.get("mcpServers", {})
        if not isinstance(all_servers, dict):
            return self.get_status()
        target_servers: List[str]
        if servers:
            target_servers = [s for s in servers if s in all_servers]
        else:
            target_servers = list(all_servers.keys())

        for server in target_servers:
            conf = all_servers.get(server, {})
            if isinstance(conf, dict) and bool(conf.get("skip_preload", False)):
                self._set_status(
                    str(server),
                    "skipped",
                    last_error="skip_preload=true",
                    failure_type="",
                    suggestion="该 server 已配置 skip_preload=true；如需刷新请改为 false。",
                )
                continue
            if force:
                try:
                    self.reconnect_server(str(server), timeout_s=timeout_s)
                except Exception as e:
                    self._log("WARNING", f"refresh_status_sync reconnect failed for {server}: {e}")
            else:
                try:
                    self.list_tools(str(server), timeout_s=timeout_s, use_cache=False)
                except Exception as e:
                    self._log("WARNING", f"refresh_status_sync list failed for {server}: {e}")
        return self.get_status()

    def cached_tools_for_prompt(self) -> str:
        if not self._tools_cache:
            return "尚无已缓存的 MCP tools（需先执行 mcp_list_tools）。"
        lines: List[str] = []
        for server, info in self._tools_cache.items():
            tools = info.get("tools", [])
            entries: List[str] = []
            if isinstance(tools, list):
                for t in tools[:20]:
                    if not isinstance(t, dict):
                        continue
                    name = str(t.get("name", "<unnamed>")).strip() or "<unnamed>"
                    desc = str(t.get("description", "")).strip()
                    if len(desc) > 80:
                        desc = desc[:77] + "..."

                    # Try to summarize parameter keys from common MCP schema shapes.
                    param_keys: List[str] = []
                    schema = t.get("inputSchema")
                    if isinstance(schema, dict):
                        props = schema.get("properties")
                        if isinstance(props, dict):
                            param_keys = [str(k) for k in props.keys()][:6]
                        elif isinstance(schema.get("required"), list):
                            param_keys = [str(k) for k in schema.get("required", [])][:6]
                    elif isinstance(t.get("parameters"), dict):
                        p = t.get("parameters", {})
                        props = p.get("properties")
                        if isinstance(props, dict):
                            param_keys = [str(k) for k in props.keys()][:6]

                    param_part = f" params=[{', '.join(param_keys)}]" if param_keys else ""
                    desc_part = f" - {desc}" if desc else ""
                    entries.append(f"{name}{desc_part}{param_part}")

            show = " | ".join(entries) if entries else "(none)"
            if isinstance(tools, list) and len(tools) > 20:
                show += f" | ... total={len(tools)}"
            lines.append(f"- {server}: {show}")
        return "\n".join(lines)
