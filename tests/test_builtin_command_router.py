import unittest
from unittest.mock import patch

from src.controllers.builtin_command_router import dispatch_builtin_command


class _FakeHistoryManager:
    def clear_history(self):
        return None

    def get_all_history(self):
        return []


class _FakeAgent:
    def __init__(self):
        self.clear_context_calls = 0
        self.chat_builtin_calls = []
        self.raise_on_chat_builtin = False
        self.input_handler = None
        self.history_manager = _FakeHistoryManager()
        self.memory_enabled = True

    def _parse_mcp_shortcut_command(self, _builtin_line):
        return None, {}, "usage"

    def _save_current_workspace_position(self):
        return None

    def _clear_active_chat_context_and_tasks(self):
        self.clear_context_calls += 1

    def _handle_model_builtin_command(self, _builtin_line):
        return False

    def _handle_chat_builtin_command(self, builtin_line):
        if self.raise_on_chat_builtin:
            raise RuntimeError("boom")
        self.chat_builtin_calls.append(str(builtin_line or ""))
        return True

    def _handle_workspace_builtin_command(self, _builtin_line):
        return False

    def execute_tool_call(self, _tool_name, _args):
        return {}


class BuiltinCommandRouterTests(unittest.TestCase):
    def test_clear_context_auto_triggers_chat_reload(self):
        agent = _FakeAgent()
        with patch("builtins.print") as mock_print:
            handled, should_exit = dispatch_builtin_command(
                agent,
                "clear context",
                os_name="nt",
            )
        self.assertTrue(handled)
        self.assertFalse(should_exit)
        self.assertEqual(agent.clear_context_calls, 1)
        self.assertEqual(agent.chat_builtin_calls, ["chat reload"])
        mock_print.assert_any_call("AI context and recorded tasks cleared.")

    def test_clear_context_still_succeeds_when_reload_hook_raises(self):
        agent = _FakeAgent()
        agent.raise_on_chat_builtin = True
        handled, should_exit = dispatch_builtin_command(
            agent,
            "clear context",
            os_name="nt",
        )
        self.assertTrue(handled)
        self.assertFalse(should_exit)
        self.assertEqual(agent.clear_context_calls, 1)


if __name__ == "__main__":
    unittest.main()
