import unittest

from src.smart_shell_agent import SmartShellAgent


class StatusBarTokenUsageTests(unittest.TestCase):
    def _make_agent(self) -> SmartShellAgent:
        agent = SmartShellAgent.__new__(SmartShellAgent)
        agent.model_name = "gpt-4o-mini"
        agent.workspace_name = "Default"
        agent.active_chat_name = "Demo Chat"
        return agent

    def test_status_bar_includes_chat_usage_percent(self):
        agent = self._make_agent()
        agent._last_context_usage_percent = 37
        frags, plain = agent._status_bar_render_data()
        self.assertIn("(37%)", plain)
        self.assertEqual(frags[-1][0], "fg:ansibrightblack")
        self.assertEqual(frags[-1][1], "(37%)")

    def test_status_usage_percent_is_clamped(self):
        agent = self._make_agent()
        agent._last_context_usage_percent = 12345
        _frags, plain = agent._status_bar_render_data()
        self.assertIn("(999%)", plain)

    def test_refresh_without_service_keeps_cached_usage(self):
        agent = self._make_agent()
        agent.conversation_history = [
            {"role": "user", "content": "hello " * 120},
            {"role": "assistant", "content": "world " * 120},
        ]
        agent.params = {"context_window": 2000}
        agent.session_memory_service = None
        agent._last_context_usage_percent = 42
        agent._refresh_status_context_usage_snapshot()
        self.assertEqual(int(getattr(agent, "_last_context_usage_percent", 0)), 42)


if __name__ == "__main__":
    unittest.main()
