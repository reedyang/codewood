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

import src.agent as agent_module


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
        try:
            from src.core.logging.app_logging import shutdown_app_logging_handlers

            shutdown_app_logging_handlers()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _write_config(self, payload: dict) -> None:
        (self.config_dir / "config.jsonc").write_text(
            json.dumps(
                {
                    "execution_policy": "confirmation",
                    "mcp_tools_enabled": True,
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
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            tools_result = self.agent.execute_tool_call(
                "mcp_list_tools",
                {"server": server_name, "use_cache": False, "timeout_s": 8.0},
            )
        self.assertTrue(tools_result.get("success"), tools_result)

        if server_name == "fake_stdio":
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                stream_result = self.agent.execute_tool_call(
                    "mcp_call_tool",
                    {
                        "server": server_name,
                        "tool": "echo_stream",
                        "arguments": {"message": "flow"},
                        "timeout_s": 10.0,
                    },
                )
            self.assertTrue(stream_result.get("success"), stream_result)
            stream_meta = stream_result.get("result", {}).get("_stream", {})
            self.assertEqual(stream_meta.get("chunk_count"), 3)
            self.assertIn("flow-A", str(stream_meta.get("text", "")))
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                bad_call = self.agent.execute_tool_call(
                    "mcp_call_tool",
                    {
                        "server": server_name,
                        "tool": "echo",
                        "arguments": {"message": 123},
                        "timeout_s": 10.0,
                    },
                )
            self.assertFalse(bad_call.get("success", True))
            self.assertIn("schema", str(bad_call.get("error", "")))
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                bidi_result = self.agent.execute_tool_call(
                    "mcp_call_tool",
                    {
                        "server": server_name,
                        "tool": "ask_client",
                        "arguments": {"message": "from-server", "maxTokens": 16},
                        "timeout_s": 10.0,
                    },
                )
            self.assertTrue(bidi_result.get("success"), bidi_result)
            self.assertIn("ask_client:[client-sampled", str(bidi_result.get("result", {}).get("content", [{}])[0].get("text", "")))

        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            batch_result = self.agent.execute_tool_call(
                "mcp_call_tool_batch",
                {
                    "server": server_name,
                    "calls": [
                        {"tool": "echo", "arguments": {"message": "b1"}},
                        {"tool": "echo", "arguments": {"message": "b2"}},
                    ],
                    "timeout_s": 10.0,
                },
            )
        self.assertTrue(batch_result.get("success"), batch_result)
        results = batch_result.get("results", [])
        self.assertTrue(isinstance(results, list) and len(results) == 2)
        self.assertIn("echo:b1", str(results[0].get("content", [{}])[0].get("text", "")))
        self.assertEqual(batch_result.get("total_count"), 2)
        self.assertEqual(batch_result.get("ok_count"), 2)
        self.assertEqual(batch_result.get("error_count"), 0)
        self.assertFalse(batch_result.get("has_error"))
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            tolerant_batch = self.agent.execute_tool_call(
                "mcp_call_tool_batch",
                {
                    "server": server_name,
                    "calls": [
                        {"tool": "echo", "arguments": {"message": "ok"}},
                        {"tool": "tool_not_found_for_partial_test", "arguments": {}},
                    ],
                    "allow_partial_failure": True,
                    "timeout_s": 10.0,
                },
            )
        self.assertTrue(tolerant_batch.get("success"), tolerant_batch)
        tolerant_results = tolerant_batch.get("results", [])
        self.assertEqual(len(tolerant_results), 2)
        self.assertTrue(tolerant_results[0].get("ok"))
        self.assertIn("echo:ok", str(tolerant_results[0].get("result", {}).get("content", [{}])[0].get("text", "")))
        self.assertFalse(tolerant_results[1].get("ok"))
        self.assertEqual(tolerant_batch.get("total_count"), 2)
        self.assertEqual(tolerant_batch.get("ok_count"), 1)
        self.assertEqual(tolerant_batch.get("error_count"), 1)
        self.assertTrue(tolerant_batch.get("has_error"))

        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            list_result = self.agent.execute_tool_call(
                "mcp_list_resources",
                {"server": server_name, "use_cache": False, "timeout_s": 8.0},
            )
        self.assertTrue(list_result.get("success"), list_result)
        resources = list_result.get("resources", [])
        self.assertIn("fake://docs/readme", [str(x.get("uri", "")) for x in resources if isinstance(x, dict)])

        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            stream_result = self.agent.execute_tool_call(
                "mcp_call_tool",
                {
                    "server": server_name,
                    "tool": "echo_stream",
                    "arguments": {"message": "flow"},
                    "timeout_s": 10.0,
                },
            )
        self.assertTrue(stream_result.get("success"), stream_result)
        stream_meta = stream_result.get("result", {}).get("_stream", {})
        self.assertEqual(stream_meta.get("chunk_count"), 3)
        if server_name == "fake_stdio":
            self.assertIn("flow-A", str(stream_meta.get("text", "")))
        if server_name == "fake_url":
            self.assertIn("flow-U1", str(stream_meta.get("text", "")))
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            elicited_tool = self.agent.execute_tool_call(
                "mcp_call_tool",
                {
                    "server": server_name,
                    "tool": "ask_elicitation",
                    "arguments": {"title": "Need inputs", "message": "collect fields"},
                    "timeout_s": 10.0,
                },
            )
        self.assertTrue(elicited_tool.get("success"), elicited_tool)
        text = str(elicited_tool.get("result", {}).get("content", [{}])[0].get("text", ""))
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

        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            sampling_result = self.agent.execute_tool_call(
                "mcp_sampling_create_message",
                {
                    "server": server_name,
                    "sampling_params": {
                        "messages": [{"role": "user", "content": {"type": "text", "text": "hi sampling"}}],
                        "maxTokens": 32,
                    },
                    "timeout_s": 8.0,
                },
            )
        self.assertTrue(sampling_result.get("success"), sampling_result)
        self.assertIn("hi sampling", str(sampling_result.get("result", {}).get("content", {}).get("text", "")))

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

