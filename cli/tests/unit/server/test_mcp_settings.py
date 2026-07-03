import tempfile
import threading
import time
import unittest
from pathlib import Path

from cli.server.serve_app import ServeApp


class _FakeBroadcaster:
    def publish(self, event, data):
        _ = (event, data)
        return None


class _FakeMcpManager:
    def __init__(self) -> None:
        self.mcp_config = {"mcpServers": {"slow": {"command": "python", "args": []}}}
        self.reconnect_started = threading.Event()
        self.allow_finish = threading.Event()
        self.status_updates = []

    def _set_status(self, server, state, **kwargs):
        self.status_updates.append((server, state, kwargs))

    def reconnect_server(self, server, timeout_s=12.0):
        _ = timeout_s
        self.reconnect_started.set()
        self.allow_finish.wait(1.0)
        return [{"name": f"{server}-tool"}]


class _FakeAgent:
    def __init__(self, config_dir: Path, mgr: _FakeMcpManager) -> None:
        self.config_dir = config_dir
        self.mcp_manager = mgr
        self.mcp_config = mgr.mcp_config

    def _calc_mcp_config_sig(self, cfg):
        return str(cfg)


def _app(cfg_dir: Path) -> ServeApp:
    class _Stub:
        pass

    mgr = _FakeMcpManager()
    stub = _Stub()
    stub.agent = _FakeAgent(cfg_dir, mgr)
    stub.broadcaster = _FakeBroadcaster()
    stub._mcp_reconnect_threads = {}
    stub._mcp_reconnect_lock = threading.Lock()
    for name in (
        "_mcp_load_jsonc",
        "_mcp_save_jsonc",
        "_start_mcp_reconnect_async",
        "set_mcp_server_enabled",
    ):
        setattr(stub, name, getattr(ServeApp, name).__get__(stub, _Stub))
    return stub  # type: ignore[return-value]


class ServeAppMcpSettingsTests(unittest.TestCase):
    def test_enable_server_returns_before_reconnect_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp)
            (cfg_dir / "mcp.jsonc").write_text(
                '{\n'
                '  "mcpServers": {\n'
                '    "slow": {\n'
                '      "command": "python",\n'
                '      "args": [],\n'
                '      "skip_preload": true\n'
                "    }\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            app = _app(cfg_dir)
            started = time.perf_counter()
            ok = app.set_mcp_server_enabled("slow", True)
            elapsed = time.perf_counter() - started
            self.assertTrue(ok)
            self.assertLess(elapsed, 0.25)
            self.assertTrue(app.agent.mcp_manager.reconnect_started.wait(0.5))
            self.assertIn(
                ("slow", "loading", {"last_error": "", "failure_type": "", "suggestion": ""}),
                app.agent.mcp_manager.status_updates,
            )
            saved = app._mcp_load_jsonc()
            self.assertFalse(saved["mcpServers"]["slow"].get("skip_preload", False))
            app.agent.mcp_manager.allow_finish.set()


if __name__ == "__main__":
    unittest.main()
