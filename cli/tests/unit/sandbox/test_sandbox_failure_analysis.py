"""Unit tests for sandbox failure analysis and one-time escalation approval."""

import unittest
from types import SimpleNamespace

from cli.core.sandbox import SandboxPlan
from cli.tools.shell import (
    _apply_sandbox_escalation_approval,
    _sandbox_escalation_hint,
    _sandbox_failure_analysis,
)


def _plan(level="workspace_write", network=True):
    backend = SimpleNamespace(is_supported=lambda: True)
    return SandboxPlan(level=level, network=network, backend=backend)


class _EscalationAgent:
    """Minimal agent with the confirm surface used by escalation prompts."""

    def __init__(self, confirm_result=True, supplement=""):
        self.display_language = "en"
        self.confirm_result = confirm_result
        self.supplement = supplement
        # The real confirm UI stores reject-with-supplement text on the agent
        # via set_confirm_supplement (``_confirm_supplement_text``).
        self._confirm_supplement_text = supplement
        self.prompt_calls = []

    def _prompt_confirm_yes_no_maybe_always(self, _prompt, **kwargs):
        self.prompt_calls.append(kwargs)
        if not self.confirm_result and self.supplement:
            self._confirm_supplement_text = self.supplement
        return self.confirm_result

    def _confirm_declined_result(self, error):
        if self.supplement:
            return {
                "success": False,
                "error": error,
                "retryable": False,
                "user_rejected_with_supplement": True,
                "user_supplement": self.supplement,
                "message": f"User rejected and provided info: {self.supplement}",
            }
        return {"success": False, "error": error}


class SandboxFailureAnalysisTests(unittest.TestCase):
    def test_no_plan_returns_none(self):
        self.assertIsNone(_sandbox_failure_analysis("dir", 5, "x", None))

    def test_exit_code_5_is_access_denied(self):
        result = _sandbox_failure_analysis("copy a b", 5, "", _plan())
        self.assertIsNotNone(result)
        self.assertTrue(result["sandbox_related"])
        self.assertEqual(result["sandbox_reason"], "access_denied")

    def test_access_denied_output(self):
        out = "Access is denied.\n"
        result = _sandbox_failure_analysis("dir", 1, out, _plan())
        self.assertIsNotNone(result)
        self.assertEqual(result["sandbox_reason"], "access_denied")

    def test_permission_denied_output(self):
        out = "sh: 1: cannot create /root/x: Permission denied"
        result = _sandbox_failure_analysis("touch /root/x", 1, out, _plan())
        self.assertIsNotNone(result)
        self.assertEqual(result["sandbox_reason"], "access_denied")

    def test_spawn_failure_marker(self):
        out = "⚠️ Sandboxed command could not be started: something broke"
        result = _sandbox_failure_analysis("whoami", -1, out, _plan())
        self.assertIsNotNone(result)
        self.assertEqual(result["sandbox_reason"], "spawn_failure")

    def test_network_blocked(self):
        plan = _plan(network=False)
        result = _sandbox_failure_analysis(
            "curl https://example.com",
            7,
            "curl: (7) Failed to connect ... Network is unreachable",
            plan,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["sandbox_reason"], "network_blocked")

    def test_network_command_but_network_allowed_not_marked(self):
        plan = _plan(network=True)
        result = _sandbox_failure_analysis(
            "curl https://example.com",
            7,
            "curl: (7) Failed to connect ... Network is unreachable",
            plan,
        )
        self.assertIsNone(result)

    def test_non_network_command_not_marked_network(self):
        plan = _plan(network=False)
        result = _sandbox_failure_analysis(
            "python tools/a.py",
            1,
            "network is unreachable",
            plan,
        )
        self.assertIsNone(result)

    def test_unrelated_failure_returns_none(self):
        out = "SyntaxError: invalid syntax"
        result = _sandbox_failure_analysis("python -c bad", 1, out, _plan())
        self.assertIsNone(result)

    def test_hint_mentions_bypass_and_reflection(self):
        hint = _sandbox_escalation_hint("access_denied", _plan())
        self.assertIn("bypass_sandbox", hint)
        self.assertIn("reflect", hint)
        network_hint = _sandbox_escalation_hint("network_blocked", _plan())
        self.assertIn("network", network_hint)


class SandboxEscalationApprovalTests(unittest.TestCase):
    def test_approved_returns_none_result(self):
        agent = _EscalationAgent(confirm_result=True)
        approval, result = _apply_sandbox_escalation_approval(
            agent, "git push", _plan()
        )
        self.assertEqual(approval, "approved")
        self.assertIsNone(result)
        self.assertEqual(agent.prompt_calls[0]["offer_always"], False)
        self.assertEqual(agent.prompt_calls[0]["kind"], "shell")

    def test_plain_reject_ends_task(self):
        agent = _EscalationAgent(confirm_result=False, supplement="")
        approval, result = _apply_sandbox_escalation_approval(
            agent, "git push", _plan()
        )
        self.assertEqual(approval, "rejected")
        self.assertIsNotNone(result)
        self.assertTrue(result["user_cancelled"])
        self.assertTrue(result["sandbox_escalation_rejected"])
        self.assertFalse(result["success"])

    def test_reject_with_supplement_keeps_task_running(self):
        agent = _EscalationAgent(
            confirm_result=False, supplement="Use a local mirror instead"
        )
        approval, result = _apply_sandbox_escalation_approval(
            agent, "git push", _plan()
        )
        self.assertEqual(approval, "rejected_with_supplement")
        self.assertIsNotNone(result)
        self.assertTrue(result["user_rejected_with_supplement"])
        self.assertEqual(result["user_supplement"], "Use a local mirror instead")
        self.assertNotIn("user_cancelled", result)


if __name__ == "__main__":
    unittest.main()
