import tempfile
import threading
import unittest
from pathlib import Path

from cli.server.serve_app import ServeApp


class _FakeBroadcaster:
    def publish(self, event, data):
        _ = (event, data)
        return None


class _FakeMcpManager:
    def __init__(self) -> None:
        self.mcp_config = {"mcpServers": {"demo": {"command": "python", "args": []}}}
        self._tools_cache = {
            "demo": {"tools": [{"name": "a"}, {"name": "b"}], "source": "stdio"}
        }
        self._prompts_cache = {
            "demo": {"prompts": [{"name": "p1"}, {"name": "p2"}, {"name": "p3"}], "source": "stdio"}
        }

    def get_status(self, log_limit=20):
        _ = log_limit
        return {
            "servers": {
                "demo": {
                    "state": "loading",
                    "last_error": "",
                    "tools_count": 2,
                    "prompts_count": 3,
                }
            }
        }

    def list_disabled_tools(self, server=None):
        _ = server
        return {}


class _FakeAgent:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.mcp_manager = _FakeMcpManager()


def _app(cfg_dir: Path) -> ServeApp:
    class _Stub:
        pass

    stub = _Stub()
    stub.agent = _FakeAgent(cfg_dir)
    stub.broadcaster = _FakeBroadcaster()
    stub._mcp_reconnect_threads = {}
    stub._mcp_reconnect_lock = threading.Lock()
    for name in ("_mcp_load_jsonc", "get_mcp_overview", "_prefetch_mcp_icons"):
        setattr(stub, name, getattr(ServeApp, name).__get__(stub, _Stub))
    return stub  # type: ignore[return-value]


class ServeAppMcpOverviewTests(unittest.TestCase):
    def test_overview_uses_servers_status_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp)
            (cfg_dir / "mcp.jsonc").write_text(
                '{\n'
                '  "mcpServers": {\n'
                '    "demo": {\n'
                '      "command": "python",\n'
                '      "args": []\n'
                "    }\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            app = _app(cfg_dir)
            payload = app.get_mcp_overview()
            servers = payload.get("servers", [])
            self.assertEqual(len(servers), 1)
            self.assertEqual(servers[0]["state"], "loading")
            self.assertEqual(servers[0]["toolsCount"], 2)
            self.assertEqual(servers[0]["promptsCount"], 3)

    def test_overview_prefers_total_tool_count_over_enabled_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp)
            (cfg_dir / "mcp.jsonc").write_text(
                '{\n'
                '  "mcpServers": {\n'
                '    "demo": {\n'
                '      "command": "python",\n'
                '      "args": []\n'
                "    }\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            app = _app(cfg_dir)
            app.agent.mcp_manager._tools_cache["demo"] = {
                "tools": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
                "source": "stdio",
            }
            original_get_status = app.agent.mcp_manager.get_status
            app.agent.mcp_manager.get_status = lambda log_limit=20: {
                "servers": {
                    "demo": {
                        "state": "success",
                        "last_error": "",
                        "tools_count": 2,
                        "prompts_count": 3,
                    }
                }
            }
            try:
                payload = app.get_mcp_overview()
            finally:
                app.agent.mcp_manager.get_status = original_get_status
            servers = payload.get("servers", [])
            self.assertEqual(len(servers), 1)
            self.assertEqual(servers[0]["toolsCount"], 3)


if __name__ == "__main__":
    unittest.main()
