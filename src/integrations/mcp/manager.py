import json
import logging
import os
import platform
import queue
import re
import shutil
import subprocess
import threading
import time
import sys
import getpass
import secrets
import hashlib
import base64
import webbrowser
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...config.app_info import (
    get_app_client_model_name,
    get_app_client_name,
    get_app_env_var,
    get_app_slug_kebab,
    get_app_slug_snake,
    get_app_version,
)
from ...core.localization import DEFAULT_DISPLAY_LANGUAGE, normalize_display_language, text


class McpError(Exception):
    pass


_MCP_LOGGER_NAME = f"{get_app_slug_snake()}.mcp"


_SENSITIVE_KEY_PARTS: Tuple[str, ...] = (
    "authorization",
    "token",
    "cookie",
    "secret",
    "password",
    "api-key",
    "apikey",
    "session",
    "set-cookie",
    "email",
)


def _redact_text(text: Any, *, max_len: int = 2000) -> str:
    if text is None:
        return ""
    s = str(text)
    # Common direct identifiers / secrets.
    s = re.sub(r"(; i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b", "{E}<email>{/E}", s)
    s = re.sub(r"(; i)\b(; :\d{1,3}\.){3}\d{1,3}\b", "{E}<ip>{/E}", s)
    s = re.sub(r"(; i)\b(glpat-[A-Za-z0-9\-_]+)\b", "<token:redacted>", s)
    s = re.sub(r"(; i)\b(bearer)\s+[A-Za-z0-9\-_\.=:+/]+\b", r"\1 <token:redacted>", s)
    # Keep logs readable and bounded.
    if len(s) > max(64, int(max_len)):
        s = s[: max(64, int(max_len))] + "...<truncated>"
    return s


def _redact_obj(value: Any) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            key_l = key.lower()
            if any(p in key_l for p in _SENSITIVE_KEY_PARTS):
                out[key] = "<redacted>"
            else:
                out[key] = _redact_obj(v)
        return out
    if isinstance(value, list):
        return [_redact_obj(v) for v in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _json_schema_type_ok(expected_type: str, value: Any) -> bool:
    t = str(expected_type or "").strip().lower()
    if t == "string":
        return isinstance(value, str)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "null":
        return value is None
    return True


def _validate_json_schema_like(
    schema: Dict[str, Any],
    value: Any,
    *,
    path: str = "arguments",
) -> List[str]:
    """
    Validate a practical subset of JSON Schema for MCP tool arguments.
    Supported keys: type, required, properties, items, enum, additionalProperties.
    """
    if not isinstance(schema, dict):
        return []
    errors: List[str] = []

    st = schema.get("type")
    if isinstance(st, str) and not _json_schema_type_ok(st, value):
        errors.append(f"{path} expected type={st}, got={type(value).__name__}")
        return errors

    enum_vals = schema.get("enum")
    if isinstance(enum_vals, list) and enum_vals and value not in enum_vals:
        errors.append(f"{path} must be one of {enum_vals}, got={value!r}")

    if isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if not isinstance(key, str):
                    continue
                if key not in value:
                    errors.append(f"{path}.{key} is required")
        props = schema.get("properties", {})
        if isinstance(props, dict):
            for key, child_schema in props.items():
                if not isinstance(key, str):
                    continue
                if key not in value:
                    continue
                if isinstance(child_schema, dict):
                    errors.extend(_validate_json_schema_like(child_schema, value.get(key), path=f"{path}.{key}"))
        if schema.get("additionalProperties") is False and isinstance(props, dict):
            allowed = {k for k in props.keys() if isinstance(k, str)}
            for key in value.keys():
                if str(key) not in allowed:
                    errors.append(f"{path}.{key} is not allowed")

    if isinstance(value, list):
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            for idx, item in enumerate(value):
                errors.extend(_validate_json_schema_like(items_schema, item, path=f"{path}[{idx}]"))

    return errors


def _extract_tool_schema(tool_desc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(tool_desc, dict):
        return None
    schema = tool_desc.get("inputSchema")
    if isinstance(schema, dict):
        return schema
    params = tool_desc.get("parameters")
    if isinstance(params, dict):
        return params
    return None


def _extract_tool_stream_chunk(method: str, params: Dict[str, Any]) -> str:
    """
    Extract text chunk from MCP streaming tool-result notifications.
    Supported method patterns:
    - notifications/tools/call/stream
    - notifications/tools/call/progress
    """
    m = str(method or "").strip().lower()
    if not (m.endswith("/stream") or m.endswith("/progress")):
        return ""
    p = params if isinstance(params, dict) else {}
    if isinstance(p.get("chunk"), str):
        return str(p.get("chunk"))
    delta = p.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("text"), str):
        return str(delta.get("text"))
    content = p.get("content")
    if isinstance(content, dict) and str(content.get("type", "")).lower() == "text":
        text = content.get("text")
        if isinstance(text, str):
            return text
    return ""


def _parse_content_length(header_blob: bytes) -> int:
    text = header_blob.decode("utf-8", errors="replace").replace("\r\n", "\n")
    lines = text.split("\n")
    for line in lines:
        if line.lower().startswith("content-length:"):
            raw = line.split(":", 1)[1].strip()
            return int(raw)
    raise McpError("MCP response is missing the Content-Length header")


def _extract_initialize_instructions(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    return str(result.get("instructions", "") or "").strip()


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
    peer_request_handler: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None
    initialize_instructions: str = ""

    def _handshake_debug_enabled(self) -> bool:
        """Enable handshake debug by env or per-server config."""
        env_flag = str(os.environ.get(get_app_env_var("MCP_HANDSHAKE_DEBUG"), "")).strip().lower()
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
            "error": _redact_text(msg.get("error"), max_len=240) if "error" in msg else "",
        }

    def _trace_handshake_msg(self, event: str, msg: Dict[str, Any]) -> None:
        if not self._handshake_debug_enabled():
            return
        try:
            summary = self._safe_msg_summary(msg)
            logging.getLogger(_MCP_LOGGER_NAME).info(
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
            sample = _redact_text(sample, max_len=160)
            has_len = b"content-length:" in payload.lower()
            has_sep = (b"\r\n\r\n" in payload) or (b"\n\n" in payload)
            logging.getLogger(_MCP_LOGGER_NAME).info(
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
            raise McpError("Current client instance only supports stdio configuration; detected url configuration, please use McpUrlClient instead")
        command = self.config.get("command")
        if not command:
            raise McpError("MCP server is missing 'command'")
        command_str = str(command).strip()
        resolved_command = self._resolve_command(command_str)
        if not Path(resolved_command).is_absolute() and shutil.which(command_str) is None:
            raise McpError(f"Executable not found: {command_str}")
        args = self.config.get("args", [])
        if not isinstance(args, list):
            raise McpError("MCP server args must be an array")
        fallback_argv = [resolved_command, *self._normalize_npx_args(args)]
        spawn_argv = fallback_argv
        if self._handshake_debug_enabled():
            try:
                exec_path = str(spawn_argv[0]) if spawn_argv else ""
                logging.getLogger(_MCP_LOGGER_NAME).info(
                    f"[HSDBG] server={self.name} event=spawn argv={{\"spawn_exec\": {json.dumps(exec_path, ensure_ascii=False)}, \"arg_count\": {max(0, len(spawn_argv) - 1)}}}"
                )
            except Exception:
                pass
        popen_kwargs: Dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": self._build_env(),
            "text": False,
        }
        if platform.system().lower().startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW"):
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        self.process = subprocess.Popen(
            spawn_argv,
            **popen_kwargs,
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
                        elif isinstance(msg, list):
                            for item in msg:
                                if isinstance(item, dict):
                                    self._trace_handshake_msg("rx_line_batch_item", item)
                                    self.response_queue.put(item)
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
                                elif isinstance(msg, list):
                                    for item in msg:
                                        if isinstance(item, dict):
                                            self._trace_handshake_msg("rx_framed_batch_item", item)
                                            self.response_queue.put(item)
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
                        logging.getLogger(_MCP_LOGGER_NAME).info(
                            f"[HSDBG] server={self.name} event=stderr_tail tail={json.dumps(_redact_text(tail), ensure_ascii=False)}"
                        )
                except Exception:
                    pass

    def _tail_stderr(self, n: int = 8) -> str:
        if not self.stderr_lines:
            return ""
        return " | ".join(self.stderr_lines[-n:])

    def _write_message(self, payload: Any) -> None:
        if self.process is None or self.process.stdin is None:
            raise McpError("MCP server is not started")
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
                raise McpError(f"MCP server exited (method={method}, code={code}){suffix}")
            try:
                self._write_message(req)
            except OSError as e:
                err_tail = self._tail_stderr()
                suffix = f"；stderr: {err_tail}" if err_tail else ""
                raise McpError(f"MCP write failed (method={method}, id={req_id}): {e}{suffix}")
            deadline = time.time() + max(0.2, timeout_s)
            stream_chunks: List[str] = []
            while True:
                left = deadline - time.time()
                if left <= 0:
                    err_tail = self._tail_stderr()
                    suffix = f"；stderr: {err_tail}" if err_tail else ""
                    raise McpError(f"MCP request timed out (method={method}, id={req_id}){suffix}")
                if self.process is not None and self.process.poll() is not None:
                    err_tail = self._tail_stderr()
                    suffix = f"；stderr: {err_tail}" if err_tail else ""
                    raise McpError(f"MCP server exited (method={method}, code={self.process.returncode}){suffix}")
                try:
                    msg = self.response_queue.get(timeout=min(0.5, left))
                except queue.Empty:
                    continue
                # Handle server->client notifications/requests (bidirectional MCP).
                if isinstance(msg, dict) and "method" in msg and "result" not in msg and "error" not in msg:
                    incoming_method = str(msg.get("method", "")).strip()
                    incoming_params = msg.get("params", {})
                    incoming_id = msg.get("id")
                    if incoming_id is None:
                        # Stream notifications during tools/call.
                        if method == "tools/call" and isinstance(incoming_params, dict):
                            chunk = _extract_tool_stream_chunk(incoming_method, incoming_params)
                            if chunk:
                                stream_chunks.append(chunk)
                        # Notification: best-effort handle and continue.
                        try:
                            if callable(self.peer_request_handler):
                                p = incoming_params if isinstance(incoming_params, dict) else {}
                                self.peer_request_handler(incoming_method, p)
                        except Exception:
                            pass
                        continue
                    # Request: must respond with result/error to unblock peer.
                    try:
                        if callable(self.peer_request_handler):
                            p = incoming_params if isinstance(incoming_params, dict) else {}
                            res = self.peer_request_handler(incoming_method, p)
                            resp = {"jsonrpc": "2.0", "id": incoming_id, "result": res if isinstance(res, dict) else {}}
                        elif incoming_method == "elicitation/create":
                            # Keep bidirectional elicitation resilient even if no handler is registered.
                            resp = {
                                "jsonrpc": "2.0",
                                "id": incoming_id,
                                "result": {"action": "accept", "content": {}},
                            }
                        else:
                            resp = {
                                "jsonrpc": "2.0",
                                "id": incoming_id,
                                "error": {"code": -32601, "message": f"Method not found: {incoming_method}"},
                            }
                    except Exception as e:
                        if incoming_method == "elicitation/create":
                            # Fallback to protocol-safe default instead of surfacing handler errors to peer.
                            resp = {
                                "jsonrpc": "2.0",
                                "id": incoming_id,
                                "result": {"action": "accept", "content": {}},
                            }
                        else:
                            resp = {
                                "jsonrpc": "2.0",
                                "id": incoming_id,
                                "error": {"code": -32000, "message": f"Client handler error: {e}"},
                            }
                    try:
                        self._write_message(resp)
                    except Exception:
                        pass
                    continue
                msg_id = msg.get("id")
                # Be tolerant to JSON-RPC id type differences (e.g. "2" vs 2).
                if not (msg_id == req_id or str(msg_id) == str(req_id)):
                    continue
                if "error" in msg:
                    err_tail = self._tail_stderr()
                    suffix = f"；stderr: {err_tail}" if err_tail else ""
                    raise McpError(f"{msg.get('error')}{suffix}")
                result = msg.get("result", {})
                if not isinstance(result, dict):
                    result = {"value": result}
                if stream_chunks:
                    result["_stream"] = {
                        "chunks": stream_chunks,
                        "text": "".join(stream_chunks),
                        "chunk_count": len(stream_chunks),
                    }
                    content = result.get("content")
                    if not content:
                        result["content"] = [{"type": "text", "text": result["_stream"]["text"]}]
                return result

    def _request_batch(
        self,
        calls: List[Tuple[str, Optional[Dict[str, Any]]]],
        timeout_s: float,
        *,
        allow_partial_failure: bool = False,
    ) -> List[Dict[str, Any]]:
        with self.lock:
            self._start_if_needed()
            if not calls:
                return []
            reqs: List[Dict[str, Any]] = []
            id_to_idx: Dict[str, int] = {}
            for idx, (method, params) in enumerate(calls):
                req_id = self.next_id
                self.next_id += 1
                req: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": str(method)}
                if params is not None:
                    req["params"] = params
                reqs.append(req)
                id_to_idx[str(req_id)] = idx

            if self.process is None or self.process.poll() is not None:
                err_tail = self._tail_stderr()
                suffix = f"；stderr: {err_tail}" if err_tail else ""
                code = self.process.returncode if self.process is not None else "unknown"
                raise McpError(f"MCP server exited (method=batch, code={code}){suffix}")

            try:
                self._write_message(reqs)  # type: ignore[arg-type]
            except OSError as e:
                err_tail = self._tail_stderr()
                suffix = f"；stderr: {err_tail}" if err_tail else ""
                raise McpError(f"MCP write failed (method=batch): {e}{suffix}")

            deadline = time.time() + max(0.2, timeout_s)
            pending = set(id_to_idx.keys())
            results: List[Optional[Dict[str, Any]]] = [None] * len(calls)
            while pending:
                left = deadline - time.time()
                if left <= 0:
                    err_tail = self._tail_stderr()
                    suffix = f"；stderr: {err_tail}" if err_tail else ""
                    raise McpError(f"MCP batch request timed out (pending={len(pending)}){suffix}")
                if self.process is not None and self.process.poll() is not None:
                    err_tail = self._tail_stderr()
                    suffix = f"；stderr: {err_tail}" if err_tail else ""
                    raise McpError(f"MCP server exited (method=batch, code={self.process.returncode}){suffix}")
                try:
                    msg = self.response_queue.get(timeout=min(0.5, left))
                except queue.Empty:
                    continue
                if not isinstance(msg, dict):
                    continue
                # ignore notifications/requests in batch context
                if "method" in msg and "result" not in msg and "error" not in msg:
                    continue
                msg_id = str(msg.get("id"))
                if msg_id not in pending:
                    continue
                idx = id_to_idx[msg_id]
                if "error" in msg:
                    if allow_partial_failure:
                        results[idx] = {
                            "ok": False,
                            "error": msg.get("error"),
                        }
                        pending.remove(msg_id)
                        continue
                    err_tail = self._tail_stderr()
                    suffix = f"；stderr: {err_tail}" if err_tail else ""
                    raise McpError(f"{msg.get('error')}{suffix}")
                r = msg.get("result", {})
                if not isinstance(r, dict):
                    r = {"value": r}
                results[idx] = {"ok": True, "result": r} if allow_partial_failure else r
                pending.remove(msg_id)
            if allow_partial_failure:
                return [r if isinstance(r, dict) else {"ok": False, "error": "missing"} for r in results]
            return [r if isinstance(r, dict) else {} for r in results]

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
                        result = self._request(
                            "initialize",
                            {
                                "protocolVersion": pv,
                                "clientInfo": {"name": get_app_client_name(), "version": get_app_version()},
                                "capabilities": {"elicitation": {"form": {}}},
                            },
                            timeout_s=min(left, per_try_floor),
                        )
                        self.initialize_instructions = _extract_initialize_instructions(result)
                        self.negotiated_protocol = pv
                        last_error = None
                        break
                    except Exception as e:
                        last_error = str(e)
                        is_exit = "MCP server exited" in last_error or "MCP write failed" in last_error
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
            raise McpError(f"initialize failed (still failed after protocol fallback): {last_error}")
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

    def call_tools_batch(
        self, calls: List[Dict[str, Any]], timeout_s: float = 30.0, *, allow_partial_failure: bool = False
    ) -> List[Dict[str, Any]]:
        reqs: List[Tuple[str, Optional[Dict[str, Any]]]] = []
        for c in calls:
            if not isinstance(c, dict):
                continue
            reqs.append(
                (
                    "tools/call",
                    {
                        "name": str(c.get("tool", "")),
                        "arguments": c.get("arguments", {}) if isinstance(c.get("arguments", {}), dict) else {},
                    },
                )
            )
        return self._request_batch(reqs, timeout_s=timeout_s, allow_partial_failure=allow_partial_failure)

    def list_resources(self, timeout_s: float = 8.0) -> List[Dict[str, Any]]:
        self.initialize(timeout_s=timeout_s)
        result = self._request("resources/list", {}, timeout_s=timeout_s)
        resources = result.get("resources", [])
        return resources if isinstance(resources, list) else []

    def read_resource(self, uri: str, timeout_s: float = 20.0) -> Dict[str, Any]:
        self.initialize(timeout_s=timeout_s)
        return self._request(
            "resources/read",
            {"uri": str(uri)},
            timeout_s=timeout_s,
        )

    def list_resource_templates(self, timeout_s: float = 8.0) -> List[Dict[str, Any]]:
        self.initialize(timeout_s=timeout_s)
        result = self._request("resources/templates/list", {}, timeout_s=timeout_s)
        templates = result.get("resourceTemplates")
        if not isinstance(templates, list):
            templates = result.get("templates", [])
        return templates if isinstance(templates, list) else []

    def list_prompts(self, timeout_s: float = 8.0) -> List[Dict[str, Any]]:
        self.initialize(timeout_s=timeout_s)
        result = self._request("prompts/list", {}, timeout_s=timeout_s)
        prompts = result.get("prompts", [])
        return prompts if isinstance(prompts, list) else []

    def get_prompt(self, prompt_name: str, arguments: Dict[str, Any], timeout_s: float = 20.0) -> Dict[str, Any]:
        self.initialize(timeout_s=timeout_s)
        return self._request(
            "prompts/get",
            {"name": str(prompt_name), "arguments": arguments or {}},
            timeout_s=timeout_s,
        )

    def sampling_create_message(self, params: Dict[str, Any], timeout_s: float = 30.0) -> Dict[str, Any]:
        self.initialize(timeout_s=timeout_s)
        return self._request(
            "sampling/createMessage",
            params or {},
            timeout_s=timeout_s,
        )

    def completion_complete(self, params: Dict[str, Any], timeout_s: float = 20.0) -> Dict[str, Any]:
        self.initialize(timeout_s=timeout_s)
        return self._request(
            "completion/complete",
            params or {},
            timeout_s=timeout_s,
        )



@dataclass
class McpUrlClient:
    name: str
    config: Dict[str, Any]
    next_id: int = 1
    initialized: bool = False
    initialize_instructions: str = ""
    session_id: Optional[str] = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    peer_request_handler: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None
    token_store_path: Optional[str] = None
    oauth_token: Dict[str, Any] = field(default_factory=dict)
    _manual_rejected_token_fps: set = field(default_factory=set)
    _legacy_sse_stream: Any = None
    _legacy_sse_message_url: str = ""

    def _debug_enabled(self) -> bool:
        env_flag = str(os.environ.get(get_app_env_var("MCP_HANDSHAKE_DEBUG"), "")).strip().lower()
        if env_flag in ("1", "true", "yes", "on"):
            return True
        return bool(self.config.get("debug_handshake", False))

    def _debug_log(self, message: str) -> None:
        if not self._debug_enabled():
            return
        try:
            logging.getLogger(_MCP_LOGGER_NAME).info(
                f"[HSDBG-URL] server={self.name} {_redact_text(message, max_len=3000)}"
            )
        except Exception:
            pass

    @staticmethod
    def _redact_text(text: str) -> str:
        return _redact_text(text)

    @classmethod
    def _redact_obj(cls, value: Any) -> Any:
        return _redact_obj(value)

    def _base_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        extra = self.config.get("headers", {})
        if isinstance(extra, dict):
            for k, v in extra.items():
                headers[str(k)] = str(v)
        # Only inject OAuth bearer token if caller didn't already set Authorization.
        if "Authorization" not in headers and isinstance(self.oauth_token, dict):
            at = str(self.oauth_token.get("access_token", "") or "").strip()
            if at:
                headers["Authorization"] = f"Bearer {at}"
        if self.session_id:
            headers["mcp-session-id"] = str(self.session_id)
        return headers

    @staticmethod
    def _is_session_lost_error(err: Exception) -> bool:
        msg = str(err).lower()
        return (
            "session not found" in msg
            or "invalid session" in msg
            or "unknown session" in msg
            or ("404" in msg and "session" in msg)
        )

    def _oauth_conf(self) -> Dict[str, Any]:
        oauth = self.config.get("oauth", {})
        return oauth if isinstance(oauth, dict) else {}

    def _manual_auth_fallback_enabled(self, mcp_url: str) -> bool:
        conf_flag = self.config.get("manual_auth_fallback")
        if isinstance(conf_flag, bool):
            return conf_flag
        try:
            host = str(urllib.parse.urlsplit(str(mcp_url or "")).hostname or "").lower()
        except Exception:
            host = ""
        # Provider-specific fallback defaults (can still be overridden by config).
        return host.endswith("mcp.figma.com")

    @staticmethod
    def _extract_bearer_token(raw: str) -> str:
        s = str(raw or "").strip()
        if not s:
            return ""
        m = re.search(r"(; i)\bauthorization\s*:\s*bearer\s+(.+)$", s)
        if m:
            return str(m.group(1)).strip()
        m = re.search(r"(; i)\bbearer\s+(.+)$", s)
        if m:
            return str(m.group(1)).strip()
        return s

    def _manual_token_prompt_and_store(self, mcp_url: str) -> bool:
        # Only available in interactive terminal sessions.
        try:
            allow_non_tty = bool(self.config.get("manual_auth_allow_non_tty", False))
            if not sys.stdin.isatty() and not allow_non_tty:
                return False
        except Exception:
            return False
        print(text("mcp.auth.missing_challenge", self.display_language))
        print(text("mcp.auth.server_url", self.display_language, name=self.name, url=mcp_url))
        print(text("mcp.auth.complete_login_then_paste", self.display_language))
        print(text("mcp.auth.authorization_bearer", self.display_language))
        print(text("mcp.auth.or_paste_token", self.display_language))
        # Do not echo token back to terminal output.
        raw = getpass.getpass("Enter token (press Enter to cancel): ").strip()
        token = self._extract_bearer_token(raw)
        if not token:
            return False
        fp = self._token_fp(token)
        if fp and fp in self._manual_rejected_token_fps:
            print(text("mcp.auth.token_previously_failed", self.display_language))
            return False
        self.oauth_token = {
            "access_token": token,
            "token_type": "Bearer",
            "scope": "",
            "token_endpoint": "",
        }
        self._save_oauth_token_to_store()
        return True

    @staticmethod
    def _token_fp(token: str) -> str:
        s = str(token or "").strip()
        if not s:
            return ""
        try:
            return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()[:16]
        except Exception:
            return ""

    def _is_current_manual_token_rejected(self) -> bool:
        at = ""
        if isinstance(self.oauth_token, dict):
            at = str(self.oauth_token.get("access_token", "") or "").strip()
        fp = self._token_fp(at)
        return bool(fp and fp in self._manual_rejected_token_fps)

    def _mark_current_manual_token_rejected(self) -> None:
        at = ""
        if isinstance(self.oauth_token, dict):
            at = str(self.oauth_token.get("access_token", "") or "").strip()
        fp = self._token_fp(at)
        if fp:
            self._manual_rejected_token_fps.add(fp)
            self._save_oauth_token_to_store()

    def _server_resource_uri(self) -> str:
        """
        Canonical resource URI for RFC8707 resource parameter.
        Keep scheme+host(+port)+path, drop query/fragment.
        """
        url = str(self.config.get("url", "") or "").strip()
        if not url:
            return ""
        u = urllib.parse.urlsplit(url)
        scheme = str(u.scheme or "").lower()
        netloc = str(u.netloc or "").lower()
        path = str(u.path or "")
        return urllib.parse.urlunsplit((scheme, netloc, path, "", ""))

    def _token_store_key(self) -> str:
        res = self._server_resource_uri() or str(self.config.get("url", "")).strip()
        return f"{self.name}::{res}"

    def _load_oauth_token_from_store(self) -> None:
        p = str(self.token_store_path or "").strip()
        if not p:
            return
        try:
            path = Path(p)
            if not path.exists():
                return
            payload = json.loads(path.read_text(encoding="utf-8") or "{}")
            if not isinstance(payload, dict):
                return
            item = payload.get(self._token_store_key(), {})
            if isinstance(item, dict):
                self.oauth_token = dict(item)
                rejected = item.get("_manual_rejected_token_fps", [])
                if isinstance(rejected, list):
                    self._manual_rejected_token_fps = {
                        str(x).strip() for x in rejected if str(x).strip()
                    }
        except Exception:
            return

    def _save_oauth_token_to_store(self) -> None:
        p = str(self.token_store_path or "").strip()
        if not p:
            return
        try:
            path = Path(p)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload: Dict[str, Any] = {}
            if path.exists():
                try:
                    current = json.loads(path.read_text(encoding="utf-8") or "{}")
                    if isinstance(current, dict):
                        payload = current
                except Exception:
                    payload = {}
            token_obj = dict(self.oauth_token if isinstance(self.oauth_token, dict) else {})
            if self._manual_rejected_token_fps:
                token_obj["_manual_rejected_token_fps"] = sorted(self._manual_rejected_token_fps)
            payload[self._token_store_key()] = token_obj
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            return

    def _token_expired(self) -> bool:
        if not isinstance(self.oauth_token, dict):
            return True
        at = str(self.oauth_token.get("access_token", "") or "").strip()
        if not at:
            return True
        exp = float(self.oauth_token.get("expires_at", 0.0) or 0.0)
        if exp <= 0:
            return False
        # Refresh a bit earlier for clock skew.
        return time.time() >= (exp - 30.0)

    @staticmethod
    def _parse_www_authenticate(header_value: str) -> Dict[str, str]:
        s = str(header_value or "").strip()
        out: Dict[str, str] = {}
        if not s:
            return out
        if s.lower().startswith("bearer"):
            s = s[6:].strip()
        # key="value", key=value
        for m in re.finditer(r'([a-zA-Z_][a-zA-Z0-9_\-]*)\s*=\s*("([^"]*)"|[^,\s]+)', s):
            k = str(m.group(1)).strip()
            v = str(m.group(3) if m.group(3) is not None else m.group(2)).strip().strip('"')
            out[k] = v
        return out

    def _oauth_fetch_json(self, url: str, headers: Optional[Dict[str, str]] = None, timeout_s: float = 8.0) -> Dict[str, Any]:
        req = urllib.request.Request(url=url, headers=headers or {}, method="GET")
        with urllib.request.urlopen(req, timeout=max(0.5, float(timeout_s))) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw or "{}")
            return data if isinstance(data, dict) else {}

    def _oauth_discover_resource_metadata(self, mcp_url: str, challenge: Dict[str, str], timeout_s: float) -> Dict[str, Any]:
        # 1) WWW-Authenticate resource_metadata
        rm = str(challenge.get("resource_metadata", "") or "").strip()
        if rm:
            return self._oauth_fetch_json(rm, timeout_s=timeout_s)
        # 2) Well-known fallback (path-specific then root), per MCP auth draft.
        u = urllib.parse.urlsplit(mcp_url)
        origin = urllib.parse.urlunsplit((u.scheme, u.netloc, "", "", ""))
        path = str(u.path or "").lstrip("/")
        candidates: List[str] = []
        if path:
            candidates.append(f"{origin}/.well-known/oauth-protected-resource/{path}")
        candidates.append(f"{origin}/.well-known/oauth-protected-resource")
        last_err = ""
        for c in candidates:
            try:
                return self._oauth_fetch_json(c, timeout_s=timeout_s)
            except Exception as e:
                last_err = str(e)
                continue
        raise McpError(f"OAuth resource metadata discovery failed: {last_err or 'unknown'}")

    def _oauth_discover_authorization_server_metadata(self, issuer: str, timeout_s: float) -> Dict[str, Any]:
        iu = urllib.parse.urlsplit(str(issuer or "").strip())
        if not iu.scheme or not iu.netloc:
            raise McpError("OAuth authorization server URL is invalid")
        base = urllib.parse.urlunsplit((iu.scheme, iu.netloc, "", "", ""))
        path = str(iu.path or "").strip("/")
        candidates: List[str] = []
        if path:
            candidates.extend(
                [
                    f"{base}/.well-known/oauth-authorization-server/{path}",
                    f"{base}/.well-known/openid-configuration/{path}",
                    f"{base}/{path}/.well-known/openid-configuration",
                ]
            )
        else:
            candidates.extend(
                [
                    f"{base}/.well-known/oauth-authorization-server",
                    f"{base}/.well-known/openid-configuration",
                ]
            )
        last_err = ""
        for url in candidates:
            try:
                md = self._oauth_fetch_json(url, timeout_s=timeout_s)
                if md:
                    return md
            except Exception as e:
                last_err = str(e)
                continue
        raise McpError(f"OAuth authorization server metadata discovery failed: {last_err or 'unknown'}")

    @staticmethod
    def _pkce_challenge(verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def _oauth_exchange_token(
        self,
        token_endpoint: str,
        form: Dict[str, str],
        client_id: str,
        client_secret: str,
        timeout_s: float,
    ) -> Dict[str, Any]:
        body = urllib.parse.urlencode(form).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if client_secret:
            # Use basic auth when confidential client secret exists.
            basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {basic}"
        req = urllib.request.Request(url=token_endpoint, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=max(0.5, float(timeout_s))) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw or "{}")
            return data if isinstance(data, dict) else {}

    def _oauth_dynamic_register(
        self,
        registration_endpoint: str,
        redirect_uri: str,
        timeout_s: float,
    ) -> Dict[str, Any]:
        payload = {
            "client_name": f"{get_app_slug_kebab()}-{self.name}",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url=registration_endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=max(0.5, float(timeout_s))) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw or "{}")
            return data if isinstance(data, dict) else {}

    def _oauth_refresh_token(self, timeout_s: float = 10.0) -> bool:
        oauth = self._oauth_conf()
        rt = str(self.oauth_token.get("refresh_token", "") if isinstance(self.oauth_token, dict) else "").strip()
        token_ep = str(self.oauth_token.get("token_endpoint", "") if isinstance(self.oauth_token, dict) else "").strip()
        client_id = str(oauth.get("client_id", "")).strip()
        client_secret = str(oauth.get("client_secret", "")).strip()
        if not (rt and token_ep and client_id):
            return False
        try:
            data = self._oauth_exchange_token(
                token_ep,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": rt,
                    "client_id": client_id,
                },
                client_id=client_id,
                client_secret=client_secret,
                timeout_s=timeout_s,
            )
            at = str(data.get("access_token", "")).strip()
            if not at:
                return False
            self.oauth_token["access_token"] = at
            if isinstance(data.get("refresh_token"), str) and data.get("refresh_token"):
                self.oauth_token["refresh_token"] = str(data.get("refresh_token"))
            exp_in = data.get("expires_in")
            if isinstance(exp_in, (int, float)) and float(exp_in) > 0:
                self.oauth_token["expires_at"] = time.time() + float(exp_in)
            self._save_oauth_token_to_store()
            return True
        except Exception:
            return False

    def _oauth_authorize_interactive(self, mcp_url: str, challenge: Dict[str, str], timeout_s: float = 30.0) -> None:
        oauth = self._oauth_conf()
        resource_md = self._oauth_discover_resource_metadata(mcp_url, challenge, timeout_s=min(10.0, timeout_s))
        auth_servers = resource_md.get("authorization_servers", [])
        if isinstance(auth_servers, str):
            auth_servers = [auth_servers]
        if not isinstance(auth_servers, list) or not auth_servers:
            raise McpError("OAuth resource metadata is missing authorization_servers")
        auth_server = str(oauth.get("authorization_server", "") or "").strip() or str(auth_servers[0]).strip()
        as_md = self._oauth_discover_authorization_server_metadata(auth_server, timeout_s=min(10.0, timeout_s))
        authorization_endpoint = str(as_md.get("authorization_endpoint", "")).strip()
        token_endpoint = str(as_md.get("token_endpoint", "")).strip()
        methods = as_md.get("code_challenge_methods_supported", [])
        if not authorization_endpoint or not token_endpoint:
            raise McpError("OAuth metadata is missing authorization_endpoint/token_endpoint")
        if not isinstance(methods, list) or "S256" not in [str(x) for x in methods]:
            raise McpError("Authorization Server does not support PKCE S256; MCP client refuses to continue")

        client_id = str(oauth.get("client_id", "")).strip()
        client_secret = str(oauth.get("client_secret", "")).strip()
        redirect_host = str(oauth.get("redirect_host", "127.0.0.1")).strip() or "127.0.0.1"
        redirect_port_cfg = oauth.get("redirect_port", 0)
        try:
            redirect_port = int(redirect_port_cfg)
        except Exception:
            redirect_port = 0

        scope_from_challenge = str(challenge.get("scope", "")).strip()
        scopes_supported = resource_md.get("scopes_supported", [])
        conf_scope = oauth.get("scope")
        scope = ""
        if isinstance(conf_scope, str) and conf_scope.strip():
            scope = conf_scope.strip()
        elif isinstance(conf_scope, list) and conf_scope:
            scope = " ".join([str(x).strip() for x in conf_scope if str(x).strip()])
        elif scope_from_challenge:
            scope = scope_from_challenge
        elif isinstance(scopes_supported, list) and scopes_supported:
            scope = " ".join([str(x).strip() for x in scopes_supported if str(x).strip()])

        # loopback callback server
        import http.server
        import socketserver

        callback_box: Dict[str, Any] = {"code": "", "state": "", "error": ""}
        done_ev = threading.Event()

        class _CbHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                return

            def do_GET(self) -> None:  # noqa: N802
                try:
                    pu = urllib.parse.urlsplit(self.path)
                    qs = urllib.parse.parse_qs(pu.query or "")
                    callback_box["code"] = str((qs.get("code") or [""])[0])
                    callback_box["state"] = str((qs.get("state") or [""])[0])
                    callback_box["error"] = str((qs.get("error") or [""])[0])
                    done_ev.set()
                    body = b"OAuth authorization completed. You can close this tab."
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:
                    self.send_response(500)
                    self.end_headers()

        class _Srv(socketserver.TCPServer):
            allow_reuse_address = True

        with _Srv((redirect_host, max(0, redirect_port)), _CbHandler) as srv:
            actual_port = int(srv.server_address[1])
            redirect_uri = f"http://{redirect_host}:{actual_port}/callback"
            # Dynamic client registration fallback when no pre-registered client_id exists.
            if not client_id:
                reg_ep = str(as_md.get("registration_endpoint", "")).strip()
                if not reg_ep:
                    raise McpError("Missing OAuth client_id, and authorization server did not provide registration_endpoint")
                reg = self._oauth_dynamic_register(reg_ep, redirect_uri=redirect_uri, timeout_s=min(10.0, timeout_s))
                client_id = str(reg.get("client_id", "")).strip()
                client_secret = str(reg.get("client_secret", "")).strip()
                if not client_id:
                    raise McpError("OAuth dynamic registration failed: missing client_id in response")
            verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
            challenge_s256 = self._pkce_challenge(verifier)
            state = secrets.token_urlsafe(16)
            resource_uri = self._server_resource_uri()
            auth_params = {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": challenge_s256,
                "code_challenge_method": "S256",
                "state": state,
                "resource": resource_uri,
            }
            if scope:
                auth_params["scope"] = scope
            auth_url = authorization_endpoint + ("&" if "?" in authorization_endpoint else "?") + urllib.parse.urlencode(auth_params)
            self._debug_log(
                "oauth_authorize "
                + json.dumps(
                    {"authorization_endpoint": authorization_endpoint, "redirect_uri": redirect_uri, "resource": resource_uri},
                    ensure_ascii=False,
                )
            )
            if oauth.get("open_browser", True) is False:
                print(text("mcp.oauth.complete_in_browser_with_url", self.display_language, url=auth_url))
            else:
                try:
                    webbrowser.open(auth_url)
                    print(text("mcp.oauth.browser_open_attempted", self.display_language))
                    print(auth_url)
                except Exception:
                    print(text("mcp.oauth.complete_in_browser", self.display_language))
                    print(auth_url)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            ok = done_ev.wait(timeout=max(10.0, float(timeout_s)))
            try:
                srv.shutdown()
            except Exception:
                pass
            if not ok:
                raise McpError("OAuth authorization timed out: callback code was not received")
            if callback_box.get("error"):
                raise McpError(f"OAuth authorization failed: {callback_box.get('error')}")
            code = str(callback_box.get("code", "")).strip()
            if not code:
                raise McpError("OAuth callback is missing code")
            if str(callback_box.get("state", "")).strip() != state:
                raise McpError("OAuth state validation failed")
            token_data = self._oauth_exchange_token(
                token_endpoint,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "code_verifier": verifier,
                    "resource": resource_uri,
                },
                client_id=client_id,
                client_secret=client_secret,
                timeout_s=max(10.0, float(timeout_s)),
            )
            at = str(token_data.get("access_token", "")).strip()
            if not at:
                raise McpError("OAuth token response is missing access_token")
            self.oauth_token = {
                "access_token": at,
                "refresh_token": str(token_data.get("refresh_token", "") or ""),
                "token_type": str(token_data.get("token_type", "Bearer") or "Bearer"),
                "scope": str(token_data.get("scope", "") or ""),
                "token_endpoint": token_endpoint,
            }
            exp_in = token_data.get("expires_in")
            if isinstance(exp_in, (int, float)) and float(exp_in) > 0:
                self.oauth_token["expires_at"] = time.time() + float(exp_in)
            self._save_oauth_token_to_store()

    def _ensure_oauth_token(self, timeout_s: float = 20.0) -> None:
        # Lazy load token once from store.
        if not self.oauth_token:
            self._load_oauth_token_from_store()
        if not self._token_expired():
            return
        if self._oauth_refresh_token(timeout_s=min(10.0, timeout_s)):
            return

    @staticmethod
    def _is_insufficient_scope_challenge(www_authenticate: str) -> bool:
        wa = str(www_authenticate or "").lower()
        return "insufficient_scope" in wa

    def _is_legacy_sse_transport(self) -> bool:
        transport = str(self.config.get("transport", "") or "").strip().lower()
        if transport == "sse":
            return True
        url = str(self.config.get("url", "") or "").strip().lower()
        return url.endswith("/sse")

    def _post_jsonrpc_response_to_url(
        self,
        target_url: str,
        req_id: Any,
        *,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
        timeout_s: float = 8.0,
    ) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result if isinstance(result, dict) else {}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self._base_headers()
        request = urllib.request.Request(url=target_url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=max(0.5, float(timeout_s))) as resp:
            try:
                sid = resp.headers.get("mcp-session-id")
            except Exception:
                sid = None
            if sid:
                self.session_id = sid
            try:
                _ = resp.read()
            except Exception:
                pass

    def _reset_legacy_sse_connection(self) -> None:
        stream = self._legacy_sse_stream
        self._legacy_sse_stream = None
        self._legacy_sse_message_url = ""
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

    def _reset_session_state(self) -> None:
        self.initialized = False
        self.session_id = None
        if self._is_legacy_sse_transport():
            self._reset_legacy_sse_connection()

    def _legacy_sse_read_event(self, timeout_s: float) -> Tuple[str, str]:
        stream = self._legacy_sse_stream
        if stream is None:
            raise McpError("SSE stream is not established")
        event_name = ""
        data_lines: List[str] = []
        deadline = time.time() + max(0.5, float(timeout_s))
        while True:
            if time.time() >= deadline:
                raise McpError("SSE read timed out")
            line_b = stream.readline()
            if not line_b:
                raise McpError("SSE stream disconnected")
            line = line_b.decode("utf-8", errors="replace").rstrip("\r\n")
            if line == "":
                return event_name, "\n".join(data_lines).strip()
            if line.startswith("event:"):
                event_name = line[6:].strip().lower()
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())

    def _ensure_legacy_sse_connection(self, timeout_s: float) -> str:
        if self._legacy_sse_stream is not None and self._legacy_sse_message_url:
            return self._legacy_sse_message_url
        self._reset_legacy_sse_connection()
        url = str(self.config.get("url", "")).strip()
        if not url:
            raise McpError("URL MCP server is missing url configuration")
        sse_headers = self._base_headers()
        sse_headers.pop("Content-Type", None)
        sse_headers["Accept"] = "text/event-stream"
        sse_req = urllib.request.Request(url=url, headers=sse_headers, method="GET")
        resp = urllib.request.urlopen(sse_req, timeout=max(0.5, float(timeout_s)))
        self._legacy_sse_stream = resp
        try:
            while True:
                event_name, payload_text = self._legacy_sse_read_event(timeout_s=max(1.0, timeout_s))
                if event_name != "endpoint":
                    continue
                endpoint = str(payload_text or "").strip()
                if not endpoint:
                    continue
                try:
                    parsed = urllib.parse.urlsplit(endpoint)
                    if not parsed.scheme:
                        endpoint = urllib.parse.urljoin(url, endpoint)
                except Exception:
                    endpoint = urllib.parse.urljoin(url, endpoint)
                self._legacy_sse_message_url = endpoint
                self._debug_log(
                    "legacy_sse_connected "
                    + json.dumps({"sse_url": url, "message_url": endpoint}, ensure_ascii=False)
                )
                return endpoint
        except Exception:
            self._reset_legacy_sse_connection()
            raise

    def _post_jsonrpc(
        self,
        method: str,
        params: Optional[Dict[str, Any]],
        timeout_s: float,
        *,
        allow_oauth_retry: bool = True,
    ) -> Dict[str, Any]:
        url = str(self.config.get("url", "")).strip()
        if not url:
            raise McpError("URL MCP server is missing url configuration")
        # Best-effort refresh existing OAuth token before request.
        try:
            self._ensure_oauth_token(timeout_s=min(10.0, timeout_s))
        except Exception:
            pass
        req_id = self.next_id
        self.next_id += 1
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self._base_headers()
        request = urllib.request.Request(url=url, data=body, headers=headers, method="POST")
        self._debug_log(
            "tx "
            + json.dumps(
                {
                    "url": url,
                    "method": method,
                    "id": req_id,
                    "headers": self._redact_obj(headers),
                    "body": self._redact_obj(payload),
                    "timeout_s": timeout_s,
                },
                ensure_ascii=False,
            )
        )
        content_type = ""
        raw = ""
        data: Any = None
        stream_chunks: List[str] = []
        response_target_url = url

        def _consume_jsonrpc_obj(obj: Any) -> Optional[Dict[str, Any]]:
            nonlocal stream_chunks
            if isinstance(obj, list):
                for item in obj:
                    out = _consume_jsonrpc_obj(item)
                    if out is not None:
                        return out
                return None
            if not isinstance(obj, dict):
                return None
            # Notifications / server requests from server.
            if "method" in obj and "result" not in obj and "error" not in obj:
                m = str(obj.get("method", "")).strip()
                p = obj.get("params", {})
                incoming_id = obj.get("id")
                if isinstance(p, dict):
                    chunk = _extract_tool_stream_chunk(m, p)
                    if chunk:
                        stream_chunks.append(chunk)
                # URL transport can carry server->client request frames in SSE.
                # When request id exists, best-effort dispatch and respond.
                if incoming_id is not None:
                    try:
                        if callable(self.peer_request_handler):
                            result = self.peer_request_handler(m, p if isinstance(p, dict) else {})
                            self._post_jsonrpc_response_to_url(
                                response_target_url,
                                incoming_id,
                                result=result,
                                timeout_s=min(10.0, timeout_s),
                            )
                        else:
                            self._post_jsonrpc_response_to_url(
                                response_target_url,
                                incoming_id,
                                error={"code": -32601, "message": f"Client method not supported: {m}"},
                                timeout_s=min(10.0, timeout_s),
                            )
                    except Exception as e:
                        try:
                            self._post_jsonrpc_response_to_url(
                                response_target_url,
                                incoming_id,
                                error={"code": -32000, "message": str(e)},
                                timeout_s=min(10.0, timeout_s),
                            )
                        except Exception:
                            pass
                return None
            if str(obj.get("id")) == str(req_id):
                return obj
            return None

        try:
            if self._is_legacy_sse_transport():
                response_target_url = self._ensure_legacy_sse_connection(timeout_s=timeout_s)
                post_request = urllib.request.Request(
                    url=response_target_url,
                    data=body,
                    headers=headers,
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(post_request, timeout=max(0.5, float(timeout_s))) as post_resp:
                        try:
                            sid = post_resp.headers.get("mcp-session-id")
                        except Exception:
                            sid = None
                        if sid:
                            self.session_id = sid
                        try:
                            post_ct = str(post_resp.headers.get("content-type", "") or "")
                        except Exception:
                            post_ct = ""
                        if "application/json" in post_ct.lower():
                            post_raw = post_resp.read().decode("utf-8", errors="replace").strip()
                            if post_raw:
                                raw = post_raw[:2000]
                                try:
                                    parsed = json.loads(post_raw)
                                    hit = _consume_jsonrpc_obj(parsed)
                                    if hit is not None:
                                        data = hit
                                except Exception:
                                    pass
                except urllib.error.HTTPError as e:
                    detail = ""
                    try:
                        detail = e.read().decode("utf-8", errors="replace")
                    except Exception:
                        detail = ""
                    if int(getattr(e, "code", 0) or 0) == 404 and "session" in detail.lower():
                        self._reset_legacy_sse_connection()
                        raise McpError(f"URL MCP HTTP error: {e.code} {e.reason}; {detail}")
                    raise

                if data is None:
                    raw_preview_parts: List[str] = []
                    deadline = time.time() + max(0.5, float(timeout_s))
                    while time.time() < deadline:
                        event_name, payload_text = self._legacy_sse_read_event(
                            timeout_s=max(0.5, deadline - time.time())
                        )
                        if not payload_text:
                            continue
                        if len(raw_preview_parts) < 20:
                            raw_preview_parts.append(payload_text[:200])
                        if event_name == "endpoint":
                            # server may rotate endpoint; update it.
                            self._legacy_sse_message_url = payload_text
                            response_target_url = payload_text
                            continue
                        try:
                            parsed = json.loads(payload_text)
                        except Exception:
                            continue
                        hit = _consume_jsonrpc_obj(parsed)
                        if hit is not None:
                            data = hit
                            break
                    if raw_preview_parts:
                        raw = "\n".join(raw_preview_parts)
            else:
                with urllib.request.urlopen(request, timeout=max(0.5, float(timeout_s))) as resp:
                    status_code = getattr(resp, "status", None)
                    sid = None
                    try:
                        sid = resp.headers.get("mcp-session-id")
                    except Exception:
                        sid = None
                    if sid:
                        self.session_id = sid
                    try:
                        content_type = str(resp.headers.get("content-type", "") or "")
                    except Exception:
                        content_type = ""
                    if "text/event-stream" in content_type.lower():
                        data_lines: List[str] = []
                        terminal_obj: Optional[Dict[str, Any]] = None
                        raw_preview_parts: List[str] = []

                        def _flush_event() -> None:
                            nonlocal data_lines, terminal_obj
                            if not data_lines:
                                return
                            payload_text = "\n".join(data_lines).strip()
                            data_lines = []
                            if not payload_text:
                                return
                            if len(raw_preview_parts) < 20:
                                raw_preview_parts.append(payload_text[:200])
                            try:
                                parsed = json.loads(payload_text)
                            except Exception:
                                return
                            hit = _consume_jsonrpc_obj(parsed)
                            if hit is not None:
                                terminal_obj = hit

                        while True:
                            line_b = resp.readline()
                            if not line_b:
                                break
                            line = line_b.decode("utf-8", errors="replace").rstrip("\r\n")
                            if line == "":
                                _flush_event()
                                if terminal_obj is not None:
                                    break
                                continue
                            if line.startswith("data:"):
                                data_lines.append(line[5:].strip())
                        _flush_event()

                        if raw_preview_parts:
                            raw = "\n".join(raw_preview_parts)
                        else:
                            raw = ""
                        data = terminal_obj
                    else:
                        raw = resp.read().decode("utf-8", errors="replace").strip()
                    try:
                        resp_headers = dict(resp.headers.items())
                    except Exception:
                        resp_headers = {}
                    self._debug_log(
                        "rx "
                        + json.dumps(
                            {
                                "status": status_code,
                                "content_type": content_type,
                                "headers": self._redact_obj(resp_headers),
                                "raw": self._redact_text(raw),
                            },
                            ensure_ascii=False,
                        )
                    )
        except urllib.error.HTTPError as e:
            detail = ""
            headers_map: Dict[str, str] = {}
            try:
                detail = e.read().decode("utf-8", errors="replace")
                if len(detail) > 600:
                    detail = detail[:600] + "..."
            except Exception:
                detail = ""
            try:
                headers_map = dict(e.headers.items()) if e.headers is not None else {}
            except Exception:
                headers_map = {}
            # OAuth 2.0 challenge flow (HTTP transport only).
            if allow_oauth_retry and int(getattr(e, "code", 0) or 0) == 401:
                try:
                    wa = str(headers_map.get("WWW-Authenticate", "") or "")
                    challenge = self._parse_www_authenticate(wa)
                    if challenge or self._oauth_conf():
                        self._oauth_authorize_interactive(url, challenge, timeout_s=max(15.0, float(timeout_s)))
                        # Retry original request once with new token.
                        return self._post_jsonrpc(method, params, timeout_s=timeout_s, allow_oauth_retry=False)
                    # Provider-specific/manual fallback when challenge is absent.
                    if self._manual_auth_fallback_enabled(url):
                        if self._is_current_manual_token_rejected():
                            raise McpError("Manually provided token was already verified as failed. Please replace it and retry")
                        if self._manual_token_prompt_and_store(url):
                            try:
                                return self._post_jsonrpc(method, params, timeout_s=timeout_s, allow_oauth_retry=False)
                            except Exception:
                                self._mark_current_manual_token_rejected()
                                raise McpError("Manually provided token is invalid or lacks permission. Please replace it and retry")
                except Exception as auth_e:
                    self._debug_log(f"oauth_flow_failed {repr(auth_e)}")
            # Scope step-up flow for runtime insufficient_scope challenges.
            if allow_oauth_retry and int(getattr(e, "code", 0) or 0) == 403:
                try:
                    wa = str(headers_map.get("WWW-Authenticate", "") or "")
                    if self._is_insufficient_scope_challenge(wa):
                        challenge = self._parse_www_authenticate(wa)
                        self._oauth_authorize_interactive(url, challenge, timeout_s=max(15.0, float(timeout_s)))
                        return self._post_jsonrpc(method, params, timeout_s=timeout_s, allow_oauth_retry=False)
                except Exception as auth_e:
                    self._debug_log(f"oauth_stepup_failed {repr(auth_e)}")
            self._debug_log(
                "http_error "
                + json.dumps(
                    {
                        "status": e.code,
                        "reason": str(e.reason),
                        "headers": self._redact_obj(headers_map),
                        "body": self._redact_text(detail),
                    },
                    ensure_ascii=False,
                )
            )
            raise McpError(f"URL MCP HTTP error: {e.code} {e.reason}" + (f"; {detail}" if detail else ""))
        except Exception as e:
            if self._is_legacy_sse_transport() and self._is_session_lost_error(e):
                self._reset_legacy_sse_connection()
            self._debug_log(f"transport_error {repr(e)}")
            raise McpError(f"URL MCP request failed: {e}")

        if data is None and not raw:
            raise McpError("URL MCP returned empty response")
        if data is None:
            try:
                data = json.loads(raw)
            except Exception as e:
                # Some MCP HTTP servers may respond in SSE-like format:
                # event: message\n
                # data: {...}\n\n
                data = None
                if "data:" in raw or "text/event-stream" in content_type.lower():
                    for line in raw.splitlines():
                        s = line.strip()
                        if not s.startswith("data:"):
                            continue
                        payload = s[5:].strip()
                        if not payload:
                            continue
                        try:
                            parsed = json.loads(payload)
                        except Exception:
                            continue
                        hit = _consume_jsonrpc_obj(parsed)
                        if hit is not None:
                            data = hit
                            break
                if data is None:
                    sample = self._redact_text(raw[:1200].replace("\n", "\\n").replace("\r", "\\r"))
                    self._debug_log(f"parse_error sample={sample}")
                    raise McpError(f"URL MCP response is not JSON: {e}; sample={sample}")
        if isinstance(data, list):
            # pick matching id entry if server returned batch
            for item in data:
                if isinstance(item, dict) and str(item.get("id")) == str(req_id):
                    data = item
                    break
        if not isinstance(data, dict):
            raise McpError("URL MCP response format is invalid")
        if "error" in data:
            raise McpError(str(data.get("error")))
        result = data.get("result", {})
        out = result if isinstance(result, dict) else {"value": result}
        if stream_chunks:
            out["_stream"] = {
                "chunks": stream_chunks,
                "text": "".join(stream_chunks),
                "chunk_count": len(stream_chunks),
            }
            if not out.get("content"):
                out["content"] = [{"type": "text", "text": out["_stream"]["text"]}]
        return out

    def _post_jsonrpc_response(
        self,
        req_id: Any,
        *,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
        timeout_s: float = 8.0,
        allow_oauth_retry: bool = True,
    ) -> None:
        url = str(self.config.get("url", "")).strip()
        if not url:
            raise McpError("URL MCP server is missing url configuration")
        if self._is_legacy_sse_transport():
            target_url = self._legacy_sse_message_url.strip() or self._ensure_legacy_sse_connection(timeout_s=timeout_s)
            return self._post_jsonrpc_response_to_url(
                target_url, req_id, result=result, error=error, timeout_s=timeout_s
            )
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result if isinstance(result, dict) else {}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self._base_headers()
        request = urllib.request.Request(url=url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=max(0.5, float(timeout_s))) as resp:
                try:
                    sid = resp.headers.get("mcp-session-id")
                except Exception:
                    sid = None
                if sid:
                    self.session_id = sid
                try:
                    _ = resp.read()
                except Exception:
                    pass
        except urllib.error.HTTPError as e:
            if allow_oauth_retry and int(getattr(e, "code", 0) or 0) == 401:
                headers_map: Dict[str, str] = {}
                try:
                    headers_map = dict(e.headers.items()) if e.headers is not None else {}
                except Exception:
                    headers_map = {}
                wa = str(headers_map.get("WWW-Authenticate", "") or "")
                challenge = self._parse_www_authenticate(wa)
                self._oauth_authorize_interactive(url, challenge, timeout_s=max(15.0, float(timeout_s)))
                return self._post_jsonrpc_response(
                    req_id,
                    result=result,
                    error=error,
                    timeout_s=timeout_s,
                    allow_oauth_retry=False,
                )
            if allow_oauth_retry and int(getattr(e, "code", 0) or 0) == 403:
                headers_map: Dict[str, str] = {}
                try:
                    headers_map = dict(e.headers.items()) if e.headers is not None else {}
                except Exception:
                    headers_map = {}
                wa = str(headers_map.get("WWW-Authenticate", "") or "")
                if self._is_insufficient_scope_challenge(wa):
                    challenge = self._parse_www_authenticate(wa)
                    self._oauth_authorize_interactive(url, challenge, timeout_s=max(15.0, float(timeout_s)))
                    return self._post_jsonrpc_response(
                        req_id,
                        result=result,
                        error=error,
                        timeout_s=timeout_s,
                        allow_oauth_retry=False,
                    )
            raise

    def _post_jsonrpc_batch(
        self,
        calls: List[Tuple[str, Optional[Dict[str, Any]]]],
        timeout_s: float,
        *,
        allow_partial_failure: bool = False,
        allow_oauth_retry: bool = True,
    ) -> List[Dict[str, Any]]:
        if self._is_legacy_sse_transport():
            if not calls:
                return []
            req_payloads: List[Dict[str, Any]] = []
            id_to_idx: Dict[str, int] = {}
            for idx, (method, params) in enumerate(calls):
                req_id = self.next_id
                self.next_id += 1
                payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": str(method)}
                if params is not None:
                    payload["params"] = params
                req_payloads.append(payload)
                id_to_idx[str(req_id)] = idx
            target_url = self._ensure_legacy_sse_connection(timeout_s=timeout_s)
            body = json.dumps(req_payloads, ensure_ascii=False).encode("utf-8")
            headers = self._base_headers()
            request = urllib.request.Request(url=target_url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=max(0.5, float(timeout_s))) as resp:
                    try:
                        sid = resp.headers.get("mcp-session-id")
                    except Exception:
                        sid = None
                    if sid:
                        self.session_id = sid
                    try:
                        post_ct = str(resp.headers.get("content-type", "") or "")
                    except Exception:
                        post_ct = ""
                    if "application/json" in post_ct.lower():
                        post_raw = resp.read().decode("utf-8", errors="replace").strip()
                        if post_raw:
                            try:
                                parsed = json.loads(post_raw)
                                parsed_items = parsed if isinstance(parsed, list) else [parsed]
                                direct_results: List[Optional[Dict[str, Any]]] = [None] * len(calls)
                                for it in parsed_items:
                                    if not isinstance(it, dict):
                                        continue
                                    msg_id = str(it.get("id"))
                                    if msg_id not in id_to_idx:
                                        continue
                                    if "error" in it:
                                        if allow_partial_failure:
                                            direct_results[id_to_idx[msg_id]] = {"ok": False, "error": it.get("error")}
                                            continue
                                        raise McpError(str(it.get("error")))
                                    idx = id_to_idx[msg_id]
                                    r = it.get("result", {})
                                    if not isinstance(r, dict):
                                        r = {"value": r}
                                    direct_results[idx] = {"ok": True, "result": r} if allow_partial_failure else r
                                if all(x is not None for x in direct_results):
                                    return [x if isinstance(x, dict) else {} for x in direct_results]
                            except Exception:
                                pass
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", errors="replace")
                except Exception:
                    detail = ""
                if int(getattr(e, "code", 0) or 0) == 404 and "session" in detail.lower():
                    self._reset_legacy_sse_connection()
                # Legacy SSE servers may reject array POST; fallback to sequential.
                if int(getattr(e, "code", 0) or 0) not in (400, 404, 405):
                    raise McpError(f"URL MCP HTTP error: {e.code} {e.reason}" + (f"; {detail}" if detail else ""))
            except Exception:
                # fallback to sequential mode below
                pass

            # Fallback: sequential calls (server may not support JSON-RPC array on message endpoint).
            results: List[Dict[str, Any]] = []
            for method, params in calls:
                try:
                    item = self._post_jsonrpc(str(method), params if isinstance(params, dict) else {}, timeout_s=timeout_s)
                    results.append({"ok": True, "result": item} if allow_partial_failure else item)
                except Exception as e:
                    if allow_partial_failure:
                        results.append({"ok": False, "error": str(e)})
                    else:
                        raise
            return results
        url = str(self.config.get("url", "")).strip()
        if not url:
            raise McpError("URL MCP server is missing url configuration")
        try:
            self._ensure_oauth_token(timeout_s=min(10.0, timeout_s))
        except Exception:
            pass
        if not calls:
            return []
        payloads: List[Dict[str, Any]] = []
        id_to_idx: Dict[str, int] = {}
        for idx, (method, params) in enumerate(calls):
            req_id = self.next_id
            self.next_id += 1
            p: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": str(method)}
            if params is not None:
                p["params"] = params
            payloads.append(p)
            id_to_idx[str(req_id)] = idx
        body = json.dumps(payloads, ensure_ascii=False).encode("utf-8")
        headers = self._base_headers()
        request = urllib.request.Request(url=url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=max(0.5, float(timeout_s))) as resp:
                sid = None
                try:
                    sid = resp.headers.get("mcp-session-id")
                except Exception:
                    sid = None
                if sid:
                    self.session_id = sid
                raw = resp.read().decode("utf-8", errors="replace").strip()
        except urllib.error.HTTPError as e:
            detail = ""
            headers_map: Dict[str, str] = {}
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            try:
                headers_map = dict(e.headers.items()) if e.headers is not None else {}
            except Exception:
                headers_map = {}
            if allow_oauth_retry and int(getattr(e, "code", 0) or 0) == 401:
                wa = str(headers_map.get("WWW-Authenticate", "") or "")
                challenge = self._parse_www_authenticate(wa)
                if challenge or self._oauth_conf():
                    self._oauth_authorize_interactive(url, challenge, timeout_s=max(15.0, float(timeout_s)))
                    return self._post_jsonrpc_batch(
                        calls,
                        timeout_s=timeout_s,
                        allow_partial_failure=allow_partial_failure,
                        allow_oauth_retry=False,
                    )
                if self._manual_auth_fallback_enabled(url):
                    if self._is_current_manual_token_rejected():
                        raise McpError("Manually provided token was already verified as failed. Please replace it and retry")
                    if self._manual_token_prompt_and_store(url):
                        try:
                            return self._post_jsonrpc_batch(
                                calls,
                                timeout_s=timeout_s,
                                allow_partial_failure=allow_partial_failure,
                                allow_oauth_retry=False,
                            )
                        except Exception:
                            self._mark_current_manual_token_rejected()
                            raise McpError("Manually provided token is invalid or lacks permission. Please replace it and retry")
            if allow_oauth_retry and int(getattr(e, "code", 0) or 0) == 403:
                wa = str(headers_map.get("WWW-Authenticate", "") or "")
                if self._is_insufficient_scope_challenge(wa):
                    challenge = self._parse_www_authenticate(wa)
                    self._oauth_authorize_interactive(url, challenge, timeout_s=max(15.0, float(timeout_s)))
                    return self._post_jsonrpc_batch(
                        calls,
                        timeout_s=timeout_s,
                        allow_partial_failure=allow_partial_failure,
                        allow_oauth_retry=False,
                    )
            raise McpError(f"URL MCP HTTP error: {e.code} {e.reason}" + (f"; {detail}" if detail else ""))
        except Exception as e:
            raise McpError(f"URL MCP request failed: {e}")
        if not raw:
            raise McpError("URL MCP returned empty response")
        try:
            data = json.loads(raw)
        except Exception as e:
            raise McpError(f"URL MCP response is not JSON: {e}")
        items = data if isinstance(data, list) else [data]
        results: List[Optional[Dict[str, Any]]] = [None] * len(calls)
        for it in items:
            if not isinstance(it, dict):
                continue
            msg_id = str(it.get("id"))
            if msg_id not in id_to_idx:
                continue
            if "error" in it:
                if allow_partial_failure:
                    results[id_to_idx[msg_id]] = {"ok": False, "error": it.get("error")}
                    continue
                raise McpError(str(it.get("error")))
            idx = id_to_idx[msg_id]
            r = it.get("result", {})
            if not isinstance(r, dict):
                r = {"value": r}
            results[idx] = {"ok": True, "result": r} if allow_partial_failure else r
        missing = [i for i, r in enumerate(results) if r is None]
        if missing:
            raise McpError(f"URL MCP batch response missing: {missing}")
        if allow_partial_failure:
            return [r if isinstance(r, dict) else {"ok": False, "error": "missing"} for r in results]
        return [r if isinstance(r, dict) else {} for r in results]

    def initialize(self, timeout_s: float = 8.0) -> None:
        if self.initialized:
            return
        protocol_candidates = ["2025-11-25", "2024-11-05", "2024-10-07", "2024-06-01"]
        last_error = None
        for pv in protocol_candidates:
            try:
                result = self._post_jsonrpc(
                    "initialize",
                    {
                        "protocolVersion": pv,
                        "clientInfo": {"name": get_app_client_name(), "version": get_app_version()},
                        "capabilities": {"elicitation": {"form": {}}},
                    },
                    timeout_s=timeout_s,
                )
                self.initialize_instructions = _extract_initialize_instructions(result)
                self.initialized = True
                # best-effort initialized notification
                try:
                    self._post_jsonrpc("notifications/initialized", {}, timeout_s=min(3.0, timeout_s))
                except Exception:
                    pass
                return
            except Exception as e:
                last_error = str(e)
                # Some HTTP MCP servers are stateful and may return
                # "Server already initialized" for repeated initialize calls.
                if "already initialized" in last_error.lower():
                    self.initialized = True
                    return
                continue
        raise McpError(f"URL MCP initialize failed: {last_error or 'unknown'}")

    def list_tools(self, timeout_s: float = 8.0) -> List[Dict[str, Any]]:
        with self.lock:
            self.initialize(timeout_s=timeout_s)
            try:
                result = self._post_jsonrpc("tools/list", {}, timeout_s=timeout_s)
            except Exception as e:
                # Recover once when server-side session is lost.
                if not self._is_session_lost_error(e):
                    raise
                self._debug_log("session_lost on tools/list, reinitialize and retry once")
                self._reset_session_state()
                self.initialize(timeout_s=timeout_s)
                result = self._post_jsonrpc("tools/list", {}, timeout_s=timeout_s)
            tools = result.get("tools", [])
            return tools if isinstance(tools, list) else []

    def call_tool(self, tool_name: str, arguments: Dict[str, Any], timeout_s: float = 20.0) -> Dict[str, Any]:
        with self.lock:
            self.initialize(timeout_s=timeout_s)
            try:
                return self._post_jsonrpc(
                    "tools/call",
                    {"name": tool_name, "arguments": arguments or {}},
                    timeout_s=timeout_s,
                )
            except Exception as e:
                # Recover once when server-side session is lost.
                if not self._is_session_lost_error(e):
                    raise
                self._debug_log(
                    f"session_lost on tools/call name={tool_name}, reinitialize and retry once"
                )
                self._reset_session_state()
                self.initialize(timeout_s=timeout_s)
                return self._post_jsonrpc(
                    "tools/call",
                    {"name": tool_name, "arguments": arguments or {}},
                    timeout_s=timeout_s,
                )

    def call_tools_batch(
        self, calls: List[Dict[str, Any]], timeout_s: float = 30.0, *, allow_partial_failure: bool = False
    ) -> List[Dict[str, Any]]:
        with self.lock:
            self.initialize(timeout_s=timeout_s)
            reqs: List[Tuple[str, Optional[Dict[str, Any]]]] = []
            for c in calls:
                if not isinstance(c, dict):
                    continue
                reqs.append(
                    (
                        "tools/call",
                        {
                            "name": str(c.get("tool", "")),
                            "arguments": c.get("arguments", {}) if isinstance(c.get("arguments", {}), dict) else {},
                        },
                    )
                )
            try:
                return self._post_jsonrpc_batch(
                    reqs, timeout_s=timeout_s, allow_partial_failure=allow_partial_failure
                )
            except Exception as e:
                if not self._is_session_lost_error(e):
                    raise
                self._debug_log("session_lost on tools/call batch, reinitialize and retry once")
                self._reset_session_state()
                self.initialize(timeout_s=timeout_s)
                return self._post_jsonrpc_batch(
                    reqs, timeout_s=timeout_s, allow_partial_failure=allow_partial_failure
                )

    def list_resources(self, timeout_s: float = 8.0) -> List[Dict[str, Any]]:
        with self.lock:
            self.initialize(timeout_s=timeout_s)
            try:
                result = self._post_jsonrpc("resources/list", {}, timeout_s=timeout_s)
            except Exception as e:
                if not self._is_session_lost_error(e):
                    raise
                self._debug_log("session_lost on resources/list, reinitialize and retry once")
                self._reset_session_state()
                self.initialize(timeout_s=timeout_s)
                result = self._post_jsonrpc("resources/list", {}, timeout_s=timeout_s)
            resources = result.get("resources", [])
            return resources if isinstance(resources, list) else []

    def read_resource(self, uri: str, timeout_s: float = 20.0) -> Dict[str, Any]:
        with self.lock:
            self.initialize(timeout_s=timeout_s)
            params = {"uri": str(uri)}
            try:
                return self._post_jsonrpc("resources/read", params, timeout_s=timeout_s)
            except Exception as e:
                if not self._is_session_lost_error(e):
                    raise
                self._debug_log("session_lost on resources/read, reinitialize and retry once")
                self._reset_session_state()
                self.initialize(timeout_s=timeout_s)
                return self._post_jsonrpc("resources/read", params, timeout_s=timeout_s)

    def list_resource_templates(self, timeout_s: float = 8.0) -> List[Dict[str, Any]]:
        with self.lock:
            self.initialize(timeout_s=timeout_s)
            try:
                result = self._post_jsonrpc("resources/templates/list", {}, timeout_s=timeout_s)
            except Exception as e:
                if not self._is_session_lost_error(e):
                    raise
                self._debug_log("session_lost on resources/templates/list, reinitialize and retry once")
                self._reset_session_state()
                self.initialize(timeout_s=timeout_s)
                result = self._post_jsonrpc("resources/templates/list", {}, timeout_s=timeout_s)
            templates = result.get("resourceTemplates")
            if not isinstance(templates, list):
                templates = result.get("templates", [])
            return templates if isinstance(templates, list) else []

    def list_prompts(self, timeout_s: float = 8.0) -> List[Dict[str, Any]]:
        with self.lock:
            self.initialize(timeout_s=timeout_s)
            try:
                result = self._post_jsonrpc("prompts/list", {}, timeout_s=timeout_s)
            except Exception as e:
                if not self._is_session_lost_error(e):
                    raise
                self._debug_log("session_lost on prompts/list, reinitialize and retry once")
                self._reset_session_state()
                self.initialize(timeout_s=timeout_s)
                result = self._post_jsonrpc("prompts/list", {}, timeout_s=timeout_s)
            prompts = result.get("prompts", [])
            return prompts if isinstance(prompts, list) else []

    def get_prompt(self, prompt_name: str, arguments: Dict[str, Any], timeout_s: float = 20.0) -> Dict[str, Any]:
        with self.lock:
            self.initialize(timeout_s=timeout_s)
            params = {"name": str(prompt_name), "arguments": arguments or {}}
            try:
                return self._post_jsonrpc("prompts/get", params, timeout_s=timeout_s)
            except Exception as e:
                if not self._is_session_lost_error(e):
                    raise
                self._debug_log("session_lost on prompts/get, reinitialize and retry once")
                self._reset_session_state()
                self.initialize(timeout_s=timeout_s)
                return self._post_jsonrpc("prompts/get", params, timeout_s=timeout_s)

    def sampling_create_message(self, params: Dict[str, Any], timeout_s: float = 30.0) -> Dict[str, Any]:
        with self.lock:
            self.initialize(timeout_s=timeout_s)
            payload = params if isinstance(params, dict) else {}
            try:
                return self._post_jsonrpc("sampling/createMessage", payload, timeout_s=timeout_s)
            except Exception as e:
                if not self._is_session_lost_error(e):
                    raise
                self._debug_log("session_lost on sampling/createMessage, reinitialize and retry once")
                self._reset_session_state()
                self.initialize(timeout_s=timeout_s)
                return self._post_jsonrpc("sampling/createMessage", payload, timeout_s=timeout_s)

    def completion_complete(self, params: Dict[str, Any], timeout_s: float = 20.0) -> Dict[str, Any]:
        with self.lock:
            self.initialize(timeout_s=timeout_s)
            payload = params if isinstance(params, dict) else {}
            try:
                return self._post_jsonrpc("completion/complete", payload, timeout_s=timeout_s)
            except Exception as e:
                if not self._is_session_lost_error(e):
                    raise
                self._debug_log("session_lost on completion/complete, reinitialize and retry once")
                self._reset_session_state()
                self.initialize(timeout_s=timeout_s)
                return self._post_jsonrpc("completion/complete", payload, timeout_s=timeout_s)


    def _shutdown_unlocked(self) -> None:
        """
        Keep interface parity with stdio client.
        URL transport has no child process to terminate; reset session state only.
        """
        with self.lock:
            self._reset_session_state()


class McpManager:
    def __init__(
        self,
        config_dir: Path,
        mcp_config: Dict[str, Any],
        workspace_dir: Optional[Path] = None,
        tool_policy_parent: Optional[Path] = None,
        language: str = DEFAULT_DISPLAY_LANGUAGE,
    ):
        self.config_dir = Path(config_dir)
        self.workspace_dir = Path(workspace_dir) if workspace_dir else self.config_dir / "workspace"
        self.mcp_config = mcp_config or {}
        self.display_language = normalize_display_language(language) or DEFAULT_DISPLAY_LANGUAGE
        self._clients: Dict[str, McpServerClient] = {}
        self._tools_cache: Dict[str, Dict[str, Any]] = {}
        self._resources_cache: Dict[str, Dict[str, Any]] = {}
        self._resource_templates_cache: Dict[str, Dict[str, Any]] = {}
        self._prompts_cache: Dict[str, Dict[str, Any]] = {}
        self._client_method_handlers: Dict[str, Callable[[str, Dict[str, Any]], Dict[str, Any]]] = {}
        self._status: Dict[str, Dict[str, Any]] = {}
        self._active_ops: Dict[str, int] = {}
        self._preload_thread: Optional[threading.Thread] = None
        self._preload_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._policy_lock = threading.Lock()
        _policy_parent = Path(tool_policy_parent) if tool_policy_parent is not None else self.config_dir
        self._tool_policy_path = _policy_parent / "mcp_tool_policy.json"
        self._disabled_tools_by_server: Dict[str, set[str]] = self._load_disabled_tools_policy()
        self._logger = self._build_logger()
        self._recent_logs: "deque[str]" = deque(maxlen=200)
        self._init_server_status()

    def _load_disabled_tools_policy(self) -> Dict[str, set[str]]:
        out: Dict[str, set[str]] = {}
        try:
            if not self._tool_policy_path.exists():
                return out
            raw = json.loads(self._tool_policy_path.read_text(encoding="utf-8") or "{}")
            servers = raw.get("servers", {}) if isinstance(raw, dict) else {}
            if not isinstance(servers, dict):
                return out
            for server, conf in servers.items():
                if not isinstance(server, str) or not isinstance(conf, dict):
                    continue
                disabled = conf.get("disabled_tools", [])
                if not isinstance(disabled, list):
                    continue
                names = {str(x).strip() for x in disabled if str(x).strip()}
                if names:
                    out[server] = names
        except Exception as e:
            self._log("WARNING", f"load mcp tool policy failed: {e}")
        return out

    def _save_disabled_tools_policy(self) -> None:
        data: Dict[str, Any] = {"servers": {}}
        with self._policy_lock:
            for server, names in self._disabled_tools_by_server.items():
                if not names:
                    continue
                data["servers"][server] = {
                    "disabled_tools": sorted(names),
                }
        self._tool_policy_path.parent.mkdir(parents=True, exist_ok=True)
        self._tool_policy_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _filter_disabled_tools(self, server: str, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        with self._policy_lock:
            disabled = set(self._disabled_tools_by_server.get(str(server), set()))
        if not disabled:
            return [t for t in tools if isinstance(t, dict)]
        filtered: List[Dict[str, Any]] = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name", "")).strip()
            if name and name in disabled:
                continue
            filtered.append(t)
        return filtered

    def is_tool_disabled(self, server: str, tool_name: str) -> bool:
        name = str(tool_name or "").strip()
        if not name:
            return False
        with self._policy_lock:
            return name in self._disabled_tools_by_server.get(str(server), set())

    def list_disabled_tools(self, server: Optional[str] = None) -> Dict[str, List[str]]:
        with self._policy_lock:
            if server is not None and str(server).strip():
                s = str(server).strip()
                return {s: sorted(self._disabled_tools_by_server.get(s, set()))}
            return {
                s: sorted(names)
                for s, names in self._disabled_tools_by_server.items()
                if names
            }

    def list_tools_with_disabled(
        self, server: str, timeout_s: float = 8.0, use_cache: bool = True
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """
        Return full tools list for a server and annotate disabled tools with
        display suffix "(disabled)" for reporting.
        Unlike list_tools(), this method does NOT hide disabled tools.
        """
        srv = str(server or "").strip()
        if not srv:
            raise McpError("Missing server")

        from_cache = False
        if use_cache and srv in self._tools_cache:
            from_cache = True
        else:
            # Ensure raw cache exists; list_tools applies filtering only to return value.
            self.list_tools(srv, timeout_s=timeout_s, use_cache=False)
            from_cache = False

        raw_tools = self._tools_cache.get(srv, {}).get("tools", [])
        tools = raw_tools if isinstance(raw_tools, list) else []
        with self._policy_lock:
            disabled = set(self._disabled_tools_by_server.get(srv, set()))

        annotated: List[Dict[str, Any]] = []
        for item in tools:
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            name = str(copied.get("name", "")).strip()
            is_disabled = bool(name and name in disabled)
            copied["disabled"] = is_disabled
            copied["display_name"] = f"{name} (disabled)" if is_disabled and name else name
            annotated.append(copied)
        return annotated, from_cache

    def disable_tools(self, server: str, tool_names: List[str]) -> List[str]:
        srv = str(server or "").strip()
        if not srv:
            raise McpError("Missing server")
        self._server_conf(srv)
        norm = sorted({str(x).strip() for x in tool_names if str(x).strip()})
        if not norm:
            return sorted(self._disabled_tools_by_server.get(srv, set()))
        with self._policy_lock:
            cur = set(self._disabled_tools_by_server.get(srv, set()))
            cur.update(norm)
            self._disabled_tools_by_server[srv] = cur
        self._save_disabled_tools_policy()
        return sorted(self._disabled_tools_by_server.get(srv, set()))

    def enable_tools(self, server: str, tool_names: List[str]) -> List[str]:
        srv = str(server or "").strip()
        if not srv:
            raise McpError("Missing server")
        self._server_conf(srv)
        norm = {str(x).strip() for x in tool_names if str(x).strip()}
        with self._policy_lock:
            cur = set(self._disabled_tools_by_server.get(srv, set()))
            if norm:
                cur -= norm
            if cur:
                self._disabled_tools_by_server[srv] = cur
            else:
                self._disabled_tools_by_server.pop(srv, None)
        self._save_disabled_tools_policy()
        return sorted(self._disabled_tools_by_server.get(srv, set()))

    def _build_logger(self) -> logging.Logger:
        logs_dir = self.config_dir / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        logger = logging.getLogger(_MCP_LOGGER_NAME)
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

    @staticmethod
    def _server_conf_fingerprint(conf: Any) -> str:
        try:
            return json.dumps(conf, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except Exception:
            return str(conf)

    def _remove_server_runtime(self, server: str) -> None:
        if server in self._clients:
            try:
                self._clients[server]._shutdown_unlocked()
            except Exception:
                pass
            self._clients.pop(server, None)
        self._tools_cache.pop(server, None)
        self._resources_cache.pop(server, None)
        self._resource_templates_cache.pop(server, None)
        self._prompts_cache.pop(server, None)
        with self._status_lock:
            self._status.pop(server, None)
            self._active_ops.pop(server, None)
        self._log("INFO", f"mcp server unloaded: {server}")

    def apply_config_changes(self, new_config: Dict[str, Any], timeout_s: float = 12.0) -> Dict[str, Any]:
        """
        Apply new mcp config incrementally:
        - load newly added servers
        - reconnect changed servers
        - unload removed servers
        """
        old_servers = self.mcp_config.get("mcpServers", {})
        old_servers = old_servers if isinstance(old_servers, dict) else {}
        incoming = (new_config or {}).get("mcpServers", {})
        new_servers = incoming if isinstance(incoming, dict) else {}

        old_keys = set(str(k) for k in old_servers.keys())
        new_keys = set(str(k) for k in new_servers.keys())
        added = sorted(new_keys - old_keys)
        removed = sorted(old_keys - new_keys)
        maybe_common = sorted(old_keys & new_keys)

        changed: List[str] = []
        for s in maybe_common:
            if self._server_conf_fingerprint(old_servers.get(s)) != self._server_conf_fingerprint(new_servers.get(s)):
                changed.append(s)

        # Switch to new config first so subsequent reconnect uses new params.
        self.mcp_config = {"mcpServers": dict(new_servers)}

        # Ensure status entries exist for new/changed servers.
        now = time.time()
        with self._status_lock:
            for s in sorted(set(added + changed)):
                self._status.setdefault(
                    s,
                    {
                        "state": "pending",
                        "last_error": "",
                        "last_updated_ts": now,
                        "loading_since_ts": 0.0,
                        "tool_count": 0,
                        "source": "",
                        "is_cached": False,
                        "failure_type": "",
                        "suggestion": "",
                    },
                )
                self._active_ops.setdefault(s, 0)

        for s in removed:
            self._remove_server_runtime(s)

        reloaded: List[str] = []
        skipped: List[str] = []
        failed: Dict[str, str] = {}

        targets = sorted(set(added + changed))
        for s in targets:
            conf = new_servers.get(s, {})
            if isinstance(conf, dict) and bool(conf.get("skip_preload", False)):
                self._set_status(
                    s,
                    "skipped",
                    last_error="skip_preload=true",
                    failure_type="",
                    suggestion="This server is configured with skip_preload=true; set it to false if you need automatic preload.",
                )
                skipped.append(s)
                continue
            try:
                self.reconnect_server(s, timeout_s=timeout_s)
                reloaded.append(s)
            except Exception as e:
                failed[s] = str(e)
                self._log("WARNING", f"reload server failed during config change: {s}: {e}")

        return {
            "added": added,
            "removed": removed,
            "changed": changed,
            "reloaded": reloaded,
            "skipped": skipped,
            "failed": failed,
        }

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
        err_text = str(err or "")
        e = err_text.lower()
        conf = self._server_conf(server)
        # Layer 1: prefer structured JSON-RPC error.code classification.
        # Accept common stringified formats:
        # - {"code": -32601, ...}
        # - {'code': -32601, ...}
        # - code=-32601 / code: -32601
        code: Optional[int] = None
        m = re.search(r"[\"']code[\"']\s*[:=]\s*(-?\d+)", err_text, flags=re.IGNORECASE)
        if m is None:
            m = re.search(r"\bcode\s*[:=]\s*(-?\d+)\b", err_text, flags=re.IGNORECASE)
        if m is not None:
            try:
                code = int(m.group(1))
            except Exception:
                code = None
        if code in (-32601, -32602, -32600):
            return (
                "unsupported",
                "MCP returned error.code, indicating the current method/arguments are unavailable on this server; verify capabilities and method arguments, and upgrade or switch server if needed.",
            )
        if code is not None and -32099 <= code <= -32000:
            return (
                "connect_failed",
                "MCP returned server error (-320xx); check server status, connection config, and logs, then retry.",
            )
        if code == -32700:
            return (
                "connect_failed",
                "MCP returned parse error (-32700); check whether transport and request format are complete.",
            )
        # Layer 2: fallback to keyword-based heuristics.
        # Capability/method unsupported cases should be surfaced explicitly
        # instead of falling through to generic connect_failed.
        unsupported_markers = (
            "method not found",
            "this server request method is not supported",
            "not implemented",
            "unsupported",
            "unsupported",
            "unknown method",
            "unknown tool",
            "unknown prompt",
            "-32601",
            "resources/list failed",
            "resources/templates/list failed",
            "prompts/list failed",
        )
        if any(m in e for m in unsupported_markers):
            return (
                "unsupported",
                "This MCP server/client combination does not currently support this capability or method; verify server capabilities and tool/method names, and upgrade or switch to a supporting server if needed.",
            )
        if "session not found" in e or "invalid session" in e or "unknown session" in e:
            return (
                "connect_failed",
                "URL MCP session expired: automatic reconnect has been recommended; retry the operation or run mcp_reconnect to refresh the session.",
            )
        if "url" in conf or "url is not currently supported" in e:
            return (
                "connect_failed",
                "URL MCP connection/authentication failed: check URL reachability and headers/token config, or temporarily set skip_preload=true.",
            )
        if "winerror 2" in e or "cannot find the file specified" in e or "no such file or directory" in e:
            cmd = str(conf.get("command", "")).strip() or "<unknown>"
            return (
                "missing_dependency",
                f"Executable `{cmd}` was not found; install dependencies and ensure PATH visibility, or set an absolute path in mcp.jsonc.",
            )
        return (
            "connect_failed",
            "Connection/handshake failed: increase timeout, run mcp_reconnect, or set skip_preload=true in mcp.jsonc to skip startup preload.",
        )

    def _log(self, level: str, message: str) -> None:
        safe_message = _redact_text(message, max_len=3000)
        line = f"[{level}] {safe_message}"
        self._recent_logs.append(line)
        if level == "WARNING":
            self._logger.warning(safe_message)
        elif level == "ERROR":
            self._logger.error(safe_message)
        else:
            self._logger.info(safe_message)

    def _server_conf(self, server: str) -> Dict[str, Any]:
        servers = self.mcp_config.get("mcpServers", {})
        if not isinstance(servers, dict) or server not in servers:
            raise McpError(f"MCP server is not configured: {server}")
        conf = servers[server]
        if not isinstance(conf, dict):
            raise McpError(f"MCP server configuration is invalid: {server}")
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

    def _dispatch_server_request_to_client(self, server: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        method_name = str(method or "").strip()
        payload = params if isinstance(params, dict) else {}
        handler = self._client_method_handlers.get(method_name)
        if callable(handler):
            return handler(str(server), payload)
        if method_name == "sampling/createMessage":
            # Safe default fallback for bidirectional sampling.
            messages = payload.get("messages", [])
            max_tokens = payload.get("maxTokens", 256)
            if not isinstance(max_tokens, int):
                max_tokens = 256
            last_text = ""
            if isinstance(messages, list) and messages:
                last = messages[-1]
                if isinstance(last, dict):
                    content = last.get("content")
                    if isinstance(content, dict):
                        last_text = str(content.get("text", ""))
                    elif isinstance(content, str):
                        last_text = content
            return {
                "model": get_app_client_model_name(),
                "role": "assistant",
                "content": {"type": "text", "text": f"[client-sampled maxTokens={max_tokens}] {last_text}".strip()},
                "stopReason": "endTurn",
            }
        if method_name == "elicitation/create":
            # Safe default fallback for bidirectional elicitation (form mode only).
            mode = str(payload.get("mode", "form") or "form").strip().lower()
            if mode not in ("", "form"):
                raise McpError(f"Unsupported elicitation mode: {mode}")
            requested = payload.get("requestedSchema")
            if not isinstance(requested, dict):
                requested = {}
            props = requested.get("properties")
            content: Dict[str, Any] = {}
            if isinstance(props, dict):
                for key in props.keys():
                    k = str(key).strip()
                    if k:
                        content[k] = ""
            return {
                "action": "accept",
                "content": content,
            }
        raise McpError(f"Client does not support this server request method: {method_name}")

    def register_client_method_handler(
        self, method: str, handler: Callable[[str, Dict[str, Any]], Dict[str, Any]]
    ) -> None:
        key = str(method or "").strip()
        if not key:
            raise McpError("method cannot be empty")
        if not callable(handler):
            raise McpError("handler must be callable")
        self._client_method_handlers[key] = handler

    def _client(self, server: str) -> Any:
        if server not in self._clients:
            conf = self._server_conf(server)
            if "url" in conf:
                self._clients[server] = McpUrlClient(
                    name=server,
                    config=conf,
                    token_store_path=str(self.config_dir / "oauth_tokens.json"),
                )
                self._clients[server].peer_request_handler = (
                    lambda method, params, _server=server: self._dispatch_server_request_to_client(
                        _server, method, params
                    )
                )
            else:
                self._clients[server] = McpServerClient(name=server, config=conf)
                self._clients[server].peer_request_handler = (
                    lambda method, params, _server=server: self._dispatch_server_request_to_client(
                        _server, method, params
                    )
                )
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
            tools_raw = cached.get("tools", [])
            tools = self._filter_disabled_tools(
                str(server), tools_raw if isinstance(tools_raw, list) else []
            )
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
                suggestion="Cache is not ready; check mcp_status first, or fetch actively with use_cache=false.",
            )
            raise McpError("Cache miss (use_cache=true); skipped live connection")
        self._mark_op(server, +1)
        self._set_status(server, "loading")
        timeout_s = self._effective_timeout(server, timeout_s)
        self._log("INFO", f"start list_tools server={server}, timeout={timeout_s:.1f}, use_cache={use_cache}")
        last_error: Optional[str] = None
        for _ in range(2):
            try:
                tools = self._client(server).list_tools(timeout_s=timeout_s)
                conf = self._server_conf(server)
                source = "url" if isinstance(conf, dict) and "url" in conf else "stdio"
                self._tools_cache[server] = {"tools": tools, "ts": time.time(), "source": source}
                visible_tools = self._filter_disabled_tools(
                    str(server), tools if isinstance(tools, list) else []
                )
                self._set_status(
                    server,
                    "success",
                    tool_count=len(visible_tools),
                    source=source,
                    is_cached=False,
                    failure_type="",
                    suggestion="",
                )
                self._log("INFO", f"list_tools success server={server}, source={source}, tool_count={len(visible_tools)}")
                self._mark_op(server, -1)
                return visible_tools, False
            except Exception as e:
                last_error = str(e)
                self._log("WARNING", f"list_tools failed for {server}: {last_error}")
                # force reconnect once
                if server in self._clients:
                    try:
                        self._clients[server]._shutdown_unlocked()
                    except Exception:
                        pass
                    self._clients.pop(server, None)
        fallback_tools = self._try_cli_schemas_fallback(server, timeout_s=timeout_s)
        if fallback_tools is not None:
            self._tools_cache[server] = {"tools": fallback_tools, "ts": time.time(), "source": "cli_schemas"}
            visible_tools = self._filter_disabled_tools(str(server), fallback_tools)
            self._set_status(
                server,
                "success",
                tool_count=len(visible_tools),
                source="cli_schemas",
                is_cached=False,
                failure_type="",
                suggestion="",
            )
            self._log("INFO", f"list_tools success server={server}, source=cli_schemas, tool_count={len(visible_tools)}")
            self._mark_op(server, -1)
            return visible_tools, False
        ft, sugg = self._classify_failure(server, last_error or "tools/list failed")
        self._set_status(
            server,
            "failed",
            last_error=last_error or "tools/list failed",
            failure_type=ft,
            suggestion=sugg,
        )
        self._mark_op(server, -1)
        raise McpError(last_error or "tools/list failed")

    def call_tool(self, server: str, tool_name: str, arguments: Dict[str, Any], timeout_s: float = 20.0) -> Dict[str, Any]:
        if self.is_tool_disabled(str(server), str(tool_name)):
            raise McpError(f"MCP tool is disabled (server={server}, tool={tool_name})")
        # Refresh cache on first tool call if absent; failure is non-fatal.
        if server not in self._tools_cache:
            try:
                self.list_tools(server, timeout_s=min(8.0, timeout_s), use_cache=False)
            except Exception:
                pass
        # Local schema validation before remote call.
        try:
            cached_tools = self._tools_cache.get(server, {}).get("tools", [])
            if isinstance(cached_tools, list):
                for t in cached_tools:
                    if not isinstance(t, dict):
                        continue
                    name = str(t.get("name", "")).strip()
                    if name != str(tool_name):
                        continue
                    schema = _extract_tool_schema(t)
                    if isinstance(schema, dict):
                        errs = _validate_json_schema_like(schema, arguments if isinstance(arguments, dict) else {})
                        if errs:
                            raise McpError(
                                "tool arguments schema validation failed: " + "; ".join(errs[:8])
                            )
                    break
        except McpError:
            raise
        except Exception:
            # Non-fatal for unknown schema shape.
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

    def call_tools_batch(
        self,
        server: str,
        calls: List[Dict[str, Any]],
        timeout_s: float = 30.0,
        *,
        allow_partial_failure: bool = False,
    ) -> List[Dict[str, Any]]:
        if not isinstance(calls, list) or not calls:
            raise McpError("calls must be a non-empty array")
        # Ensure tools cache exists for local validation.
        if server not in self._tools_cache:
            try:
                self.list_tools(server, timeout_s=min(8.0, timeout_s), use_cache=False)
            except Exception:
                pass
        # Local schema validation per call.
        tools_map: Dict[str, Dict[str, Any]] = {}
        cached_tools = self._tools_cache.get(server, {}).get("tools", [])
        if isinstance(cached_tools, list):
            for t in cached_tools:
                if isinstance(t, dict):
                    tools_map[str(t.get("name", "")).strip()] = t
        normalized: List[Dict[str, Any]] = []
        for idx, c in enumerate(calls):
            if not isinstance(c, dict):
                raise McpError(f"calls[{idx}] must be an object")
            tool_name = str(c.get("tool", "")).strip()
            if not tool_name:
                raise McpError(f"calls[{idx}].tool cannot be empty")
            if self.is_tool_disabled(str(server), tool_name):
                raise McpError(f"calls[{idx}].tool is disabled (server={server}, tool={tool_name})")
            args = c.get("arguments", {})
            if not isinstance(args, dict):
                raise McpError(f"calls[{idx}].arguments must be an object")
            desc = tools_map.get(tool_name)
            if isinstance(desc, dict):
                schema = _extract_tool_schema(desc)
                if isinstance(schema, dict):
                    errs = _validate_json_schema_like(schema, args, path=f"calls[{idx}].arguments")
                    if errs:
                        raise McpError("tool arguments schema validation failed: " + "; ".join(errs[:8]))
            normalized.append({"tool": tool_name, "arguments": args})
        self._set_status(server, "loading")
        timeout_s = self._effective_timeout(server, timeout_s)
        try:
            client = self._client(server)
            if hasattr(client, "call_tools_batch"):
                res = client.call_tools_batch(
                    normalized, timeout_s=timeout_s, allow_partial_failure=allow_partial_failure
                )
            else:
                res = [client.call_tool(c["tool"], c["arguments"], timeout_s=timeout_s) for c in normalized]
            self._set_status(server, "success", failure_type="", suggestion="")
            return res if isinstance(res, list) else []
        except Exception as e:
            ft, sugg = self._classify_failure(server, str(e))
            self._set_status(server, "failed", last_error=str(e), failure_type=ft, suggestion=sugg)
            self._log("WARNING", f"call_tools_batch failed for {server}: {e}")
            raise

    def list_resources(self, server: str, timeout_s: float = 8.0, use_cache: bool = True) -> Tuple[List[Dict[str, Any]], bool]:
        if use_cache and server in self._resources_cache:
            cached = self._resources_cache[server]
            resources = cached.get("resources", [])
            return resources if isinstance(resources, list) else [], True
        if use_cache and server not in self._resources_cache:
            raise McpError("resources cache miss (use_cache=true); skipped live connection")
        self._mark_op(server, +1)
        timeout_s = self._effective_timeout(server, timeout_s)
        last_error: Optional[str] = None
        for _ in range(2):
            try:
                resources = self._client(server).list_resources(timeout_s=timeout_s)
                conf = self._server_conf(server)
                source = "url" if isinstance(conf, dict) and "url" in conf else "stdio"
                self._resources_cache[server] = {
                    "resources": resources if isinstance(resources, list) else [],
                    "ts": time.time(),
                    "source": source,
                }
                self._mark_op(server, -1)
                return resources if isinstance(resources, list) else [], False
            except Exception as e:
                last_error = str(e)
                self._log("WARNING", f"list_resources failed for {server}: {last_error}")
                if server in self._clients:
                    try:
                        self._clients[server]._shutdown_unlocked()
                    except Exception:
                        pass
                    self._clients.pop(server, None)
        self._mark_op(server, -1)
        raise McpError(last_error or "resources/list failed")

    def read_resource(self, server: str, uri: str, timeout_s: float = 20.0) -> Dict[str, Any]:
        uri_s = str(uri or "").strip()
        if not uri_s:
            raise McpError("Missing resource URI")
        timeout_s = self._effective_timeout(server, timeout_s)
        try:
            return self._client(server).read_resource(uri_s, timeout_s=timeout_s)
        except Exception as e:
            self._log("WARNING", f"read_resource failed for {server} uri={uri_s}: {e}")
            raise

    def list_resource_templates(
        self, server: str, timeout_s: float = 8.0, use_cache: bool = True
    ) -> Tuple[List[Dict[str, Any]], bool]:
        if use_cache and server in self._resource_templates_cache:
            cached = self._resource_templates_cache[server]
            templates = cached.get("templates", [])
            return templates if isinstance(templates, list) else [], True
        if use_cache and server not in self._resource_templates_cache:
            raise McpError("resource templates cache miss (use_cache=true); skipped live connection")
        self._mark_op(server, +1)
        timeout_s = self._effective_timeout(server, timeout_s)
        last_error: Optional[str] = None
        for _ in range(2):
            try:
                templates = self._client(server).list_resource_templates(timeout_s=timeout_s)
                conf = self._server_conf(server)
                source = "url" if isinstance(conf, dict) and "url" in conf else "stdio"
                self._resource_templates_cache[server] = {
                    "templates": templates if isinstance(templates, list) else [],
                    "ts": time.time(),
                    "source": source,
                }
                self._mark_op(server, -1)
                return templates if isinstance(templates, list) else [], False
            except Exception as e:
                last_error = str(e)
                self._log("WARNING", f"list_resource_templates failed for {server}: {last_error}")
                if server in self._clients:
                    try:
                        self._clients[server]._shutdown_unlocked()
                    except Exception:
                        pass
                    self._clients.pop(server, None)
        self._mark_op(server, -1)
        raise McpError(last_error or "resources/templates/list failed")

    def list_prompts(self, server: str, timeout_s: float = 8.0, use_cache: bool = True) -> Tuple[List[Dict[str, Any]], bool]:
        if use_cache and server in self._prompts_cache:
            cached = self._prompts_cache[server]
            prompts = cached.get("prompts", [])
            return prompts if isinstance(prompts, list) else [], True
        if use_cache and server not in self._prompts_cache:
            raise McpError("prompts cache miss (use_cache=true); skipped live connection")
        self._mark_op(server, +1)
        timeout_s = self._effective_timeout(server, timeout_s)
        last_error: Optional[str] = None
        for _ in range(2):
            try:
                prompts = self._client(server).list_prompts(timeout_s=timeout_s)
                conf = self._server_conf(server)
                source = "url" if isinstance(conf, dict) and "url" in conf else "stdio"
                self._prompts_cache[server] = {
                    "prompts": prompts if isinstance(prompts, list) else [],
                    "ts": time.time(),
                    "source": source,
                }
                self._mark_op(server, -1)
                return prompts if isinstance(prompts, list) else [], False
            except Exception as e:
                last_error = str(e)
                self._log("WARNING", f"list_prompts failed for {server}: {last_error}")
                if server in self._clients:
                    try:
                        self._clients[server]._shutdown_unlocked()
                    except Exception:
                        pass
                    self._clients.pop(server, None)
        self._mark_op(server, -1)
        raise McpError(last_error or "prompts/list failed")

    def get_prompt(self, server: str, prompt_name: str, arguments: Dict[str, Any], timeout_s: float = 20.0) -> Dict[str, Any]:
        name = str(prompt_name or "").strip()
        if not name:
            raise McpError("Missing prompt name")
        if not isinstance(arguments, dict):
            raise McpError("prompt arguments must be an object")
        timeout_s = self._effective_timeout(server, timeout_s)
        try:
            return self._client(server).get_prompt(name, arguments, timeout_s=timeout_s)
        except Exception as e:
            self._log("WARNING", f"get_prompt failed for {server} prompt={name}: {e}")
            raise

    def sampling_create_message(self, server: str, params: Dict[str, Any], timeout_s: float = 30.0) -> Dict[str, Any]:
        if not isinstance(params, dict):
            raise McpError("sampling params must be an object")
        timeout_s = self._effective_timeout(server, timeout_s)
        try:
            return self._client(server).sampling_create_message(params, timeout_s=timeout_s)
        except Exception as e:
            self._log("WARNING", f"sampling_create_message failed for {server}: {e}")
            raise

    def completion_complete(self, server: str, params: Dict[str, Any], timeout_s: float = 20.0) -> Dict[str, Any]:
        if not isinstance(params, dict):
            raise McpError("completion params must be an object")
        timeout_s = self._effective_timeout(server, timeout_s)
        try:
            return self._client(server).completion_complete(params, timeout_s=timeout_s)
        except Exception as e:
            self._log("WARNING", f"completion_complete failed for {server}: {e}")
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
                    suggestion="This server is configured with skip_preload=true; set it to false if you need preload.",
                )
                self._log("INFO", f"preload skip server={server} reason=skip_preload")
                return
            if not force and server in self._tools_cache and server in self._prompts_cache:
                self._set_status(server, "success", is_cached=True)
                self._log("INFO", f"preload hit_cache server={server}")
                return
            try:
                self.list_tools(str(server), timeout_s=timeout_s, use_cache=False)
                try:
                    self.list_prompts(str(server), timeout_s=timeout_s, use_cache=False)
                except Exception as e:
                    # prompts are optional per MCP server; tools preload should still be considered usable.
                    self._log("INFO", f"preload prompts skipped for {server}: {e}")
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
                cached_tools_raw = cached.get("tools", [])
                cached_tools = self._filter_disabled_tools(
                    str(name), cached_tools_raw if isinstance(cached_tools_raw, list) else []
                )
                cached_count = len(cached_tools)
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
            # Expose per-section cache counts for `/mcp status` rendering.
            resources_cached = self._resources_cache.get(name, {})
            templates_cached = self._resource_templates_cache.get(name, {})
            prompts_cached = self._prompts_cache.get(name, {})
            resources_list = resources_cached.get("resources", []) if isinstance(resources_cached, dict) else []
            templates_list = (
                templates_cached.get("resource_templates", [])
                if isinstance(templates_cached, dict)
                else []
            )
            prompts_list = prompts_cached.get("prompts", []) if isinstance(prompts_cached, dict) else []
            v["tools_count"] = int(v.get("tool_count", 0) or 0)
            v["resources_count"] = len(resources_list) if isinstance(resources_list, list) else 0
            v["resource_templates_count"] = len(templates_list) if isinstance(templates_list, list) else 0
            v["prompts_count"] = len(prompts_list) if isinstance(prompts_list, list) else 0
            if v.get("state") == "loading":
                since = float(v.get("loading_since_ts", 0.0) or 0.0)
                elapsed = (now - since) if since > 0 else 0.0
                if since > 0 and elapsed > hard_loading_timeout_s:
                    self._set_status(
                        name,
                        "failed",
                        last_error=f"loading_hard_timeout>{int(hard_loading_timeout_s)}s",
                        failure_type="connect_failed",
                        suggestion="Loading took too long and state was auto-reclaimed; run mcp_reconnect or mcp_status_refresh(force=true) and retry.",
                    )
                    self._force_reset_active_ops(name)
                elif since > 0 and active_ops <= 0 and elapsed > no_active_threshold_s:
                    self._set_status(
                        name,
                        "failed",
                        last_error="loading_state_stuck_without_active_op",
                        failure_type="connect_failed",
                        suggestion="Detected no active loading tasks while state is still loading; run mcp_status_refresh(force=true) or mcp_reconnect.",
                    )
                elif since > 0 and elapsed > stale_threshold_s:
                    self._set_status(
                        name,
                        "failed",
                        last_error=f"loading_timeout>{int(stale_threshold_s)}s",
                        failure_type="connect_failed",
                        suggestion="Loading timed out; run mcp_reconnect or mcp_status_refresh(force=true) and retry.",
                    )
        with self._status_lock:
            items = {k: dict(v) for k, v in self._status.items()}
            active_snapshot = {k: int(v) for k, v in self._active_ops.items()}
            for name, v in items.items():
                v["active_ops"] = int(active_snapshot.get(name, 0))
                # Expose per-section cache counts for `/mcp status` rendering.
                resources_cached = self._resources_cache.get(name, {})
                templates_cached = self._resource_templates_cache.get(name, {})
                prompts_cached = self._prompts_cache.get(name, {})
                resources_list = resources_cached.get("resources", []) if isinstance(resources_cached, dict) else []
                templates_list = (
                    templates_cached.get("resource_templates", [])
                    if isinstance(templates_cached, dict)
                    else []
                )
                prompts_list = prompts_cached.get("prompts", []) if isinstance(prompts_cached, dict) else []
                v["tools_count"] = int(v.get("tool_count", 0) or 0)
                v["resources_count"] = len(resources_list) if isinstance(resources_list, list) else 0
                v["resource_templates_count"] = len(templates_list) if isinstance(templates_list, list) else 0
                v["prompts_count"] = len(prompts_list) if isinstance(prompts_list, list) else 0
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
            # Force create a fresh client object to avoid reusing stale URL session state.
            self._clients.pop(server, None)
        self._tools_cache.pop(server, None)
        self._resources_cache.pop(server, None)
        self._resource_templates_cache.pop(server, None)
        self._prompts_cache.pop(server, None)
        conf = self._server_conf(server)
        tools, _ = self.list_tools(server, timeout_s=timeout_s, use_cache=False)
        try:
            prompts, _ = self.list_prompts(server, timeout_s=timeout_s, use_cache=False)
            self._prompts_cache[server] = {
                "prompts": prompts if isinstance(prompts, list) else [],
                "ts": time.time(),
                "source": "url" if "url" in conf else "stdio",
            }
        except Exception as e:
            self._log("WARNING", f"prompt warmup failed during reconnect for {server}: {e}")
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
                    suggestion="This server is configured with skip_preload=true; set it to false if you need refresh.",
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
            return "No cached MCP tools yet (run mcp_list_tools first)."
        lines: List[str] = []
        for server, info in self._tools_cache.items():
            tools_raw = info.get("tools", [])
            tools = self._filter_disabled_tools(
                str(server), tools_raw if isinstance(tools_raw, list) else []
            )
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
            disabled_count = 0
            with self._policy_lock:
                disabled_count = len(self._disabled_tools_by_server.get(str(server), set()))
            if disabled_count > 0:
                show += f" | disabled={disabled_count}"
            lines.append(f"- {server}: {show}")
        return "\n".join(lines)

    def cached_resources_for_prompt(self) -> str:
        if not self._resources_cache:
            return "No cached MCP resources yet (run mcp_list_resources first)."
        lines: List[str] = []
        for server, info in self._resources_cache.items():
            resources = info.get("resources", [])
            entries: List[str] = []
            if isinstance(resources, list):
                for r in resources[:20]:
                    if not isinstance(r, dict):
                        continue
                    name = str(r.get("name", "")).strip()
                    uri = str(r.get("uri", "")).strip()
                    desc = str(r.get("description", "")).strip()
                    label = name or uri or "<unnamed>"
                    if desc:
                        if len(desc) > 80:
                            desc = desc[:77] + "..."
                        entries.append(f"{label} - {desc}")
                    else:
                        entries.append(label)
            show = " | ".join(entries) if entries else "(none)"
            if isinstance(resources, list) and len(resources) > 20:
                show += f" | ... total={len(resources)}"
            lines.append(f"- {server}: {show}")
        return "\n".join(lines)

    def cached_prompts_for_prompt(self) -> str:
        if not self._prompts_cache:
            return "No cached MCP prompts yet (run mcp_list_prompts first)."
        lines: List[str] = []
        for server, info in self._prompts_cache.items():
            prompts = info.get("prompts", [])
            entries: List[str] = []
            if isinstance(prompts, list):
                for p in prompts[:20]:
                    if not isinstance(p, dict):
                        continue
                    name = str(p.get("name", "")).strip() or "<unnamed>"
                    desc = str(p.get("description", "")).strip()
                    if desc:
                        if len(desc) > 80:
                            desc = desc[:77] + "..."
                        entries.append(f"{name} - {desc}")
                    else:
                        entries.append(name)
            show = " | ".join(entries) if entries else "(none)"
            if isinstance(prompts, list) and len(prompts) > 20:
                show += f" | ... total={len(prompts)}"
            lines.append(f"- {server}: {show}")
        return "\n".join(lines)

    def cached_initialize_instructions_for_prompt(self) -> str:
        items: List[Tuple[str, str]] = []
        for server, client in self._clients.items():
            if not bool(getattr(client, "initialized", False)):
                continue
            instructions = str(getattr(client, "initialize_instructions", "") or "").strip()
            if not instructions:
                continue
            items.append((str(server), instructions))
        if not items:
            return "No cached MCP initialize instructions yet (connect an MCP server first)."
        lines: List[str] = []
        for server, instructions in sorted(items, key=lambda item: item[0].lower()):
            lines.append(f"- {server}:")
            for line in instructions.splitlines():
                lines.append(f"  {line}" if line else "  ")
        return "\n".join(lines)
