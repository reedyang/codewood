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
import queue
import re
import secrets
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

    def write(self, s: Any) -> int:  # type: ignore[override]
        if s is None:
            return 0
        text = s if isinstance(s, str) else str(s)
        if not text:
            return 0
        cleaned = strip_ansi(text)
        if cleaned:
            self._broadcaster.publish("output", {"text": cleaned})
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
                workspaces.append(
                    {
                        "id": str(entry.get("id") or ""),
                        "name": str(entry.get("name") or ""),
                        "root": root,
                        "active": str(entry.get("id") or "") == active_ws_id,
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
