import sys
import types
import unittest


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from cli.agent import Agent


class TaskControlToolTests(unittest.TestCase):
    def setUp(self):
        # Bypass heavy init; these tool branches are pure.
        self.agent = Agent.__new__(Agent)
        self.agent.skills = []

    def test_ask_more_info_returns_need_user_input_payload(self):
        result = self.agent.execute_tool_call(
            "ask_more_info",
            {
                "question": "Which environment?",
                "options": ["Production", "Staging"],
            },
        )
        self.assertTrue(result.get("success"))
        self.assertTrue(result.get("needs_user_input"))
        self.assertEqual(result.get("input_type"), "supplement")
        self.assertEqual(result.get("question"), "Which environment?")
        self.assertEqual(result.get("options"), ["Production", "Staging"])
        # ``multi_select`` defaults to False; the host must always see
        # it explicitly so the GUI doesn't have to guess.
        self.assertEqual(result.get("multi_select"), False)

    def test_ask_more_info_supports_multi_select_flag(self):
        # Multi-select is opt-in via ``multi_select: true``. Stringy
        # variants ("true"/"yes") are also accepted so a loose JSON
        # client doesn't accidentally fall back to single-select.
        for raw_flag, expected in (
            (True, True),
            (False, False),
            ("true", True),
            ("yes", True),
            ("false", False),
            ("", False),
            (1, True),
            (0, False),
        ):
            with self.subTest(raw_flag=raw_flag):
                result = self.agent.execute_tool_call(
                    "ask_more_info",
                    {
                        "question": "Pick targets",
                        "options": ["A", "B", "C"],
                        "multi_select": raw_flag,
                    },
                )
                self.assertTrue(result.get("success"))
                self.assertEqual(result.get("multi_select"), expected)

    def test_ask_more_info_rejects_fewer_than_two_options(self):
        # The new contract: model MUST provide at least two discrete
        # choices so the host can render real option buttons. A
        # missing/short ``options`` array is a retryable error so the
        # model can re-issue the call without ending the turn.
        for params in (
            {"question": "Pick a colour"},
            {"question": "Pick a colour", "options": []},
            {"question": "Pick a colour", "options": ["Only"]},
        ):
            with self.subTest(params=params):
                result = self.agent.execute_tool_call("ask_more_info", params)
                self.assertFalse(result.get("success", True))
                self.assertTrue(result.get("retryable"))
                self.assertIn("options", str(result.get("error") or ""))

    def test_ask_more_info_dedupes_and_caps_options(self):
        # Duplicates collapse in arrival order; the cap (16) protects
        # the UI from a runaway list of choices. Empty/whitespace
        # entries are dropped silently.
        many = [f"opt-{i}" for i in range(40)]
        params = {
            "question": "Pick one",
            "options": ["A", " ", "A", "B", "", "C", *many],
        }
        result = self.agent.execute_tool_call("ask_more_info", params)
        self.assertTrue(result.get("success"))
        opts = result.get("options")
        self.assertIsInstance(opts, list)
        self.assertEqual(opts[:3], ["A", "B", "C"])
        self.assertLessEqual(len(opts), 16)
        self.assertEqual(len(opts), len(set(opts)))

    def test_task_changed_tool_is_no_longer_registered(self):
        result = self.agent.execute_tool_call(
            "task_changed",
            {"new_task": "Generate release notes", "reason": "user changed request"},
        )
        self.assertFalse(result.get("success", True))

    def test_done_tool_is_no_longer_registered(self):
        result = self.agent.execute_tool_call("done", {})
        self.assertFalse(result.get("success", True))

    def test_cancel_detection_does_not_scan_success_output_text(self):
        result = {
            "success": True,
            "output": "This is file content and contains the keyword: user cancelled, but this is not a cancellation.",
        }
        self.assertFalse(self.agent._result_indicates_user_cancelled(result))

    def test_cancel_detection_uses_error_message_on_failure(self):
        result = {"success": False, "error": "The operation was cancelled by the user"}
        self.assertTrue(self.agent._result_indicates_user_cancelled(result))


if __name__ == "__main__":
    unittest.main()
