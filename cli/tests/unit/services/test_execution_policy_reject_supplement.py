"""Tests for the "reject & supplement info" confirm-gate action.

Covers:
  * ``_confirm_choice_via_selection`` TUI free-text row -> ``n_supplement``
    with the typed text stored on the agent;
  * GUI provider ``n_supplement`` passthrough;
  * the plain y/n/r text fallback;
  * the reject-with-supplement tool result built by the agent
    (``_confirm_declined_result``) and its effect on cancellation detection.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cli.services.execution_policy_service as eps
from cli.agent import Agent


class _FakeSelector:
    """Stand-in for ``input_handler.prompt_request_user_input_selection``."""

    def __init__(self, picked):
        self._picked = picked
        self.calls = []

    def prompt_request_user_input_selection(self, question, options, multi_select, **kwargs):
        self.calls.append((question, list(options), multi_select, kwargs))
        return self._picked


class _FakeTTY:
    def isatty(self):
        return True


class ConfirmChoiceViaSelectionTests(unittest.TestCase):
    def _agent(self, selector=None):
        return SimpleNamespace(
            input_handler=selector,
            _confirm_choice_provider=None,
        )

    def test_tui_freeform_returns_n_supplement_and_stores_text(self):
        selector = _FakeSelector("请先切换到 dev 分支再重试")
        agent = self._agent(selector)
        with patch.object(eps.sys, "stdin", _FakeTTY()), patch.object(
            eps.sys, "stdout", _FakeTTY()
        ):
            result = eps._confirm_choice_via_selection(
                agent, "Confirm?", offer_always=False
            )
        self.assertEqual(result, "n_supplement")
        self.assertEqual(eps.get_confirm_supplement(agent), "请先切换到 dev 分支再重试")
        # The selector must be offered the trailing free-text row.
        self.assertTrue(selector.calls[0][3].get("allow_other", False))

    def test_tui_fixed_option_still_maps_locally(self):
        selector = _FakeSelector("Yes, execute")
        agent = self._agent(selector)
        with patch.object(eps.sys, "stdin", _FakeTTY()), patch.object(
            eps.sys, "stdout", _FakeTTY()
        ):
            result = eps._confirm_choice_via_selection(
                agent, "Confirm?", offer_always=False
            )
        self.assertEqual(result, "y")
        self.assertEqual(eps.get_confirm_supplement(agent), "")

    def test_tui_empty_freeform_falls_back_to_plain_no(self):
        selector = _FakeSelector("   ")
        agent = self._agent(selector)
        with patch.object(eps.sys, "stdin", _FakeTTY()), patch.object(
            eps.sys, "stdout", _FakeTTY()
        ):
            result = eps._confirm_choice_via_selection(
                agent, "Confirm?", offer_always=False
            )
        self.assertEqual(result, "n")
        self.assertEqual(eps.get_confirm_supplement(agent), "")

    def test_gui_n_supplement_passthrough(self):
        agent = self._agent()
        agent._confirm_choice_provider = lambda *a, **k: "n_supplement"
        result = eps._confirm_choice_via_selection(agent, "Confirm?", offer_always=False)
        self.assertEqual(result, "n_supplement")

    def test_gui_plain_no_unaffected(self):
        agent = self._agent()
        agent._confirm_choice_provider = lambda *a, **k: "n"
        result = eps._confirm_choice_via_selection(agent, "Confirm?", offer_always=False)
        self.assertEqual(result, "n")


class PromptConfirmFallbackTests(unittest.TestCase):
    def test_plain_r_input_collects_supplement(self):
        answers = iter(["r", "请使用只读命令"])
        agent = SimpleNamespace(
            _suspended_input=lambda line: next(answers),
            display_language="en",
            input_handler=None,
            _confirm_choice_provider=None,
            _shell_command_in_allowlist=lambda *a: False,
            _load_confirm_allowlist=lambda: None,
            _confirm_allowlist_salt="",
        )
        ok = eps.prompt_confirm_yes_no_maybe_always(
            agent,
            "Confirm?",
            offer_always=False,
            kind="shell",
        )
        self.assertFalse(ok)
        self.assertEqual(eps.get_confirm_supplement(agent), "请使用只读命令")

    def test_plain_y_still_proceeds(self):
        agent = SimpleNamespace(
            _suspended_input=lambda line: "y",
            display_language="en",
            input_handler=None,
            _confirm_choice_provider=None,
            _shell_command_in_allowlist=lambda *a: False,
            _load_confirm_allowlist=lambda: None,
            _confirm_allowlist_salt="",
        )
        ok = eps.prompt_confirm_yes_no_maybe_always(
            agent,
            "Confirm?",
            offer_always=False,
            kind="shell",
        )
        self.assertTrue(ok)
        self.assertEqual(eps.get_confirm_supplement(agent), "")


class AgentResultTests(unittest.TestCase):
    def test_confirm_declined_result_carries_supplement(self):
        agent = SimpleNamespace(_confirm_supplement_text="改用 http 而非 https")
        result = Agent._confirm_declined_result(agent, "Operation cancelled by user")
        self.assertFalse(result["success"])
        self.assertTrue(result["user_rejected_with_supplement"])
        self.assertEqual(result["user_supplement"], "改用 http 而非 https")
        self.assertFalse(result["retryable"])
        # Consumed in one shot: a second call yields a plain cancellation.
        second = Agent._confirm_declined_result(agent, "Operation cancelled by user")
        self.assertNotIn("user_supplement", second)
        self.assertNotIn("user_rejected_with_supplement", second)

    def test_result_indicates_user_cancelled_false_for_reject_supplement(self):
        agent = SimpleNamespace()
        result = {
            "success": False,
            "error": "Operation cancelled by user",
            "user_rejected_with_supplement": True,
            "user_supplement": "补充说明",
        }
        self.assertFalse(Agent._result_indicates_user_cancelled(agent, result))

    def test_result_indicates_user_cancelled_still_true_for_plain_cancel(self):
        agent = SimpleNamespace()
        result = {"success": False, "error": "Operation cancelled by user"}
        self.assertTrue(Agent._result_indicates_user_cancelled(agent, result))


if __name__ == "__main__":
    unittest.main()
