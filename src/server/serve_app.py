"""Headless HTTP + SSE server that drives the existing Agent.

Design goals:
- Reuse 100% of the existing command/AI logic by running the normal
  ``run_agent_loop`` in a worker thread and feeding it input lines the
  same way a terminal user would type them (prompts and ``/slash``
  commands alike).
- Stream all terminal output back to GUI clients over SSE after
  stripping ANSI escape codes.
- Route interactive confirmation/elicitation prompts to the GUI via a
  ``confirm`` SSE event answered through ``POST /confirm``.

Security: binds to loopback only, rejects non-loopback peers, and
requires a per-launch bearer token on every request. The token is
emitted once on the real stdout as a single JSON handshake line so the
launching host can read it; it is never written to logs.
"""

from __future__ import annotations

import io
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

# Matches CSI / SGR and most other ANSI escape sequences.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")

# Upper bounds to reject oversized/abusive payloads (input guarding).
_MAX_INPUT_CHARS = 200_000
_MAX_CONFIRM_ANSWER_CHARS = 64
_MAX_BODY_BYTES = 1_048_576  # 1 MiB
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences so the GUI can theme output itself."""
    if not text:
        return ""
    return _ANSI_RE.sub("", text)


def _open_in_file_manager(path: str) -> bool:
    """Open a validated directory in the OS file manager (no shell).

    ``path`` must already resolve to an existing directory; the caller is
    responsible for mapping a trusted id to this root.
    """
    try:
        target = os.path.realpath(str(path))
    except Exception:
        return False
    if not target or not os.path.isdir(target):
        return False
    try:
        if sys.platform.startswith("win"):
            os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target], close_fds=True)
        else:
            subprocess.Popen(["xdg-open", target], close_fds=True)
        return True
    except Exception:
        return False


def _read_workspace_chat_index(storage_dir: Any) -> List[Dict[str, Any]]:
    """Read chat summaries from a workspace's on-disk chat index.

    Returns ``[{id, name, updatedAt}]`` (possibly empty). The path is derived
    from trusted agent state, not from any client input.
    """
    from ..agent import CHAT_STATE_FILE

    out: List[Dict[str, Any]] = []
    try:
        index_path = os.path.join(str(storage_dir), "chats", CHAT_STATE_FILE)
        if not os.path.isfile(index_path):
            return out
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return out
    chats = data.get("chats") if isinstance(data, dict) else None
    if not isinstance(chats, list):
        return out
    for c in chats:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "")
        if not cid:
            continue
        out.append(
            {
                "id": cid,
                "name": str(c.get("name") or ""),
                "updatedAt": str(c.get("updated_at") or ""),
            }
        )
    return out


class _Broadcaster:
    """Fan-out of server events to all connected SSE subscribers."""

    def __init__(self) -> None:
        self._subscribers: List["queue.Queue[Dict[str, Any]]"] = []
        self._lock = threading.Lock()

    def subscribe(self) -> "queue.Queue[Dict[str, Any]]":
        q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[Dict[str, Any]]") -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def publish(self, event: str, data: Optional[Dict[str, Any]] = None) -> None:
        message = {"event": str(event), "data": data or {}}
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(message)
            except Exception:
                pass


class _OutputBridge(io.TextIOBase):
    """A text stream that forwards all writes to SSE subscribers.

    Installed as ``sys.stdout`` / ``sys.stderr`` for the serve process
    after the stdout handshake, so every ``print`` in the agent loop
    becomes an ``output`` event instead of hitting the console.
    """

    def __init__(self, broadcaster: _Broadcaster) -> None:
        self._broadcaster = broadcaster
        # Event name used for subsequent writes. The agent loop flips this to
        # "assistant" while streaming the final reply (via the GUI hooks) so
        # the GUI can render the answer separately from intermediate steps.
        self._tag = "output"

    def set_tag(self, tag: str) -> None:
        self._tag = str(tag or "output")

    def write(self, s: Any) -> int:  # type: ignore[override]
        if s is None:
            return 0
        text = s if isinstance(s, str) else str(s)
        if not text:
            return 0
        cleaned = strip_ansi(text)
        if cleaned:
            self._broadcaster.publish(self._tag, {"text": cleaned})
        return len(text)

    def writable(self) -> bool:  # type: ignore[override]
        return True

    def flush(self) -> None:  # type: ignore[override]
        return None

    def isatty(self) -> bool:  # type: ignore[override]
        return False


def _build_state(agent: Any) -> Dict[str, Any]:
    """Serialize a read-only snapshot of agent state for the GUI."""
    from ..config.app_info import get_app_name, get_app_version
    from ..core.localization import get_display_language

    default_ws_id = ""
    try:
        default_ws_id = str(
            getattr(getattr(agent, "_workspace_state_manager", None), "_default_workspace_id", "")
            or ""
        )
    except Exception:
        default_ws_id = ""

    workspaces: List[Dict[str, Any]] = []
    try:
        raw = agent._workspaces_state.get("workspaces", {})
        active_ws_id = str(getattr(agent, "workspace_id", "") or "")
        if isinstance(raw, dict):
            for entry in raw.values():
                if not isinstance(entry, dict):
                    continue
                try:
                    root = str(agent._workspace_root_path(entry))
                except Exception:
                    root = str(entry.get("root") or "")
                ws_id = str(entry.get("id") or "")
                is_default = (
                    str(entry.get("kind") or "").lower() == "default"
                    or (bool(default_ws_id) and ws_id == default_ws_id)
                )
                workspaces.append(
                    {
                        "id": ws_id,
                        "name": str(entry.get("name") or ""),
                        "root": root,
                        "active": ws_id == active_ws_id,
                        "isDefault": is_default,
                    }
                )
    except Exception:
        pass

    chats: List[Dict[str, Any]] = []
    try:
        active_chat_id = str(getattr(agent, "active_chat_id", "") or "")
        for i, c in enumerate(agent._chat_entries(), start=1):
            if not isinstance(c, dict):
                continue
            chats.append(
                {
                    "index": i,
                    "id": str(c.get("id") or ""),
                    "name": str(c.get("name") or ""),
                    "messageCount": len(c.get("messages") or []),
                    "updatedAt": str(c.get("updated_at") or ""),
                    "active": str(c.get("id") or "") == active_chat_id,
                }
            )
    except Exception:
        pass

    model_current = ""
    model_available: List[str] = []
    try:
        model_current = str(agent._current_model_selector() or "")
    except Exception:
        pass
    try:
        model_available = [str(s) for s in (agent._get_configured_model_selectors() or []) if str(s)]
    except Exception:
        pass

    try:
        language = get_display_language(agent)
    except Exception:
        language = "en"

    return {
        "app": {"name": get_app_name(), "version": get_app_version()},
        "workspace": {
            "name": str(getattr(agent, "workspace_name", "") or ""),
            "id": str(getattr(agent, "workspace_id", "") or ""),
            "root": str(getattr(agent, "workspace_root", "") or ""),
            "workDirectory": str(getattr(agent, "work_directory", "") or ""),
        },
        "workspaces": workspaces,
        "chats": chats,
        "activeChatId": str(getattr(agent, "active_chat_id", "") or ""),
        "model": {"current": model_current, "available": model_available},
        "language": language,
        "executionPolicy": str(getattr(agent, "execution_policy", "") or ""),
    }


class ServeApp:
    """Owns the agent loop, the event broadcaster, and the HTTP server."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.broadcaster = _Broadcaster()
        self._input_queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._confirms: Dict[str, "queue.Queue[str]"] = {}
        self._confirms_lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._token = secrets.token_urlsafe(32)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._busy = threading.Event()

    # ----- agent loop hooks ------------------------------------------------
    def _input_provider(self) -> str:
        """Replacement for ``agent._get_user_input_with_history``."""
        self._busy.clear()
        self.broadcaster.publish("idle", {"state": _build_state(self.agent)})
        text = self._input_queue.get()
        if text is None:
            # Shutdown sentinel: ask the loop to exit cleanly.
            return "/exit"
        self._busy.set()
        self.broadcaster.publish("turn_start", {"text": text})
        return text

    def _confirm_provider(self, prompt: str = "") -> str:
        """Replacement for ``agent._suspended_input`` (y/n + elicitation)."""
        cid = secrets.token_hex(8)
        reply: "queue.Queue[str]" = queue.Queue()
        with self._confirms_lock:
            self._confirms[cid] = reply
        self.broadcaster.publish(
            "confirm", {"id": cid, "prompt": strip_ansi(str(prompt or ""))}
        )
        try:
            answer = reply.get()
        finally:
            with self._confirms_lock:
                self._confirms.pop(cid, None)
        return str(answer or "")

    # ----- API surface used by the HTTP handler ---------------------------
    @property
    def token(self) -> str:
        return self._token

    def submit_input(self, text: str) -> None:
        self._input_queue.put(str(text or ""))

    def answer_confirm(self, cid: str, answer: str) -> bool:
        with self._confirms_lock:
            reply = self._confirms.get(str(cid or ""))
        if reply is None:
            return False
        reply.put(str(answer or ""))
        return True

    def interrupt(self) -> None:
        agent = self.agent
        for name in ("_mark_process_interrupt_requested", "_terminate_interruptible_processes"):
            try:
                fn = getattr(agent, name, None)
                if callable(fn):
                    fn()
            except Exception:
                pass

    def state(self) -> Dict[str, Any]:
        return _build_state(self.agent)

    def _resolve_workspace_root(self, ws_id: str) -> Optional[str]:
        """Map a workspace id to its on-disk root using agent state only.

        The client never supplies a path; only an id that must match a known
        workspace, preventing arbitrary-path disclosure/open.
        """
        ws_id = str(ws_id or "").strip()
        if not ws_id:
            return None
        agent = self.agent
        try:
            raw = agent._workspaces_state.get("workspaces", {})
        except Exception:
            raw = None
        if isinstance(raw, dict):
            for entry in raw.values():
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("id") or "") != ws_id:
                    continue
                try:
                    root = str(agent._workspace_root_path(entry))
                except Exception:
                    root = str(entry.get("root") or "")
                return root or None
        if str(getattr(agent, "workspace_id", "") or "") == ws_id:
            return str(getattr(agent, "workspace_root", "") or "") or None
        return None

    def open_workspace(self, ws_id: str) -> bool:
        root = self._resolve_workspace_root(ws_id)
        if not root:
            return False
        return _open_in_file_manager(root)

    def list_workspace_chats(self, ws_id: str) -> Optional[List[Dict[str, Any]]]:
        """List chats for a workspace by id without switching to it.

        The active workspace uses the fresher in-memory entries; other
        workspaces are read from their on-disk chat index. The id must match a
        known workspace; raw paths are never accepted.
        """
        ws_id = str(ws_id or "").strip()
        if not ws_id:
            return None
        agent = self.agent
        if str(getattr(agent, "workspace_id", "") or "") == ws_id:
            out: List[Dict[str, Any]] = []
            try:
                for c in agent._chat_entries():
                    if not isinstance(c, dict):
                        continue
                    out.append(
                        {
                            "id": str(c.get("id") or ""),
                            "name": str(c.get("name") or ""),
                            "updatedAt": str(c.get("updated_at") or ""),
                        }
                    )
            except Exception:
                return []
            return out

        try:
            raw = agent._workspaces_state.get("workspaces", {})
        except Exception:
            raw = None
        if not isinstance(raw, dict):
            return None
        for entry in raw.values():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("id") or "") != ws_id:
                continue
            try:
                storage = agent._workspace_storage_path(entry)
            except Exception:
                return []
            return _read_workspace_chat_index(storage)
        return None

    def request_shutdown(self) -> None:
        self._shutdown_event.set()
        self._input_queue.put(None)
        # Unblock any pending confirmation so the loop can drain.
        with self._confirms_lock:
            pending = list(self._confirms.values())
        for reply in pending:
            try:
                reply.put_nowait("n")
            except Exception:
                pass
        if self._httpd is not None:
            threading.Thread(target=self._httpd.shutdown, daemon=True).start()

    # ----- lifecycle -------------------------------------------------------
    def run(self, host: str = "127.0.0.1", port: int = 0) -> int:
        real_stdout = sys.stdout
        handler_cls = _make_handler(self)
        self._httpd = ThreadingHTTPServer((host, int(port or 0)), handler_cls)
        actual_port = int(self._httpd.server_address[1])

        # Handshake: one JSON line on the real stdout for the host to read.
        try:
            real_stdout.write(
                json.dumps({"port": actual_port, "token": self._token}) + "\n"
            )
            real_stdout.flush()
        except Exception:
            pass

        # Install agent loop hooks before swapping stdout so output is captured.
        self.agent._get_user_input_with_history = self._input_provider  # type: ignore[assignment]
        self.agent._suspended_input = self._confirm_provider  # type: ignore[assignment]

        bridge = _OutputBridge(self.broadcaster)
        # GUI streaming mode: the runtime emits clean append-only deltas and
        # brackets the assistant reply with these hooks so the bridge can tag
        # those writes as "assistant" (vs "output" steps).
        self.agent._gui_plain_stream = True  # type: ignore[attr-defined]
        self.agent._gui_assistant_begin = lambda: bridge.set_tag("assistant")  # type: ignore[attr-defined]
        self.agent._gui_assistant_end = lambda: bridge.set_tag("output")  # type: ignore[attr-defined]
        prev_stdout, prev_stderr = sys.stdout, sys.stderr
        sys.stdout = bridge
        sys.stderr = bridge

        loop_thread = threading.Thread(
            target=self._run_agent_loop, name="codewood-agent-loop", daemon=True
        )
        loop_thread.start()

        try:
            self._httpd.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.request_shutdown()
            sys.stdout = prev_stdout
            sys.stderr = prev_stderr
            try:
                self._httpd.server_close()
            except Exception:
                pass
            try:
                self.agent.shutdown(wait=False)
            except Exception:
                pass
        return 0

    def _run_agent_loop(self) -> None:
        try:
            from ..runtime.runtime_loop import run_agent_loop

            run_agent_loop(self.agent)
        except Exception:
            # Best-effort: surface fatal loop errors to subscribers.
            try:
                self.broadcaster.publish("output", {"text": "\n[agent loop terminated]\n"})
            except Exception:
                pass
        finally:
            self.request_shutdown()


def _make_handler(app: ServeApp):
    class _Handler(BaseHTTPRequestHandler):
        server_version = "CodeWoodServe/1.0"
        protocol_version = "HTTP/1.1"

        # Silence default stderr request logging (would hit the SSE bridge).
        def log_message(self, *_args: Any) -> None:  # noqa: N802
            return None

        # ----- helpers -----
        def _is_loopback(self) -> bool:
            try:
                return str(self.client_address[0]) in _LOOPBACK_HOSTS
            except Exception:
                return False

        def _authorized(self, query: Dict[str, List[str]]) -> bool:
            header = self.headers.get("Authorization", "") or ""
            if header.startswith("Bearer "):
                if secrets.compare_digest(header[len("Bearer ") :].strip(), app.token):
                    return True
            token_param = (query.get("token") or [""])[0]
            if token_param and secrets.compare_digest(token_param, app.token):
                return True
            return False

        def _read_json_body(self) -> Optional[Dict[str, Any]]:
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                return None
            if length <= 0:
                return {}
            if length > _MAX_BODY_BYTES:
                return None
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                return None
            return data if isinstance(data, dict) else None

        def _send_cors(self) -> None:
            # Loopback-only, token-gated, no cookies: a permissive ACAO is
            # safe here and lets the WebView (file://, internal http server,
            # or the Vite dev origin) reach the API.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")

        def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._send_cors()
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self._send_cors()
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def _guard(self, query: Dict[str, List[str]]) -> bool:
            if not self._is_loopback():
                self._send_json(403, {"error": "forbidden"})
                return False
            if not self._authorized(query):
                self._send_json(401, {"error": "unauthorized"})
                return False
            return True

        # ----- routing -----
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path == "/health":
                self._send_json(200, {"ok": True})
                return
            if not self._guard(query):
                return
            if path == "/state":
                self._send_json(200, app.state())
                return
            if path == "/workspace-chats":
                ws_id = ""
                values = query.get("id") or []
                if values:
                    ws_id = str(values[0])[:256]
                result = app.list_workspace_chats(ws_id)
                if result is None:
                    self._send_json(404, {"error": "not found"})
                else:
                    self._send_json(200, {"chats": result})
                return
            if path == "/events":
                self._stream_events()
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if not self._guard(query):
                return
            body = self._read_json_body()
            if body is None:
                self._send_json(400, {"error": "invalid body"})
                return
            if path == "/input":
                text = str(body.get("text") or "")
                if len(text) > _MAX_INPUT_CHARS:
                    self._send_json(413, {"error": "input too large"})
                    return
                app.submit_input(text)
                self._send_json(200, {"ok": True})
                return
            if path == "/confirm":
                cid = str(body.get("id") or "")
                answer = str(body.get("answer") or "")[:_MAX_CONFIRM_ANSWER_CHARS]
                ok = app.answer_confirm(cid, answer)
                self._send_json(200 if ok else 404, {"ok": ok})
                return
            if path == "/interrupt":
                app.interrupt()
                self._send_json(200, {"ok": True})
                return
            if path == "/open-workspace":
                ws_id = str(body.get("id") or "")[:256]
                ok = app.open_workspace(ws_id)
                self._send_json(200 if ok else 404, {"ok": ok})
                return
            if path == "/shutdown":
                self._send_json(200, {"ok": True})
                app.request_shutdown()
                return
            self._send_json(404, {"error": "not found"})

        def _stream_events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self._send_cors()
            self.end_headers()
            sub = app.broadcaster.subscribe()
            try:
                # Prime the client with the current state immediately.
                self._write_sse({"event": "idle", "data": {"state": app.state()}})
                while not app._shutdown_event.is_set():
                    try:
                        message = sub.get(timeout=15.0)
                    except queue.Empty:
                        if not self._write_raw(b": ping\n\n"):
                            break
                        continue
                    if not self._write_sse(message):
                        break
            finally:
                app.broadcaster.unsubscribe(sub)

        def _write_sse(self, message: Dict[str, Any]) -> bool:
            try:
                payload = json.dumps(message, ensure_ascii=False)
            except Exception:
                return True
            chunk = f"data: {payload}\n\n".encode("utf-8")
            return self._write_raw(chunk)

        def _write_raw(self, chunk: bytes) -> bool:
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
                return True
            except Exception:
                return False

    return _Handler
