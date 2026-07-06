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


class AgentMcpActionsE2ETests(unittest.TestCase):
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

    def _assert_actions(self, server_name: str):
        sink = io.StringIO()
        self.agent.mcp_manager.list_tools(server_name, use_cache=False)
        if server_name == "fake_stdio":
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                stream_result = self.agent.mcp_manager.call_tool(
                    server_name, "echo_stream", {"message": "flow"}, timeout_s=10.0,
                )
            self.assertIn("flow", str(stream_result.get("content", [{}])[0].get("text", "")))
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                with self.assertRaises(Exception) as ctx:
                    self.agent.mcp_manager.call_tool(
                        server_name, "echo", {"message": 123}, timeout_s=10.0,
                    )
                self.assertIn("schema", str(ctx.exception))
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                bidi_result = self.agent.mcp_manager.call_tool(
                    server_name, "ask_client", {"message": "from-server", "maxTokens": 16}, timeout_s=10.0,
                )
            self.assertIn("ask_client:[client-sampled", str(bidi_result.get("content", [{}])[0].get("text", "")))

        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            batch_results = self.agent.mcp_manager.call_tools_batch(
                server_name,
                [
                    {"tool": "echo", "arguments": {"message": "b1"}},
                    {"tool": "echo", "arguments": {"message": "b2"}},
                ],
                timeout_s=10.0,
            )
        self.assertTrue(isinstance(batch_results, list) and len(batch_results) == 2)
        self.assertIn("echo:b1", str(batch_results[0].get("content", [{}])[0].get("text", "")))
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            tolerant_results = self.agent.mcp_manager.call_tools_batch(
                server_name,
                [
                    {"tool": "echo", "arguments": {"message": "ok"}},
                    {"tool": "tool_not_found_for_partial_test", "arguments": {}},
                ],
                timeout_s=10.0,
                allow_partial_failure=True,
            )
        self.assertEqual(len(tolerant_results), 2)
        self.assertTrue(tolerant_results[0].get("ok"))
        self.assertIn("echo:ok", str(tolerant_results[0].get("result", {}).get("content", [{}])[0].get("text", "")))
        self.assertFalse(tolerant_results[1].get("ok"))

        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            list_result = self.agent.execute_tool_call(
                "mcp_list_resources",
                {"server": server_name, "use_cache": False, "timeout_s": 8.0},
            )
        self.assertTrue(list_result.get("success"), list_result)
        resources = list_result.get("resources", [])
        self.assertIn("fake://docs/readme", [str(x.get("uri", "")) for x in resources if isinstance(x, dict)])

        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            stream_result = self.agent.mcp_manager.call_tool(
                server_name, "echo_stream", {"message": "flow"}, timeout_s=10.0,
            )
        self.assertIn("flow", str(stream_result.get("content", [{}])[0].get("text", "")))
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            elicited_tool = self.agent.mcp_manager.call_tool(
                server_name, "ask_elicitation",
                {"title": "Need inputs", "message": "collect fields"},
                timeout_s=10.0,
            )
        text = str(elicited_tool.get("content", [{}])[0].get("text", ""))
        self.assertIn("ask_elicitation:accept", text)

        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            read_result = self.agent.execute_tool_call(
                "mcp_read_resource",
                {"server": server_name, "uri": "fake://docs/readme", "timeout_s": 8.0},
            )
        self.assertTrue(read_result.get("success"), read_result)

        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            list_prompts_result = self.agent.execute_tool_call(
                "mcp_list_prompts",
                {"server": server_name, "use_cache": False, "timeout_s": 8.0},
            )
        self.assertTrue(list_prompts_result.get("success"), list_prompts_result)

        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            get_prompt_result = self.agent.execute_tool_call(
                "mcp_get_prompt",
                {
                    "server": server_name,
                    "prompt": "summarize_text",
                    "arguments": {"text": "hello world"},
                    "timeout_s": 8.0,
                },
            )
        self.assertTrue(get_prompt_result.get("success"), get_prompt_result)

    def test_execute_command_mcp_resources_stdio(self):
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
        self._assert_actions("fake_stdio")

    def test_execute_command_mcp_resources_url(self):
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
        self._assert_actions("fake_url")


if __name__ == "__main__":
    unittest.main()


