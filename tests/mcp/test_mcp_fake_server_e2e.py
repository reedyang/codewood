import importlib.util
import logging
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path


def _load_mcp_manager_module():
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "agent" / "mcp_manager.py"
    spec = importlib.util.spec_from_file_location("smart_shell_mcp_manager_e2e", str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load mcp_manager module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FakeMcpServerE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mcp_module = _load_mcp_manager_module()
        cls.McpManager = cls.mcp_module.McpManager
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.server_script = cls.repo_root / "tests" / "mcp" / "fake_mcp_server.py"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.procs = []

    def tearDown(self):
        for client in list(getattr(self, "manager_clients", [])):
            try:
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

    def _build_manager_for_stdio(self):
        cfg = {"mcpServers": {"fake_stdio": {"command": sys.executable, "args": [str(self.server_script), "--transport", "stdio"]}}}
        manager = self.McpManager(config_dir=self.config_dir, mcp_config=cfg, workspace_dir=self.config_dir / "workspace")
        self.manager_clients = []
        return manager

    def _build_manager_for_url(self):
        port = _get_free_port()
        proc = subprocess.Popen(
            [sys.executable, str(self.server_script), "--transport", "url", "--host", "127.0.0.1", "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(proc)
        url = f"http://127.0.0.1:{port}/mcp"
        _wait_http_ready(url)
        cfg = {"mcpServers": {"fake_url": {"url": url, "headers": {}}}}
        manager = self.McpManager(config_dir=self.config_dir, mcp_config=cfg, workspace_dir=self.config_dir / "workspace")
        self.manager_clients = []
        return manager

    def _assert_tools_resources_prompts_sampling_completion(self, manager, server_name: str):
        tools, _ = manager.list_tools(server_name, use_cache=False, timeout_s=8.0)
        self.assertIn("echo", [str(t.get("name", "")) for t in tools if isinstance(t, dict)])
        tool_result = manager.call_tool(server_name, "echo", {"message": "hi"}, timeout_s=10.0)
        self.assertIn("echo:hi", str(tool_result.get("content", [{}])[0].get("text", "")))
        batch_result = manager.call_tools_batch(
            server_name,
            [
                {"tool": "echo", "arguments": {"message": "b1"}},
                {"tool": "echo", "arguments": {"message": "b2"}},
            ],
            timeout_s=10.0,
        )
        self.assertEqual(len(batch_result), 2)
        self.assertIn("echo:b1", str(batch_result[0].get("content", [{}])[0].get("text", "")))
        tolerant_batch = manager.call_tools_batch(
            server_name,
            [
                {"tool": "echo", "arguments": {"message": "ok"}},
                {"tool": "tool_not_found_for_partial_test", "arguments": {}},
            ],
            timeout_s=10.0,
            allow_partial_failure=True,
        )
        self.assertEqual(len(tolerant_batch), 2)
        self.assertTrue(tolerant_batch[0].get("ok"))
        self.assertIn("echo:ok", str(tolerant_batch[0].get("result", {}).get("content", [{}])[0].get("text", "")))
        self.assertFalse(tolerant_batch[1].get("ok"))
        self.assertIn("unknown tool", str(tolerant_batch[1].get("error", {})).lower())
        if server_name == "fake_stdio":
            stream_result = manager.call_tool(server_name, "echo_stream", {"message": "flow"}, timeout_s=10.0)
            stream_meta = stream_result.get("_stream", {})
            self.assertEqual(stream_meta.get("chunk_count"), 3)
            self.assertIn("flow-A", str(stream_meta.get("text", "")))
            self.assertIn("flow-C", str(stream_meta.get("text", "")))
        if server_name == "fake_url":
            stream_result = manager.call_tool(server_name, "echo_stream", {"message": "flow"}, timeout_s=10.0)
            stream_meta = stream_result.get("_stream", {})
            self.assertEqual(stream_meta.get("chunk_count"), 3)
            self.assertIn("flow-U1", str(stream_meta.get("text", "")))
            self.assertIn("flow-U3", str(stream_meta.get("text", "")))
        if server_name == "fake_stdio":
            bidi_result = manager.call_tool(
                server_name,
                "ask_client",
                {"message": "from-server", "maxTokens": 24},
                timeout_s=10.0,
            )
            self.assertIn("ask_client:[client-sampled", str(bidi_result.get("content", [{}])[0].get("text", "")))

        resources, _ = manager.list_resources(server_name, use_cache=False, timeout_s=8.0)
        self.assertIn("fake://docs/readme", [str(r.get("uri", "")) for r in resources if isinstance(r, dict)])
        read_result = manager.read_resource(server_name, "fake://docs/readme", timeout_s=10.0)
        self.assertIn("fake MCP resource", str(read_result.get("contents", [{}])[0].get("text", "")))

        templates, _ = manager.list_resource_templates(server_name, use_cache=False, timeout_s=8.0)
        self.assertTrue(isinstance(templates, list) and len(templates) > 0)

        prompts, _ = manager.list_prompts(server_name, use_cache=False, timeout_s=8.0)
        self.assertIn("summarize_text", [str(p.get("name", "")) for p in prompts if isinstance(p, dict)])
        prompt_result = manager.get_prompt(server_name, "summarize_text", {"text": "hello world"}, timeout_s=10.0)
        self.assertIn("hello world", str(prompt_result.get("messages", [{}])[-1].get("content", {}).get("text", "")))

        sampled = manager.sampling_create_message(
            server_name,
            {"messages": [{"role": "user", "content": {"type": "text", "text": "hi sampling"}}], "maxTokens": 16},
            timeout_s=10.0,
        )
        self.assertIn("hi sampling", str(sampled.get("content", {}).get("text", "")))

        completion = manager.completion_complete(
            server_name,
            {"ref": {"name": "summarize_text"}, "argument": {"name": "text", "value": "hel"}},
            timeout_s=10.0,
        )
        values = completion.get("completion", {}).get("values", [])
        self.assertTrue(isinstance(values, list) and len(values) > 0)
        self.assertIn("hel-1", values)
        elicited_tool = manager.call_tool(
            server_name,
            "ask_elicitation",
            {"title": "Need inputs", "message": "collect fields"},
            timeout_s=10.0,
        )
        text = str(elicited_tool.get("content", [{}])[0].get("text", ""))
        self.assertIn("ask_elicitation:accept", text)
        self.assertIn('"name"', text)
        self.manager_clients = list(getattr(manager, "_clients", {}).values())

    def test_e2e_stdio_fake_server(self):
        self._assert_tools_resources_prompts_sampling_completion(self._build_manager_for_stdio(), "fake_stdio")

    def test_e2e_url_fake_server(self):
        self._assert_tools_resources_prompts_sampling_completion(self._build_manager_for_url(), "fake_url")


if __name__ == "__main__":
    unittest.main()
