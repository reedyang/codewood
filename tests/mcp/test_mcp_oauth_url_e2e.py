import importlib.util
import builtins
import getpass
import logging
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


def _load_mcp_manager_module():
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "agent" / "mcp_manager.py"
    spec = importlib.util.spec_from_file_location("smart_shell_mcp_manager_oauth_e2e", str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load mcp_manager module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_http_ready(url: str, timeout_s: float = 5.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.8) as resp:
                if int(getattr(resp, "status", 0) or 0) in (200, 401):
                    return
        except urllib.error.HTTPError as e:
            if int(getattr(e, "code", 0) or 0) == 401:
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("Fake OAuth MCP server not ready")


class McpOauthUrlE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mcp_module = _load_mcp_manager_module()
        cls.McpManager = cls.mcp_module.McpManager
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.server_script = cls.repo_root / "tests" / "mcp" / "fake_oauth_mcp_server.py"
        cls._orig_webbrowser_open = cls.mcp_module.webbrowser.open

        def _fake_web_open(url: str, *_args, **_kwargs) -> bool:
            def _worker() -> None:
                try:
                    urllib.request.urlopen(url, timeout=5.0).read()
                except Exception:
                    pass

            threading.Thread(target=_worker, daemon=True).start()
            return True

        cls.mcp_module.webbrowser.open = _fake_web_open

    @classmethod
    def tearDownClass(cls):
        cls.mcp_module.webbrowser.open = cls._orig_webbrowser_open

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.workspace_dir = self.config_dir / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            try:
                p.terminate()
            except Exception:
                pass
            try:
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        logger = logging.getLogger("smart_shell.mcp")
        for handler in list(logger.handlers):
            try:
                handler.close()
            except Exception:
                pass
            try:
                logger.removeHandler(handler)
            except Exception:
                pass
        self.temp_dir.cleanup()

    def _start_server_and_manager(
        self,
        *,
        with_client_id: bool,
        no_challenge: bool = False,
        manual_token: str = "manual-token",
        include_oauth: bool = True,
    ):
        port = _get_free_port()
        cmd = [sys.executable, str(self.server_script), "--host", "127.0.0.1", "--port", str(port)]
        if no_challenge:
            cmd.extend(["--no-challenge", "--manual-token", str(manual_token)])
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(proc)
        base = f"http://127.0.0.1:{port}"
        _wait_http_ready(f"{base}/.well-known/oauth-protected-resource/mcp")
        oauth_cfg = {
            "redirect_host": "127.0.0.1",
            "redirect_port": 0,
            "open_browser": True,
        }
        if with_client_id:
            oauth_cfg["client_id"] = f"{base}/auth/clients/static-client"
        server_conf = {
            "url": f"{base}/mcp",
            "headers": {},
            "skip_preload": True,
            "manual_auth_fallback": bool(no_challenge),
            "manual_auth_allow_non_tty": bool(no_challenge),
        }
        if include_oauth:
            server_conf["oauth"] = oauth_cfg
        mcp_cfg = {
            "mcpServers": {
                "oauth_url": server_conf
            }
        }
        manager = self.McpManager(config_dir=self.config_dir, mcp_config=mcp_cfg, workspace_dir=self.workspace_dir)
        return manager

    def test_oauth_flow_with_static_client_id_and_refresh(self):
        manager = self._start_server_and_manager(with_client_id=True)
        tools, _ = manager.list_tools("oauth_url", use_cache=False, timeout_s=12.0)
        self.assertIn("echo", [str(t.get("name", "")) for t in tools if isinstance(t, dict)])

        client = manager._clients.get("oauth_url")
        self.assertTrue(client is not None)
        token_before = str(client.oauth_token.get("access_token", ""))
        self.assertTrue(token_before.startswith("at-"))

        # Force expiration and validate refresh path issues a new token.
        client.oauth_token["expires_at"] = time.time() - 5.0
        result = manager.call_tool("oauth_url", "echo", {"message": "hi"}, timeout_s=12.0)
        self.assertIn("echo:hi", str(result.get("content", [{}])[0].get("text", "")))
        token_after = str(client.oauth_token.get("access_token", ""))
        self.assertTrue(token_after.startswith("at-"))
        self.assertNotEqual(token_before, token_after)

        token_store = self.config_dir / "oauth_tokens.json"
        self.assertTrue(token_store.exists())
        text = token_store.read_text(encoding="utf-8")
        self.assertIn("oauth_url::", text)
        self.assertIn("access_token", text)

    def test_oauth_flow_with_dynamic_client_registration(self):
        manager = self._start_server_and_manager(with_client_id=False)
        tools, _ = manager.list_tools("oauth_url", use_cache=False, timeout_s=12.0)
        self.assertIn("echo", [str(t.get("name", "")) for t in tools if isinstance(t, dict)])
        out = manager.call_tool("oauth_url", "echo", {"message": "dyn"}, timeout_s=12.0)
        self.assertIn("echo:dyn", str(out.get("content", [{}])[0].get("text", "")))

    def test_scope_step_up_from_403_insufficient_scope(self):
        manager = self._start_server_and_manager(with_client_id=True)
        # initial auth should acquire read scope by challenge/default
        _tools, _ = manager.list_tools("oauth_url", use_cache=False, timeout_s=12.0)
        client = manager._clients.get("oauth_url")
        self.assertTrue(client is not None)
        scope_before = str(client.oauth_token.get("scope", "") or "")
        self.assertIn("files:read", scope_before)

        # This operation requires write scope and should trigger step-up auth.
        out = manager.call_tool("oauth_url", "echo", {"message": "need-write"}, timeout_s=12.0)
        self.assertIn("echo:need-write", str(out.get("content", [{}])[0].get("text", "")))
        scope_after = str(client.oauth_token.get("scope", "") or "")
        self.assertIn("files:write", scope_after)

    def test_manual_auth_fallback_on_401_without_challenge(self):
        manual_token = "figma-manual-token"
        manager = self._start_server_and_manager(
            with_client_id=False,
            no_challenge=True,
            manual_token=manual_token,
            include_oauth=False,
        )
        orig_input = builtins.input
        orig_getpass = getpass.getpass
        try:
            builtins.input = lambda _prompt="": f"Authorization: Bearer {manual_token}"
            getpass.getpass = lambda _prompt="": f"Authorization: Bearer {manual_token}"
            tools, _ = manager.list_tools("oauth_url", use_cache=False, timeout_s=12.0)
            self.assertIn("echo", [str(t.get("name", "")) for t in tools if isinstance(t, dict)])
        finally:
            builtins.input = orig_input
            getpass.getpass = orig_getpass
        client = manager._clients.get("oauth_url")
        self.assertTrue(client is not None)
        self.assertEqual(str(client.oauth_token.get("access_token", "")), manual_token)

    def test_manual_invalid_token_not_reprompted_across_reconnect(self):
        manager = self._start_server_and_manager(
            with_client_id=False,
            no_challenge=True,
            manual_token="server-valid-token",
            include_oauth=False,
        )
        calls = {"n": 0}
        orig_getpass = getpass.getpass
        try:
            def _fake_getpass(_prompt: str = "") -> str:
                calls["n"] += 1
                return "Bearer bad-token"

            getpass.getpass = _fake_getpass
            with self.assertRaises(Exception):
                manager.list_tools("oauth_url", use_cache=False, timeout_s=12.0)
            self.assertEqual(calls["n"], 1)

            # Force a fresh client (simulates mcp_reconnect/new session).
            manager._clients.pop("oauth_url", None)
            with self.assertRaises(Exception):
                manager.list_tools("oauth_url", use_cache=False, timeout_s=12.0)
            # Should not ask for the same rejected token again.
            self.assertEqual(calls["n"], 1)
        finally:
            getpass.getpass = orig_getpass


if __name__ == "__main__":
    unittest.main()

