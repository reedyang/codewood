"""The settings page treats the elevated sandbox phase as "setup complete".

Once the elevated setup writes ``sandbox_users_ready.flag`` the page must show
the sandbox as provisioned (with fresh credentials) even while the serve-side
ACL phase is still running in the background -- it must NOT wait for the
``sandbox_provisioned.flag`` that is only written after all ACL work.
"""

import os
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from cli.server.serve_app import ServeApp, _sandbox_flag_mtime, _sandbox_ready_mtime


def _stub_agent(config_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        config_dir=str(config_dir),
        sandbox_level="workspace_write",
        sandbox_network=False,
        workspace_root=None,
        work_directory=None,
        _resolved_config_data=None,
    )


def _backend(status: dict, verify_side_effect) -> SimpleNamespace:
    return SimpleNamespace(
        status=lambda config_dir, workspace_root: dict(status),
        verify_credentials=lambda config_dir, fresh=False: verify_side_effect(
            config_dir, fresh
        ),
        name="windows",
    )


class SandboxConfigSetupCompletionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self._tmp.name)
        self.ready_path = self.config_dir / "sandbox_users_ready.flag"
        self.ready_path.write_text("{}", encoding="utf-8")
        key = str(self.config_dir)
        _sandbox_flag_mtime.pop(key, None)
        _sandbox_ready_mtime.pop(key, None)

    def tearDown(self):
        self._tmp.cleanup()

    def _load(self, verify_results):
        calls = []

        def verify(config_dir, fresh=False):
            calls.append(fresh)
            return verify_results[min(len(calls), len(verify_results)) - 1]

        stub = ServeApp.__new__(ServeApp)
        stub.agent = _stub_agent(self.config_dir)
        status = {
            "supported": True,
            "provisioned": True,
            "name": "windows",
            "users_exist": True,
            "offline_user": "CodewoodSandOffline",
            "online_user": "CodewoodSandOnline",
            "secret_exists": True,
            "users_foreign": False,
        }
        with patch(
            "cli.core.sandbox.config.read_sandbox_settings", return_value={}
        ), patch(
            "cli.core.sandbox.get_sandbox_backend",
            return_value=_backend(status, verify),
        ), patch(
            "cli.core.sandbox.windows._flag_path",
            return_value=self.config_dir / "sandbox_provisioned.flag",
        ), patch(
            "cli.core.sandbox.windows._users_ready_path",
            return_value=self.ready_path,
        ):
            out = stub.get_sandbox_config()
        return out, calls

    def test_refresh_verifies_fresh_when_ready_flag_advances(self):
        # First load: no previous mtime -> fresh re-verify happens.
        out, calls = self._load([False, True])
        self.assertTrue(out["provisioned"])
        self.assertTrue(out["passwords_ok"])
        self.assertEqual(calls.count(True), 1)

        # Steady state: ready flag unchanged -> cached False is trusted.
        out, calls = self._load([False])
        self.assertFalse(out["passwords_ok"])
        self.assertEqual(calls, [False])

        # A new elevated setup advances the ready flag -> stale cached False
        # must be dropped and the fresh accounts verified.
        now = self.ready_path.stat().st_mtime
        os.utime(self.ready_path, (now + 10, now + 10))
        out, calls = self._load([False, True])
        self.assertTrue(out["passwords_ok"])
        self.assertEqual(calls.count(True), 1)


if __name__ == "__main__":
    unittest.main()
