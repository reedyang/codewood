import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from src.completion.builtin_slash_commands import slash_builtin_completions
from src.controllers.chat_command_controller import handle_chat_builtin_command


class _FakeChatAgent:
    def __init__(self):
        self.active_chat_id = "chat-2"
        self.active_chat_name = "Demo Chat"
        self.load_chat_state_calls = 0
        self.activate_calls = []
        self.activate_result = ""

    def _load_chat_state(self):
        self.load_chat_state_calls += 1

    def _activate_chat(
        self,
        chat_id: str,
        announce: bool = True,
        clear_screen: bool = False,
        print_history: bool = False,
    ):
        self.activate_calls.append(
            {
                "chat_id": chat_id,
                "announce": announce,
                "clear_screen": clear_screen,
                "print_history": print_history,
            }
        )
        return self.activate_result


class ChatCommandControllerTests(unittest.TestCase):
    def test_non_chat_command_not_handled(self):
        agent = _FakeChatAgent()
        self.assertFalse(handle_chat_builtin_command(agent, "workspace list"))

    def test_reload_reloads_current_chat_history(self):
        agent = _FakeChatAgent()
        buf = io.StringIO()
        with (
            patch("src.controllers.chat_command_controller._clear_terminal_screen") as mock_clear,
            patch("src.controllers.chat_command_controller._print_startup_overview_safe") as mock_startup,
            redirect_stdout(buf),
        ):
            handled = handle_chat_builtin_command(agent, "chat reload")
        self.assertTrue(handled)
        mock_clear.assert_called_once_with()
        mock_startup.assert_called_once_with(agent)
        self.assertEqual(agent.load_chat_state_calls, 1)
        self.assertEqual(
            agent.activate_calls,
            [
                {
                    "chat_id": "chat-2",
                    "announce": False,
                    "clear_screen": False,
                    "print_history": True,
                }
            ],
        )
        self.assertEqual(buf.getvalue(), "")

    def test_reload_prints_activate_error(self):
        agent = _FakeChatAgent()
        agent.activate_result = "❌ 未找到 chat: chat-2"
        buf = io.StringIO()
        with (
            patch("src.controllers.chat_command_controller._clear_terminal_screen"),
            patch("src.controllers.chat_command_controller._print_startup_overview_safe"),
            redirect_stdout(buf),
        ):
            handled = handle_chat_builtin_command(agent, "chat reload")
        self.assertTrue(handled)
        self.assertEqual(agent.load_chat_state_calls, 1)
        self.assertIn("❌ 未找到 chat: chat-2", buf.getvalue())

    def test_reload_is_in_slash_completions(self):
        out = slash_builtin_completions("/chat re")
        self.assertIn("/chat reload", out)


if __name__ == "__main__":
    unittest.main()
