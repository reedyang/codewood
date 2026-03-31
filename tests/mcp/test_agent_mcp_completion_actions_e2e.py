import contextlib
import io
import json
import logging
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.request
from pathlib import Path


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

import agent.smart_shell_agent as smart_shell_agent_module


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_http_ready(url: str, timeout_s: float = 5.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                url=url,
                method="POST",
                data=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=0.8) as resp:
                if int(getattr(resp, "status", 0) or 0) in (200, 204):
                    return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("Fake MCP URL server not ready")


class AgentMcpCompletionActionsE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.server_script = cls.repo_root / "tests" / "mcp" / "fake_mcp_server.py"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.procs = []
        self.agent = None

    def tearDown(self):
        if self.agent is not None:
            try:
                for client in list(getattr(self.agent.mcp_manager, "_clients", {}).values()):
                    client._shutdown_unlocked()
            except Exception:
                pass
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

    def _write_config(self, payload: dict) -> None:
        (self.config_dir / "config.json").write_text(
            json.dumps({"knowledge_enabled": False, "execution_policy": "confirmation"}, ensure_ascii=False),
            encoding="utf-8",
        )
        (self.config_dir / "mcp.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _build_agent(self):
        smart_shell_agent_module.TAB_COMPLETION_AVAILABLE = False
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            return smart_shell_agent_module.SmartShellAgent(
                provider="openai",
                model_name="dummy",
                params={},
                work_directory=str(self.repo_root),
                config_dir=str(self.config_dir),
            )

    def _assert_completion_action(self, server_name: str):
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            completion_result = self.agent.execute_tool_call(
                "mcp_completion_complete",
                {
                    "server": server_name,
                    "completion_params": {"ref": {"name": "summarize_text"}, "argument": {"name": "text", "value": "hel"}},
                    "timeout_s": 8.0,
                },
            )
        self.assertTrue(completion_result.get("success"), completion_result)
        values = completion_result.get("result", {}).get("completion", {}).get("values", [])
        self.assertTrue(isinstance(values, list) and len(values) > 0)
        self.assertIn("hel-1", values)

    def test_execute_command_mcp_completion_stdio(self):
        self._write_config(
            {
                "mcpServers": {
                    "fake_stdio": {
                        "command": sys.executable,
                        "args": [str(self.server_script), "--transport", "stdio"],
                        "skip_preload": True,
                    }
                }
            }
        )
        self.agent = self._build_agent()
        self._assert_completion_action("fake_stdio")

    def test_execute_command_mcp_completion_url(self):
        port = _get_free_port()
        proc = subprocess.Popen(
            [sys.executable, str(self.server_script), "--transport", "url", "--host", "127.0.0.1", "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(proc)
        url = f"http://127.0.0.1:{port}/mcp"
        _wait_http_ready(url)
        self._write_config({"mcpServers": {"fake_url": {"url": url, "headers": {}, "skip_preload": True}}})
        self.agent = self._build_agent()
        self._assert_completion_action("fake_url")


if __name__ == "__main__":
    unittest.main()
