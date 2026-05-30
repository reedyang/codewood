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
        self._chat_state_lock = _NoopLock()
        self._chat_target = {"id": "chat-3", "name": "Other Chat"}
        self.print_chat_history_calls = []

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

    def _resolve_chat_selector(self, _selector: str):
        return self._chat_target

    def _print_chat_history(self, start_index=None):
        self.print_chat_history_calls.append(start_index)


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _PrefixingStream:
    def __init__(self, base_stream):
        self._base_stream = base_stream

    def write(self, text):
        s = str(text)
        if not s:
            return 0
        chunks = s.splitlines(keepends=True)
        prefixed = "".join(
            (f"  {chunk}" if chunk.strip() else chunk) for chunk in chunks
        )
        return self._base_stream.write(prefixed)

    def flush(self):
        return self._base_stream.flush()


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
                    "print_history": False,
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

    def test_switch_does_not_print_switched_to_chat_message(self):
        agent = _FakeChatAgent()
        buf = io.StringIO()
        with (
            patch("src.controllers.chat_command_controller._clear_terminal_screen") as mock_clear,
            patch("src.controllers.chat_command_controller._print_startup_overview_safe") as mock_startup,
            redirect_stdout(buf),
        ):
            handled = handle_chat_builtin_command(agent, "chat switch chat-3")
        self.assertTrue(handled)
        mock_clear.assert_called_once_with()
        mock_startup.assert_called_once_with(agent)
        self.assertEqual(agent.load_chat_state_calls, 1)
        self.assertEqual(
            agent.activate_calls,
            [
                {
                    "chat_id": "chat-3",
                    "announce": False,
                    "clear_screen": False,
                    "print_history": False,
                }
            ],
        )
        self.assertEqual(buf.getvalue(), "")

    def test_reload_with_resize_helper_uses_unwrapped_stdout(self):
        class _ReloadAgent(_FakeChatAgent):
            def _reload_chat_history_from_anchor_on_resize(self):
                print("› hello")
                print("• world")

        agent = _ReloadAgent()
        base_out = io.StringIO()
        wrapped_out = _PrefixingStream(base_out)
        with (
            patch("src.controllers.chat_command_controller.sys.stdout", wrapped_out),
            patch("src.controllers.chat_command_controller.sys.stderr", wrapped_out),
            patch("src.controllers.chat_command_controller._clear_terminal_screen") as mock_clear,
            patch("src.controllers.chat_command_controller._print_startup_overview_safe") as mock_startup,
        ):
            handled = handle_chat_builtin_command(agent, "chat reload")
        self.assertTrue(handled)
        self.assertEqual(agent.load_chat_state_calls, 1)
        self.assertEqual(base_out.getvalue(), "› hello\n• world\n")
        mock_clear.assert_not_called()
        mock_startup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
