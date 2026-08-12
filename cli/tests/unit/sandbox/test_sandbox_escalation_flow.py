"""Integration tests: bypass_sandbox wiring inside action_shell_command."""

import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cli.core.sandbox import SANDBOX_LEVEL_FULL_ACCESS, normalize_sandbox_level
from cli.tools.shell import ShellTool
from cli.tools.shell import action_shell_command
from cli.tests.unit.shell.test_shell_command_execution_guards import _DummyAgent


class _FakePlan:
    level = "workspace_write"
    network = False
    config_dir = "/tmp/cfg"


class _NoopLoader:
    def exec_module(self, _module):
        return None


def _fake_module_from_spec(spec):
    module = types.ModuleType(spec.name)
    module.sandbox_block_error = lambda _agent: None
    module.sandbox_plan_for_agent = lambda _agent: spec.plan
    return module


def _patch_sandbox_loader(plan=_FakePlan()):
    """Fake the by-path sandbox module load so a plan is always resolved."""
    spec = SimpleNamespace(
        name="codewood_sandbox_runtime",
        loader=_NoopLoader(),
        plan=plan,
    )
    return patch(
        "importlib.util.spec_from_file_location",
        return_value=spec,
    ), patch(
        "importlib.util.module_from_spec",
        side_effect=_fake_module_from_spec,
    )


class SandboxEscalationFlowTests(unittest.TestCase):
    def setUp(self):
        # Keep workspace snapshots fast: point the fake agent at a small
        # temp directory instead of the (large) repository root.
        self._tmp = Path(tempfile.mkdtemp(prefix="codewood-sandbox-test-"))

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _agent(self):
        agent = _DummyAgent()
        agent.sandbox_level = "workspace_write"
        agent.sandbox_network = False
        agent.prompt_result = True
        agent.work_directory = self._tmp
        agent.workspace_root = self._tmp
        agent.startup_initial_directory = self._tmp
        return agent

    def _attach_declined_result(self, agent):
        def _confirm_declined_result(error):
            supp = str(
                getattr(agent, "_confirm_supplement_text", "") or ""
            ).strip()
            agent._confirm_supplement_text = ""
            if supp:
                return {
                    "success": False,
                    "error": error,
                    "retryable": False,
                    "user_rejected_with_supplement": True,
                    "user_supplement": supp,
                    "message": f"User rejected and provided info: {supp}",
                }
            return {"success": False, "error": error}

        agent._confirm_declined_result = _confirm_declined_result
        return agent

    def test_approved_runs_unsandboxed_with_bypass_marker(self):
        agent = self._agent()
        spec_patch, module_patch = _patch_sandbox_loader()
        with spec_patch, module_patch:
            result = action_shell_command(
                agent,
                "python -c \"print(1)\"",
                confirmed=False,
                interactive=False,
                input_data=None,
                bypass_sandbox=True,
            )
        self.assertTrue(result["success"])
        self.assertTrue(result["sandbox_bypassed"])
        self.assertEqual(result["sandbox_level"], "workspace_write")
        # The escalation approval was the only confirmation prompt: the
        # generic command confirmation must NOT be re-prompted afterwards.
        self.assertEqual(len(agent.prompt_calls), 1)

    def test_unlimited_policy_auto_approves_bypass_without_prompt(self):
        agent = self._agent()
        agent.execution_policy = "unlimited"
        spec_patch, module_patch = _patch_sandbox_loader()
        with spec_patch, module_patch:
            result = action_shell_command(
                agent,
                "python -c \"print(1)\"",
                confirmed=False,
                interactive=False,
                input_data=None,
                bypass_sandbox=True,
            )
        self.assertTrue(result["success"])
        self.assertTrue(result["sandbox_bypassed"])
        self.assertEqual(result["sandbox_level"], "workspace_write")
        # Unlimited mode: the bypass is auto-approved, no prompt at all.
        self.assertEqual(len(agent.prompt_calls), 0)

    def test_rejected_ends_task_without_execution(self):
        agent = self._agent()
        agent.prompt_result = False
        spec_patch, module_patch = _patch_sandbox_loader()
        with spec_patch, module_patch:
            result = action_shell_command(
                agent,
                "python -c \"print(1)\"",
                confirmed=True,
                interactive=False,
                input_data=None,
                bypass_sandbox=True,
            )
        self.assertFalse(result["success"])
        self.assertTrue(result["user_cancelled"])
        self.assertTrue(result["sandbox_escalation_rejected"])

    def test_rejected_with_supplement_keeps_task_running(self):
        agent = self._agent()
        agent.prompt_result = False
        self._attach_declined_result(agent)
        agent.supplement_for_prompt = "Use a local mirror instead"

        def _decline_with_supplement(_prompt, **_kwargs):
            agent._confirm_supplement_text = agent.supplement_for_prompt
            return False

        agent._prompt_confirm_yes_no_maybe_always = _decline_with_supplement
        spec_patch, module_patch = _patch_sandbox_loader()
        with spec_patch, module_patch:
            result = action_shell_command(
                agent,
                "python -c \"print(1)\"",
                confirmed=True,
                interactive=False,
                input_data=None,
                bypass_sandbox=True,
            )
        self.assertFalse(result["success"])
        self.assertTrue(result["user_rejected_with_supplement"])
        self.assertEqual(result["user_supplement"], "Use a local mirror instead")
        self.assertNotIn("user_cancelled", result)

    def test_bypass_is_noop_without_sandbox_plan(self):
        agent = self._agent()
        agent.sandbox_level = "full_access"
        spec_patch, module_patch = _patch_sandbox_loader(plan=None)
        with spec_patch, module_patch:
            result = action_shell_command(
                agent,
                "python -c \"print(1)\"",
                confirmed=True,
                interactive=False,
                input_data=None,
                bypass_sandbox=True,
            )
        self.assertTrue(result["success"])
        self.assertNotIn("sandbox_bypassed", result)


class _FakeShellAgent:
    """Minimal agent for ShellTool.execute (AI-review skip paths)."""

    def __init__(self):
        self._mcp_pending_user_input = {}
        self.work_directory = Path.cwd()
        self.operation_results = []
        self.sandbox_level = "workspace_write"
        self.ai_review_calls = 0
        self.action_kwargs = None

    def _freedom_auto_confirm(self, _cmd):
        self.ai_review_calls += 1
        return True

    def action_shell_command(self, command, **kwargs):
        self.action_kwargs = kwargs
        return {"success": True}


class ShellToolBypassAiReviewTests(unittest.TestCase):
    def test_bypass_skips_ai_review_when_sandbox_active(self):
        agent = _FakeShellAgent()
        with patch(
            "cli.tools.shell._sandbox_active_for_agent", return_value=True
        ):
            result = ShellTool().execute(
                agent, {"command": "git push", "bypass_sandbox": True}
            )
        self.assertTrue(result["success"])
        self.assertEqual(agent.ai_review_calls, 0)
        self.assertTrue(agent.action_kwargs["bypass_sandbox"])
        # confirmed stays False: a rejected escalation returns before any
        # execution, an approved one suppresses the generic confirmation.
        self.assertFalse(agent.action_kwargs["confirmed"])

    def test_bypass_keeps_ai_review_when_sandbox_inactive(self):
        agent = _FakeShellAgent()
        with patch(
            "cli.tools.shell._sandbox_active_for_agent", return_value=False
        ):
            result = ShellTool().execute(
                agent, {"command": "git push", "bypass_sandbox": True}
            )
        self.assertTrue(result["success"])
        self.assertEqual(agent.ai_review_calls, 1)

    def test_without_bypass_keeps_ai_review(self):
        agent = _FakeShellAgent()
        with patch(
            "cli.tools.shell._sandbox_active_for_agent", return_value=True
        ):
            result = ShellTool().execute(agent, {"command": "git push"})
        self.assertTrue(result["success"])
        self.assertEqual(agent.ai_review_calls, 1)


class SandboxActiveForAgentTests(unittest.TestCase):
    class _FakeBackend:
        def __init__(self, supported=True):
            self._supported = supported

        def is_supported(self):
            return self._supported

    def _agent(self, level):
        return SimpleNamespace(
            sandbox_level=level,
            sandbox_network=True,
            config_dir="/tmp/cfg",
        )

    def test_full_access_inactive(self):
        from cli.tools.shell import _sandbox_active_for_agent

        self.assertFalse(_sandbox_active_for_agent(self._agent("full_access")))

    def test_supported_platform_active(self):
        from cli.tools.shell import _sandbox_active_for_agent

        with patch(
            "cli.core.sandbox.get_sandbox_backend",
            return_value=self._FakeBackend(supported=True),
        ):
            self.assertTrue(
                _sandbox_active_for_agent(self._agent("workspace_write"))
            )

    def test_unsupported_platform_inactive(self):
        from cli.tools.shell import _sandbox_active_for_agent

        with patch(
            "cli.core.sandbox.get_sandbox_backend",
            return_value=self._FakeBackend(supported=False),
        ):
            self.assertFalse(
                _sandbox_active_for_agent(self._agent("read_only"))
            )


if __name__ == "__main__":
    unittest.main()
