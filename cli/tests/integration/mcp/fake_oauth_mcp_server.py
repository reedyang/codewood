import argparse
import base64
import hashlib
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict


STATE_LOCK = threading.Lock()
AUTH_CODES: Dict[str, Dict[str, Any]] = {}
REFRESH_TOKENS: Dict[str, Dict[str, Any]] = {}
VALID_ACCESS_TOKENS: Dict[str, Dict[str, Any]] = {}
REG_CLIENTS: Dict[str, Dict[str, Any]] = {}
TOKEN_SEQ = 0
CODE_SEQ = 0
CLIENT_SEQ = 0
NO_CHALLENGE_MODE = False
MANUAL_TOKEN = "manual-token"


def _jsonrpc_result(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _next_token(prefix: str) -> str:
    global TOKEN_SEQ
    with STATE_LOCK:
        TOKEN_SEQ += 1
        return f"{prefix}-{TOKEN_SEQ}"


def _next_code() -> str:
    global CODE_SEQ
    with STATE_LOCK:
        CODE_SEQ += 1
        return f"code-{CODE_SEQ}"


def _next_client_id(base_url: str) -> str:
    global CLIENT_SEQ
    with STATE_LOCK:
        CLIENT_SEQ += 1
        return f"{base_url}/auth/clients/dyn-{CLIENT_SEQ}"


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _extract_token(headers: Any) -> str:
    auth = str(headers.get("Authorization", "") or "").strip()
    if not auth.lower().startswith("bearer "):
        return ""
    token = auth[7:].strip()
    if not token:
        return ""
    return token


def _is_authorized(headers: Any) -> bool:
    token = _extract_token(headers)
    if not token:
        return False
    if NO_CHALLENGE_MODE and token == str(MANUAL_TOKEN):
        return True
    with STATE_LOCK:
        return token in VALID_ACCESS_TOKENS


def _token_has_scope(token: str, required_scope: str) -> bool:
    req = str(required_scope or "").strip()
    if not req:
        return True
    with STATE_LOCK:
        meta = VALID_ACCESS_TOKENS.get(str(token), {})
    if not isinstance(meta, dict):
        return False
    scope_str = str(meta.get("scope", "") or "").strip()
    scopes = {s for s in scope_str.split(" ") if s}
    return req in scopes


def _handle_mcp(payload: Dict[str, Any]) -> Dict[str, Any]:
    req_id = payload.get("id")
    method = str(payload.get("method", "") or "").strip()
    params = payload.get("params", {})
    if not isinstance(params, dict):
        params = {}
    if method == "initialize":
        return _jsonrpc_result(
            req_id,
            {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "fake-oauth-mcp", "version": "1.0.0"},
                "capabilities": {"tools": {}},
                "instructions": (
                    "Fake OAuth MCP instructions\n"
                    "- Authenticate before calling protected tools"
                ),
            },
        )
    if method == "notifications/initialized":
        return _jsonrpc_result(req_id, {})
    if method == "tools/list":
        return _jsonrpc_result(
            req_id,
            {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo message",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"message": {"type": "string"}},
                            "required": ["message"],
                        },
                    }
                ]
            },
        )
    if method == "tools/call":
        name = str(params.get("name", "") or "").strip()
        args = params.get("arguments", {})
        if not isinstance(args, dict):
            args = {}
        if name == "echo":
            msg = str(args.get("message", "") or "")
            return _jsonrpc_result(req_id, {"content": [{"type": "text", "text": f"echo:{msg}"}]})
        return _jsonrpc_error(req_id, -32601, f"Unknown tool: {name}")
    return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeOAuthMCP/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _base_url(self) -> str:
        host = str(self.headers.get("Host", "") or "").strip() or "127.0.0.1:18888"
        return f"http://{host}"

    def _send_json(self, obj: Any, status: int = 200, headers: Dict[str, str] = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for k, v in headers.items():
                self.send_header(str(k), str(v))
        self.end_headers()
        self.wfile.write(body)

    def _send_unauthorized(self) -> None:
        if NO_CHALLENGE_MODE:
            self._send_json({"error": "unauthorized"}, status=401)
            return
        base = self._base_url()
        headers = {
            "WWW-Authenticate": (
                f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource/mcp", '
                'scope="files:read"'
            )
        }
        self._send_json({"error": "unauthorized"}, status=401, headers=headers)

    def do_GET(self) -> None:  # noqa: N802
        base = self._base_url()
        pu = urllib.parse.urlsplit(self.path)
        path = pu.path
        qs = urllib.parse.parse_qs(pu.query or "")

        if path == "/.well-known/oauth-protected-resource/mcp":
            self._send_json(
                {
                    "resource": f"{base}/mcp",
                    "authorization_servers": [f"{base}/auth"],
                    "scopes_supported": ["files:read"],
                }
            )
            return

        if path == "/.well-known/oauth-authorization-server/auth":
            self._send_json(
                {
                    "issuer": f"{base}/auth",
                    "authorization_endpoint": f"{base}/auth/authorize",
                    "token_endpoint": f"{base}/auth/token",
                    "registration_endpoint": f"{base}/auth/register",
                    "code_challenge_methods_supported": ["S256"],
                }
            )
            return

        if path == "/auth/authorize":
            client_id = str((qs.get("client_id") or [""])[0])
            redirect_uri = str((qs.get("redirect_uri") or [""])[0])
            state = str((qs.get("state") or [""])[0])
            challenge = str((qs.get("code_challenge") or [""])[0])
            method = str((qs.get("code_challenge_method") or [""])[0]).upper()
            resource = str((qs.get("resource") or [""])[0])
            if not client_id or not redirect_uri or not challenge or method != "S256":
                self._send_json({"error": "invalid_request"}, status=400)
                return
            code = _next_code()
            with STATE_LOCK:
                AUTH_CODES[code] = {
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "code_challenge": challenge,
                    "resource": resource,
                    "scope": str((qs.get("scope") or [""])[0] or ""),
                    "created_at": time.time(),
                }
            target = redirect_uri + ("&" if "?" in redirect_uri else "?") + urllib.parse.urlencode(
                {"code": code, "state": state}
            )
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self._send_json({"error": "not_found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        base = self._base_url()
        pu = urllib.parse.urlsplit(self.path)
        path = pu.path
        raw_len = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(raw_len) if raw_len > 0 else b""

        if path == "/auth/register":
            client_id = _next_client_id(base)
            self._send_json(
                {
                    "client_id": client_id,
                    "token_endpoint_auth_method": "none",
                }
            )
            return

        if path == "/auth/token":
            form = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"))
            grant_type = str((form.get("grant_type") or [""])[0])
            client_id = str((form.get("client_id") or [""])[0])
            if grant_type == "authorization_code":
                code = str((form.get("code") or [""])[0])
                redirect_uri = str((form.get("redirect_uri") or [""])[0])
                verifier = str((form.get("code_verifier") or [""])[0])
                with STATE_LOCK:
                    meta = AUTH_CODES.pop(code, None)
                if not isinstance(meta, dict):
                    self._send_json({"error": "invalid_grant"}, status=400)
                    return
                if client_id != str(meta.get("client_id", "")) or redirect_uri != str(meta.get("redirect_uri", "")):
                    self._send_json({"error": "invalid_grant"}, status=400)
                    return
                if _pkce_s256(verifier) != str(meta.get("code_challenge", "")):
                    self._send_json({"error": "invalid_grant"}, status=400)
                    return
                access_token = _next_token("at")
                refresh_token = _next_token("rt")
                with STATE_LOCK:
                    VALID_ACCESS_TOKENS.clear()
                    VALID_ACCESS_TOKENS[access_token] = {
                        "client_id": client_id,
                        "issued_at": time.time(),
                        "scope": str(meta.get("scope", "") or "files:read"),
                    }
                    REFRESH_TOKENS[refresh_token] = {
                        "client_id": client_id,
                        "scope": str(meta.get("scope", "") or "files:read"),
                    }
                self._send_json(
                    {
                        "access_token": access_token,
                        "token_type": "Bearer",
                        "expires_in": 2,
                        "refresh_token": refresh_token,
                        "scope": str(meta.get("scope", "") or "files:read"),
                    }
                )
                return
            if grant_type == "refresh_token":
                rt = str((form.get("refresh_token") or [""])[0])
                with STATE_LOCK:
                    rt_meta = REFRESH_TOKENS.get(rt)
                if not isinstance(rt_meta, dict):
                    self._send_json({"error": "invalid_grant"}, status=400)
                    return
                if client_id and client_id != str(rt_meta.get("client_id", "")):
                    self._send_json({"error": "invalid_grant"}, status=400)
                    return
                access_token = _next_token("at")
                with STATE_LOCK:
                    VALID_ACCESS_TOKENS.clear()
                    VALID_ACCESS_TOKENS[access_token] = {
                        "client_id": str(rt_meta.get("client_id", "")),
                        "issued_at": time.time(),
                        "scope": str(rt_meta.get("scope", "") or "files:read"),
                    }
                self._send_json(
                    {
                        "access_token": access_token,
                        "token_type": "Bearer",
                        "expires_in": 120,
                        "refresh_token": rt,
                        "scope": str(rt_meta.get("scope", "") or "files:read"),
                    }
                )
                return
            self._send_json({"error": "unsupported_grant_type"}, status=400)
            return

        if path == "/mcp":
            if not _is_authorized(self.headers):
                self._send_unauthorized()
                return
            token = _extract_token(self.headers)
            try:
                payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            except Exception:
                self._send_json(_jsonrpc_error(None, -32700, "Parse error"))
                return
            if not isinstance(payload, dict):
                self._send_json(_jsonrpc_error(None, -32600, "Invalid request"))
                return
            method = str(payload.get("method", "") or "").strip()
            params = payload.get("params", {})
            if method == "tools/call" and isinstance(params, dict):
                tool_name = str(params.get("name", "") or "").strip()
                args = params.get("arguments", {})
                if not isinstance(args, dict):
                    args = {}
                if tool_name == "echo" and str(args.get("message", "") or "") == "need-write":
                    if not _token_has_scope(token, "files:write"):
                        headers = {
                            "WWW-Authenticate": (
                                f'Bearer error="insufficient_scope", scope="files:write", '
                                f'resource_metadata="{base}/.well-known/oauth-protected-resource/mcp"'
                            )
                        }
                        self._send_json({"error": "insufficient_scope"}, status=403, headers=headers)
                        return
            self._send_json(_handle_mcp(payload))
            return

        self._send_json({"error": "not_found"}, status=404)


def main() -> int:
    global NO_CHALLENGE_MODE, MANUAL_TOKEN
    parser = argparse.ArgumentParser(description="Fake OAuth-protected MCP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18888)
    parser.add_argument("--no-challenge", action="store_true", help="Return 401 without WWW-Authenticate")
    parser.add_argument("--manual-token", default="manual-token", help="Accepted static bearer token in no-challenge mode")
    args = parser.parse_args()
    NO_CHALLENGE_MODE = bool(args.no_challenge)
    MANUAL_TOKEN = str(args.manual_token or "manual-token")
    httpd = ThreadingHTTPServer((str(args.host), int(args.port)), _Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

