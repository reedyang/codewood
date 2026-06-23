import sys
import types
import unittest


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from cli.agent import Agent, ASK_MORE_INFO_ANSWER_HISTORY_PREFIX


class AskMoreInfoAnswerHistoryTests(unittest.TestCase):
    """The recorded request_user_input selection is a marked assistant message so
    both the TUI transcript and the GUI render it as a left-side bubble while
    keeping it out of the model context (the model already gets it via the
    continuation prompt)."""

    def setUp(self):
        self.agent = Agent.__new__(Agent)
        self.agent.conversation_history = []

    def test_build_and_parse_round_trips(self):
        content = self.agent._build_request_user_input_answer_history_content("Prod")
        self.assertTrue(content.startswith(ASK_MORE_INFO_ANSWER_HISTORY_PREFIX))
        self.assertEqual(
            self.agent._parse_request_user_input_answer_history_content(content), "Prod"
        )

    def test_parse_returns_none_for_unrelated_content(self):
        self.assertIsNone(
            self.agent._parse_request_user_input_answer_history_content("just some text")
        )
        self.assertIsNone(
            self.agent._parse_request_user_input_answer_history_content("")
        )

    def test_record_appends_excluded_assistant_message(self):
        self.agent._record_request_user_input_answer_history("A; C")
        self.assertEqual(len(self.agent.conversation_history), 1)
        msg = self.agent.conversation_history[0]
        # Left-side bubble => assistant role; never fed back to the model.
        self.assertEqual(msg["role"], "assistant")
        self.assertTrue(msg["exclude_from_model_context"])
        self.assertEqual(
            self.agent._parse_request_user_input_answer_history_content(msg["content"]),
            "A; C",
        )

    def test_record_ignores_empty_answer(self):
        self.agent._record_request_user_input_answer_history("   ")
        self.assertEqual(self.agent.conversation_history, [])


if __name__ == "__main__":
    unittest.main()
