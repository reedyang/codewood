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
from pathlib import Path
import urllib.request
from cli.config.app_info import get_app_slug_snake
if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

import cli.agent as agent_module


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
        cls.repo_root = Path(__file__).resolve().parents[4]
        cls.server_script = cls.repo_root / "cli" / "tests" / "integration" / "mcp" / "fake_mcp_server.py"

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
        logger = logging.getLogger(f"{get_app_slug_snake()}.mcp")
        for handler in list(logger.handlers):
            try:
                handler.close()
            except Exception:
                pass
            try:
                logger.removeHandler(handler)
            except Exception:
                pass
        try:
            from cli.core.logging.app_logging import shutdown_app_logging_handlers

            shutdown_app_logging_handlers()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _write_config(self, payload: dict) -> None:
        (self.config_dir / "config.jsonc").write_text(
            json.dumps(
                {
                    "execution_policy": "confirmation",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        (self.config_dir / "mcp.jsonc").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _build_agent(self):
        agent_module.TAB_COMPLETION_AVAILABLE = False
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            return agent_module.Agent(
                provider="openai",
                model_name="dummy",
                params={},
                work_directory=str(self.repo_root),
                config_dir=str(self.config_dir),
            )


if __name__ == "__main__":
    unittest.main()


