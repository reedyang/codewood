import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "echo",
        "description": "Echo back provided message",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
            },
            "required": ["message"],
        },
    }
    ,
    {
        "name": "ask_client",
        "description": "Ask MCP client to handle sampling/createMessage",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "maxTokens": {"type": "integer"},
            },
            "required": ["message"],
        },
    }
    ,
    {
        "name": "echo_stream",
        "description": "Emit streaming notifications then return final result",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
            },
            "required": ["message"],
        },
    }
    ,
    {
        "name": "ask_elicitation",
        "description": "Ask MCP client to handle elicitation/create",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "message": {"type": "string"},
            },
        },
    }
]

RESOURCES: List[Dict[str, Any]] = [
    {
        "uri": "fake://docs/readme",
        "name": "readme",
        "description": "Fake MCP README resource",
        "mimeType": "text/plain",
    },
    {
        "uri": "fake://docs/config",
        "name": "config",
        "description": "Fake MCP config resource",
        "mimeType": "application/json",
    },
]

RESOURCE_CONTENTS: Dict[str, List[Dict[str, Any]]] = {
    "fake://docs/readme": [
        {
            "uri": "fake://docs/readme",
            "mimeType": "text/plain",
            "text": "This is a fake MCP resource.",
        }
    ],
    "fake://docs/config": [
        {
            "uri": "fake://docs/config",
            "mimeType": "application/json",
            "text": '{"name":"fake-mcp","version":"1.0.0"}',
        }
    ],
}

RESOURCE_TEMPLATES: List[Dict[str, Any]] = [
    {
        "uriTemplate": "fake://docs/{name}",
        "name": "docs-template",
        "description": "Template for fake docs resources",
    }
]

PROMPTS: List[Dict[str, Any]] = [
    {
        "name": "summarize_text",
        "description": "Summarize input text in one sentence",
        "arguments": [
            {"name": "text", "required": True, "description": "Text to summarize"},
        ],
    },
    {
        "name": "hello_user",
        "description": "Return greeting for user",
        "arguments": [
            {"name": "name", "required": False, "description": "User name"},
        ],
    },
]

URL_CLIENT_RESPONSES: Dict[str, Dict[str, Any]] = {}
URL_CLIENT_RESPONSES_LOCK = threading.Lock()


def _jsonrpc_result(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _handle_method(method: str, params: Optional[Dict[str, Any]]) -> Tuple[bool, Dict[str, Any]]:
    p = params if isinstance(params, dict) else {}
    if method == "initialize":
        return True, {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "fake-mcp-server", "version": "1.0.0"},
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}, "elicitation": {}},
        }
    if method == "notifications/initialized":
        return True, {}
    if method == "tools/list":
        return True, {"tools": TOOLS}
    if method == "tools/call":
        name = str(p.get("name", "")).strip()
        arguments = p.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}
        if name == "echo":
            message = str(arguments.get("message", ""))
            return True, {"content": [{"type": "text", "text": f"echo:{message}"}]}
        if name == "ask_client":
            # stdio-only bidirectional branch is handled in run_stdio_server.
            return True, {"content": [{"type": "text", "text": "ask_client not available on this transport"}]}
        if name == "echo_stream":
            message = str(arguments.get("message", ""))
            return True, {"content": [{"type": "text", "text": f"echo_stream:{message}"}]}
        if name == "ask_elicitation":
            # stdio-only bidirectional branch is handled in run_stdio_server.
            return True, {"content": [{"type": "text", "text": "ask_elicitation not available on this transport"}]}
        return False, {"code": -32601, "message": f"Unknown tool: {name}"}
    if method == "resources/list":
        return True, {"resources": RESOURCES}
    if method == "resources/read":
        uri = str(p.get("uri", "")).strip()
        contents = RESOURCE_CONTENTS.get(uri)
        if contents is None:
            return False, {"code": -32004, "message": f"Resource not found: {uri}"}
        return True, {"contents": contents}
    if method == "resources/templates/list":
        return True, {"resourceTemplates": RESOURCE_TEMPLATES}
    if method == "prompts/list":
        return True, {"prompts": PROMPTS}
    if method == "prompts/get":
        name = str(p.get("name", "")).strip()
        arguments = p.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}
        if name == "summarize_text":
            text = str(arguments.get("text", "")).strip()
            if not text:
                text = "(empty)"
            preview = text[:40] + ("..." if len(text) > 40 else "")
            return True, {
                "description": "Fake summarize prompt",
                "messages": [
                    {"role": "system", "content": {"type": "text", "text": "You are a concise assistant."}},
                    {"role": "user", "content": {"type": "text", "text": f"Summarize: {preview}"}},
                ],
            }
        if name == "hello_user":
            user_name = str(arguments.get("name", "friend")).strip() or "friend"
            return True, {
                "description": "Fake hello prompt",
                "messages": [
                    {"role": "user", "content": {"type": "text", "text": f"Say hello to {user_name}"}},
                ],
            }
        return False, {"code": -32601, "message": f"Unknown prompt: {name}"}
    if method == "sampling/createMessage":
        messages = p.get("messages", [])
        if not isinstance(messages, list):
            messages = []
        max_tokens = p.get("maxTokens", 256)
        if not isinstance(max_tokens, int):
            max_tokens = 256
        last_text = ""
        if messages:
            last = messages[-1]
            if isinstance(last, dict):
                content = last.get("content")
                if isinstance(content, dict):
                    last_text = str(content.get("text", ""))
                elif isinstance(content, str):
                    last_text = content
        return True, {
            "model": "fake-sampling-model",
            "role": "assistant",
            "content": {
                "type": "text",
                "text": f"[sampled maxTokens={max_tokens}] {last_text}".strip(),
            },
            "stopReason": "endTurn",
        }
    if method == "completion/complete":
        ref = p.get("ref", {})
        argument = p.get("argument", {})
        if not isinstance(ref, dict):
            ref = {}
        if not isinstance(argument, dict):
            argument = {}
        name = str(ref.get("name", "default"))
        arg_name = str(argument.get("name", "value"))
        value = str(argument.get("value", ""))
        seed = value.strip().lower()
        if not seed:
            values = [f"{name}-{arg_name}-one", f"{name}-{arg_name}-two", "fallback"]
        else:
            values = [f"{seed}-1", f"{seed}-2", f"{seed}-3"]
        return True, {
            "completion": {
                "values": values,
                "total": len(values),
                "hasMore": False,
            }
        }
    if method == "elicitation/create":
        requested = p.get("requestedSchema", {})
        if not isinstance(requested, dict):
            requested = {}
        title = str(p.get("title", "Fake elicitation"))
        message = str(p.get("message", ""))
        props = requested.get("properties", {})
        values: Dict[str, Any] = {}
        if isinstance(props, dict):
            for key, meta in props.items():
                k = str(key).strip()
                if not k:
                    continue
                if isinstance(meta, dict):
                    t = str(meta.get("type", "")).strip().lower()
                    if t in ("number", "integer"):
                        values[k] = 0
                    elif t == "boolean":
                        values[k] = False
                    else:
                        values[k] = f"{k}-value"
                else:
                    values[k] = f"{k}-value"
        return True, {"action": "accept", "title": title, "message": message, "content": values}
    return False, {"code": -32601, "message": f"Method not found: {method}"}


def _handle_jsonrpc_payload(payload: Any) -> Any:
    if isinstance(payload, list):
        out: List[Dict[str, Any]] = []
        for item in payload:
            r = _handle_jsonrpc_payload(item)
            if isinstance(r, dict):
                out.append(r)
        return out
    if not isinstance(payload, dict):
        return _jsonrpc_error(None, -32600, "Invalid Request")
    req_id = payload.get("id")
    method = payload.get("method")
    if not isinstance(method, str) or not method:
        return _jsonrpc_error(req_id, -32600, "Invalid method")
    params = payload.get("params", {})
    ok, data = _handle_method(method, params)
    if req_id is None:
        return None
    if ok:
        return _jsonrpc_result(req_id, data)
    return _jsonrpc_error(req_id, int(data.get("code", -32000)), str(data.get("message", "Unknown error")))


def _write_framed_stdout(obj: Dict[str, Any]) -> None:
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(raw + b"\n")
    sys.stdout.buffer.flush()


def _read_framed_stdin() -> Optional[Dict[str, Any]]:
    first_line = sys.stdin.buffer.readline()
    if not first_line:
        return None

    stripped = first_line.strip()
    if stripped.startswith(b"{") or stripped.startswith(b"["):
        try:
            return json.loads(stripped.decode("utf-8", errors="replace"))
        except Exception:
            return {"jsonrpc": "2.0", "id": None, "method": "", "params": {}}

    header_lines = [first_line]
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        header_lines.append(line)
        if line in (b"\r\n", b"\n"):
            break
        if sum(len(x) for x in header_lines) > 64 * 1024:
            return None

    content_length = None
    for raw_line in header_lines:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if line.lower().startswith("content-length:"):
            try:
                content_length = int(line.split(":", 1)[1].strip())
            except Exception:
                content_length = None
            break
    if content_length is None or content_length < 0:
        return None

    body = b""
    while len(body) < content_length:
        chunk = sys.stdin.buffer.read(content_length - len(body))
        if not chunk:
            break
        body += chunk
    if len(body) != content_length:
        return None
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return {"jsonrpc": "2.0", "id": None, "method": "", "params": {}}


def run_stdio_server() -> int:
    srv_req_seq = 1000
    while True:
        payload = _read_framed_stdin()
        if payload is None:
            return 0
        # Simulate bidirectional MCP: server -> client request while handling tools/call.
        if isinstance(payload, dict):
            method = str(payload.get("method", "")).strip()
            req_id = payload.get("id")
            params = payload.get("params", {})
            if method == "tools/call" and isinstance(params, dict):
                tool_name = str(params.get("name", "")).strip()
                args = params.get("arguments", {})
                if tool_name == "ask_client" and isinstance(args, dict):
                    ask_text = str(args.get("message", ""))
                    ask_max = args.get("maxTokens", 64)
                    if not isinstance(ask_max, int):
                        ask_max = 64
                    srv_req_seq += 1
                    server_req_id = f"srv-{srv_req_seq}"
                    server_req = {
                        "jsonrpc": "2.0",
                        "id": server_req_id,
                        "method": "sampling/createMessage",
                        "params": {
                            "messages": [
                                {"role": "user", "content": {"type": "text", "text": ask_text}},
                            ],
                            "maxTokens": ask_max,
                        },
                    }
                    _write_framed_stdout(server_req)
                    client_resp = _read_framed_stdin()
                    sampled_text = ""
                    if isinstance(client_resp, dict):
                        result = client_resp.get("result", {})
                        if isinstance(result, dict):
                            content = result.get("content", {})
                            if isinstance(content, dict):
                                sampled_text = str(content.get("text", ""))
                    out = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [
                                {"type": "text", "text": f"ask_client:{sampled_text}"},
                            ]
                        },
                    }
                    _write_framed_stdout(out)
                    continue
                if tool_name == "echo_stream" and isinstance(args, dict):
                    msg_text = str(args.get("message", ""))
                    parts = [f"{msg_text}-A", f"{msg_text}-B", f"{msg_text}-C"]
                    for part in parts:
                        _write_framed_stdout(
                            {
                                "jsonrpc": "2.0",
                                "method": "notifications/tools/call/stream",
                                "params": {"chunk": part},
                            }
                        )
                    _write_framed_stdout(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"content": [{"type": "text", "text": f"echo_stream:{msg_text}"}]},
                        }
                    )
                    continue
                if tool_name == "ask_elicitation" and isinstance(args, dict):
                    ask_title = str(args.get("title", "User Profile Collection"))
                    ask_message = str(args.get("message", "Please provide profile values"))
                    elicitation_id = f"elic-{srv_req_seq + 1}"
                    srv_req_seq += 1
                    server_req_id = f"srv-{srv_req_seq}"
                    server_req = {
                        "jsonrpc": "2.0",
                        "id": server_req_id,
                        "method": "elicitation/create",
                        "params": {
                            "mode": "form",
                            "message": ask_message,
                            "elicitationId": elicitation_id,
                            "requestedSchema": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "age": {"type": "integer"},
                                },
                                "required": ["name"],
                            },
                            "title": ask_title,
                        },
                    }
                    _write_framed_stdout(server_req)
                    client_resp = _read_framed_stdin()
                    action = ""
                    content: Dict[str, Any] = {}
                    if isinstance(client_resp, dict):
                        result = client_resp.get("result", {})
                        if isinstance(result, dict):
                            action = str(result.get("action", ""))
                            c = result.get("content", {})
                            if isinstance(c, dict):
                                content = c
                    out = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"ask_elicitation:{action}:{json.dumps(content, ensure_ascii=False)}",
                                }
                            ]
                        },
                    }
                    _write_framed_stdout(out)
                    continue
        response = _handle_jsonrpc_payload(payload)
        if response is None:
            continue
        if isinstance(response, list):
            if response:
                _write_framed_stdout(response)
        else:
            _write_framed_stdout(response)


class _HttpMcpHandler(BaseHTTPRequestHandler):
    server_version = "FakeMCP/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            self._send_json(_jsonrpc_error(None, -32700, "Parse error"))
            return
        # Capture client response for previously emitted server request
        # (used by URL ask_elicitation bidirectional simulation).
        if (
            isinstance(payload, dict)
            and payload.get("id") is not None
            and "method" not in payload
            and ("result" in payload or "error" in payload)
        ):
            with URL_CLIENT_RESPONSES_LOCK:
                URL_CLIENT_RESPONSES[str(payload.get("id"))] = payload
            self._send_json(_jsonrpc_result(payload.get("id"), {}))
            return
        # Simulate URL SSE multi-event streaming tool result.
        if isinstance(payload, dict):
            method = str(payload.get("method", "")).strip()
            req_id = payload.get("id")
            params = payload.get("params", {})
            if method == "tools/call" and isinstance(params, dict):
                tool_name = str(params.get("name", "")).strip()
                arguments = params.get("arguments", {})
                if tool_name == "echo_stream" and isinstance(arguments, dict):
                    msg_text = str(arguments.get("message", ""))
                    parts = [f"{msg_text}-U1", f"{msg_text}-U2", f"{msg_text}-U3"]
                    events: List[Dict[str, Any]] = []
                    for part in parts:
                        events.append(
                            {
                                "jsonrpc": "2.0",
                                "method": "notifications/tools/call/stream",
                                "params": {"chunk": part},
                            }
                        )
                    events.append(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"content": [{"type": "text", "text": f"echo_stream:{msg_text}"}]},
                        }
                    )
                    self._send_sse_json(events)
                    return
                if tool_name == "ask_elicitation" and isinstance(arguments, dict):
                    ask_title = str(arguments.get("title", "User Profile Collection"))
                    ask_message = str(arguments.get("message", "Please provide profile values"))
                    server_req_id = f"srv-url-elic-{int(time.time() * 1000)}"
                    elicitation_req = {
                        "jsonrpc": "2.0",
                        "id": server_req_id,
                        "method": "elicitation/create",
                        "params": {
                            "mode": "form",
                            "message": ask_message,
                            "elicitationId": f"elic-url-{int(time.time() * 1000)}",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "age": {"type": "integer"},
                                },
                                "required": ["name"],
                            },
                            "title": ask_title,
                        },
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(f"data: {json.dumps(elicitation_req, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    deadline = time.time() + 8.0
                    client_reply: Optional[Dict[str, Any]] = None
                    while time.time() < deadline:
                        with URL_CLIENT_RESPONSES_LOCK:
                            client_reply = URL_CLIENT_RESPONSES.pop(str(server_req_id), None)
                        if isinstance(client_reply, dict):
                            break
                        time.sleep(0.05)
                    action = "cancel"
                    content: Dict[str, Any] = {}
                    if isinstance(client_reply, dict):
                        result = client_reply.get("result", {})
                        if isinstance(result, dict):
                            action = str(result.get("action", "accept") or "accept")
                            c = result.get("content", {})
                            if isinstance(c, dict):
                                content = c
                    final_obj = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"ask_elicitation:{action}:{json.dumps(content, ensure_ascii=False)}",
                                }
                            ]
                        },
                    }
                    self.wfile.write(f"data: {json.dumps(final_obj, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    return
        response = _handle_jsonrpc_payload(payload)
        if response is None:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send_json(response)

    def _send_json(self, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_json(self, events: List[Dict[str, Any]]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for event in events:
            payload = json.dumps(event, ensure_ascii=False)
            frame = f"data: {payload}\n\n".encode("utf-8")
            self.wfile.write(frame)
            self.wfile.flush()


def run_url_server(host: str, port: int) -> int:
    httpd = ThreadingHTTPServer((host, port), _HttpMcpHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        httpd.server_close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Fake MCP server for e2e tests")
    parser.add_argument("--transport", choices=["stdio", "url"], required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    if args.transport == "stdio":
        return run_stdio_server()
    return run_url_server(host=str(args.host), port=int(args.port))


if __name__ == "__main__":
    raise SystemExit(main())
