import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cli.core.sandbox import (
    SandboxPlan,
    sandbox_block_error,
    sandbox_plan_for_agent,
)


class FakeBackend:
    def __init__(self, supported=True, provisioned=True):
        self.supported = supported
        self.provisioned = provisioned
        self.spawned = []

    def is_supported(self):
        return self.supported

    def is_provisioned(self, config_dir, workspace_root=None):
        return self.provisioned

    def spawn(self, **kwargs):
        self.spawned.append(kwargs)
        return "proc"


def _agent(**attrs):
    base = {"config_dir": "/tmp/cfg", "sandbox_level": None, "sandbox_network": None}
    base.update(attrs)
    return SimpleNamespace(**base)


class SandboxPlanForAgentTests(unittest.TestCase):
    def test_full_access_returns_none(self):
        agent = _agent(sandbox_level="full_access", sandbox_network=True)
        with patch(
            "cli.core.sandbox.get_sandbox_backend", return_value=FakeBackend()
        ):
            self.assertIsNone(sandbox_plan_for_agent(agent))

    def test_unsupported_platform_returns_none(self):
        agent = _agent(sandbox_level="read_only", sandbox_network=True)
        with patch(
            "cli.core.sandbox.get_sandbox_backend",
            return_value=FakeBackend(supported=False),
        ):
            self.assertIsNone(sandbox_plan_for_agent(agent))

    def test_workspace_write_returns_plan(self):
        agent = _agent(sandbox_level="workspace_write", sandbox_network=True)
        with patch(
            "cli.core.sandbox.get_sandbox_backend", return_value=FakeBackend()
        ):
            plan = sandbox_plan_for_agent(agent)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.level, "workspace_write")
        self.assertTrue(plan.network)
        self.assertEqual(plan.config_dir, "/tmp/cfg")

    def test_defaults_when_attrs_missing(self):
        agent = _agent()
        with patch(
            "cli.core.sandbox.get_sandbox_backend", return_value=FakeBackend()
        ):
            self.assertIsNone(sandbox_plan_for_agent(agent))


class SandboxBlockErrorTests(unittest.TestCase):
    def test_none_when_full_access(self):
        agent = _agent(sandbox_level="full_access")
        with patch(
            "cli.core.sandbox.get_sandbox_backend", return_value=FakeBackend()
        ):
            self.assertIsNone(sandbox_block_error(agent))

    def test_none_when_provisioned(self):
        agent = _agent(sandbox_level="read_only")
        with patch(
            "cli.core.sandbox.get_sandbox_backend",
            return_value=FakeBackend(provisioned=True),
        ):
            self.assertIsNone(sandbox_block_error(agent))

    def test_error_when_not_provisioned(self):
        agent = _agent(sandbox_level="read_only")
        with patch(
            "cli.core.sandbox.get_sandbox_backend",
            return_value=FakeBackend(provisioned=False),
        ):
            error = sandbox_block_error(agent)
        self.assertIsNotNone(error)
        self.assertIn("not provisioned", error)


class SandboxPlanSpawnTests(unittest.TestCase):
    def test_spawn_passes_config_dir(self):
        backend = FakeBackend()
        plan = SandboxPlan(
            level="workspace_write",
            network=False,
            backend=backend,
            config_dir="/tmp/cfg",
        )
        plan.spawn("echo hi", cwd="/tmp", env={"PATH": "/bin"}, stdin_data=b"x")
        self.assertEqual(len(backend.spawned), 1)
        call = backend.spawned[0]
        self.assertEqual(call["command"], "echo hi")
        self.assertEqual(call["cwd"], "/tmp")
        self.assertEqual(call["env"], {"PATH": "/bin"})
        self.assertEqual(call["stdin_data"], b"x")
        self.assertEqual(call["level"], "workspace_write")
        self.assertFalse(call["network"])
        self.assertEqual(call["config_dir"], "/tmp/cfg")

    def test_is_provisioned_delegates(self):
        backend = MagicMock()
        backend.is_provisioned.return_value = True
        plan = SandboxPlan(
            level="read_only",
            network=True,
            backend=backend,
            config_dir="/tmp/cfg",
        )
        self.assertTrue(plan.is_provisioned("/tmp/cfg"))


if __name__ == "__main__":
    unittest.main()
