import io
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

if "ollama" not in sys.modules:
    sys.modules["ollama"] = types.SimpleNamespace(list=lambda: {"models": []})

from src.agent import (
    DIRECT_SHELL_USER_HISTORY_PREFIX,
    INTERNAL_SLASH_USER_HISTORY_PREFIX,
)
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
        self.tail_anchor_calls = 0
        self.remembered_first_visible_indexes = []

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

    def _remember_active_chat_history_tail_anchor(self):
        self.tail_anchor_calls += 1
        return 4

    def _remember_active_chat_history_first_visible_index(self, index):
        self.remembered_first_visible_indexes.append(index)


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeInputHandler:
    def __init__(self):
        self.prefilled_text = None
        self.prefilled_cursor = None

    def set_pending_prefill(self, text, cursor_position=None):
        self.prefilled_text = text
        self.prefilled_cursor = cursor_position


class _FakeEditAgent:
    def __init__(self, conversation_history):
        self.active_chat_id = "chat-1"
        self.active_chat_name = "Demo"
        self._chat_state_lock = _NoopLock()
        self.conversation_history = list(conversation_history)
        self.sync_calls = 0
        self.input_handler = _FakeInputHandler()

    def _sync_active_chat_messages(self):
        self.sync_calls += 1


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
        self.assertEqual(agent.remembered_first_visible_indexes, [0])
        self.assertEqual(agent.tail_anchor_calls, 0)

    def test_reload_prints_activate_error(self):
        agent = _FakeChatAgent()
        agent.activate_result = "❌ chat not found: chat-2"
        buf = io.StringIO()
        with (
            patch("src.controllers.chat_command_controller._clear_terminal_screen"),
            patch("src.controllers.chat_command_controller._print_startup_overview_safe"),
            redirect_stdout(buf),
        ):
            handled = handle_chat_builtin_command(agent, "chat reload")
        self.assertTrue(handled)
        self.assertEqual(agent.load_chat_state_calls, 1)
        self.assertIn("❌ chat not found: chat-2", buf.getvalue())

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


class _FakeForkAgent:
    def __init__(self, messages, name="Demo", extra_chats=None):
        self.active_chat_id = "chat-1"
        self.active_chat_name = name
        self._chat_state_lock = _NoopLock()
        self.conversation_history = list(messages)
        chats = [
            {
                "id": "chat-1",
                "name": name,
                "name_source": "manual",
                "model_provider": "openai",
                "model_name": "gpt",
                "messages": list(messages),
            }
        ]
        if extra_chats:
            chats.extend(extra_chats)
        self._chat_state = {"version": 1, "active": "chat-1", "chats": chats}
        self.saved = 0
        self.input_handler = _FakeInputHandler()

    def _sync_active_chat_messages(self):
        chat = self._find_chat_by_id(self.active_chat_id)
        if chat is not None:
            chat["messages"] = list(self.conversation_history)

    def _find_chat_by_id(self, cid):
        for c in self._chat_state["chats"]:
            if c.get("id") == cid:
                return c
        return None

    def _chat_entries(self):
        return self._chat_state["chats"]

    def _next_chat_id(self):
        existing = {c.get("id") for c in self._chat_state["chats"]}
        i = 1
        while f"chat-{i}" in existing:
            i += 1
        return f"chat-{i}"

    def _new_chat_entry(self, cid, name="New Chat"):
        return {
            "id": cid,
            "name": name,
            "name_source": "default",
            "model_provider": "",
            "model_name": "",
            "messages": [],
        }

    def _save_chat_state(self):
        self.saved += 1


class ChatForkCommandTests(unittest.TestCase):
    def _history(self):
        return [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "assistant", "content": "a1b"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "q3"},
            {"role": "assistant", "content": "a3"},
        ]

    def _new_chat(self, agent):
        return agent._find_chat_by_id(agent._chat_state["active"])

    def test_fork_default_copies_all_and_switches(self):
        agent = _FakeForkAgent(self._history())
        buf = io.StringIO()
        with (
            patch("src.controllers.chat_command_controller._reload_chat_from_top") as mock_reload,
            redirect_stdout(buf),
        ):
            handled = handle_chat_builtin_command(agent, "chat fork")
        self.assertTrue(handled)
        self.assertEqual(agent._chat_state["active"], "chat-2")
        new_chat = self._new_chat(agent)
        self.assertEqual(new_chat["name"], "Demo (2)")
        self.assertEqual(
            [m["content"] for m in new_chat["messages"]],
            ["q1", "a1", "a1b", "q2", "a2", "q3", "a3"],
        )
        self.assertEqual(new_chat["model_provider"], "openai")
        self.assertEqual(new_chat["model_name"], "gpt")
        self.assertEqual(new_chat["name_source"], "manual")
        mock_reload.assert_called_once_with(agent, "chat-2")

    def test_fork_minus_one_equivalent_to_default(self):
        agent = _FakeForkAgent(self._history())
        with (
            patch("src.controllers.chat_command_controller._reload_chat_from_top"),
            redirect_stdout(io.StringIO()),
        ):
            handle_chat_builtin_command(agent, "chat fork -1")
        new_chat = self._new_chat(agent)
        self.assertEqual(
            [m["content"] for m in new_chat["messages"]],
            ["q1", "a1", "a1b", "q2", "a2", "q3", "a3"],
        )

    def test_fork_positive_index_copies_single_turn(self):
        agent = _FakeForkAgent(self._history())
        with (
            patch("src.controllers.chat_command_controller._reload_chat_from_top"),
            redirect_stdout(io.StringIO()),
        ):
            handle_chat_builtin_command(agent, "chat fork 1")
        new_chat = self._new_chat(agent)
        # Copies the first user turn plus its triggered assistant messages.
        self.assertEqual(
            [m["content"] for m in new_chat["messages"]],
            ["q1", "a1", "a1b"],
        )

    def test_fork_second_index_copies_two_turns(self):
        agent = _FakeForkAgent(self._history())
        with (
            patch("src.controllers.chat_command_controller._reload_chat_from_top"),
            redirect_stdout(io.StringIO()),
        ):
            handle_chat_builtin_command(agent, "chat fork 2")
        new_chat = self._new_chat(agent)
        self.assertEqual(
            [m["content"] for m in new_chat["messages"]],
            ["q1", "a1", "a1b", "q2", "a2"],
        )

    def test_fork_copied_messages_are_independent(self):
        agent = _FakeForkAgent(self._history())
        with (
            patch("src.controllers.chat_command_controller._reload_chat_from_top"),
            redirect_stdout(io.StringIO()),
        ):
            handle_chat_builtin_command(agent, "chat fork 1")
        new_chat = self._new_chat(agent)
        new_chat["messages"][0]["content"] = "mutated"
        source = agent._find_chat_by_id("chat-1")
        self.assertEqual(source["messages"][0]["content"], "q1")

    def test_fork_name_skips_existing(self):
        extra = [{"id": "other", "name": "Demo (2)", "messages": []}]
        agent = _FakeForkAgent(self._history(), extra_chats=extra)
        with (
            patch("src.controllers.chat_command_controller._reload_chat_from_top"),
            redirect_stdout(io.StringIO()),
        ):
            handle_chat_builtin_command(agent, "chat fork")
        new_chat = self._new_chat(agent)
        self.assertEqual(new_chat["name"], "Demo (3)")

    def test_fork_out_of_range(self):
        agent = _FakeForkAgent(self._history())
        buf = io.StringIO()
        with (
            patch("src.controllers.chat_command_controller._reload_chat_from_top") as mock_reload,
            redirect_stdout(buf),
        ):
            handle_chat_builtin_command(agent, "chat fork 4")
        self.assertIn("❌", buf.getvalue())
        self.assertEqual(len(agent._chat_state["chats"]), 1)
        mock_reload.assert_not_called()

    def test_fork_invalid_index(self):
        agent = _FakeForkAgent(self._history())
        buf = io.StringIO()
        with redirect_stdout(buf):
            handle_chat_builtin_command(agent, "chat fork abc")
        self.assertIn("❌", buf.getvalue())
        self.assertEqual(len(agent._chat_state["chats"]), 1)

    def test_fork_zero_index_invalid(self):
        agent = _FakeForkAgent(self._history())
        buf = io.StringIO()
        with redirect_stdout(buf):
            handle_chat_builtin_command(agent, "chat fork 0")
        self.assertIn("❌", buf.getvalue())
        self.assertEqual(len(agent._chat_state["chats"]), 1)

    def test_fork_too_many_args_shows_usage(self):
        agent = _FakeForkAgent(self._history())
        buf = io.StringIO()
        with redirect_stdout(buf):
            handle_chat_builtin_command(agent, "chat fork 1 2")
        self.assertIn("/chat fork", buf.getvalue())
        self.assertEqual(len(agent._chat_state["chats"]), 1)

    def test_fork_no_user_messages(self):
        agent = _FakeForkAgent([{"role": "assistant", "content": "note"}])
        buf = io.StringIO()
        with redirect_stdout(buf):
            handle_chat_builtin_command(agent, "chat fork")
        self.assertIn("❌", buf.getvalue())
        self.assertEqual(len(agent._chat_state["chats"]), 1)

    def test_fork_increments_existing_numeric_suffix(self):
        agent = _FakeForkAgent(self._history(), name="Demo (2)")
        with (
            patch("src.controllers.chat_command_controller._reload_chat_from_top"),
            redirect_stdout(io.StringIO()),
        ):
            handle_chat_builtin_command(agent, "chat fork")
        new_chat = self._new_chat(agent)
        self.assertEqual(new_chat["name"], "Demo (3)")

    def test_fork_increments_suffix_skipping_existing(self):
        extra = [{"id": "other", "name": "Demo (3)", "messages": []}]
        agent = _FakeForkAgent(self._history(), name="Demo (2)", extra_chats=extra)
        with (
            patch("src.controllers.chat_command_controller._reload_chat_from_top"),
            redirect_stdout(io.StringIO()),
        ):
            handle_chat_builtin_command(agent, "chat fork")
        new_chat = self._new_chat(agent)
        self.assertEqual(new_chat["name"], "Demo (4)")

    def test_fork_is_in_slash_completions(self):
        out = slash_builtin_completions("/chat fo")
        self.assertIn("/chat fork", out)
        self.assertNotIn("/chat fork ", out)


class ChatEditCommandTests(unittest.TestCase):
    def _history(self):
        return [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second question"},
            {"role": "assistant", "content": "second answer"},
            {"role": "user", "content": "third question"},
            {"role": "assistant", "content": "third answer"},
        ]

    def test_edit_positive_index_truncates_and_prefills(self):
        agent = _FakeEditAgent(self._history())
        buf = io.StringIO()
        with (
            patch("src.controllers.chat_command_controller._reload_chat_from_top") as mock_reload,
            redirect_stdout(buf),
        ):
            handled = handle_chat_builtin_command(agent, "chat edit 2")
        self.assertTrue(handled)
        # Message at user-index 2 (history index 2) and everything after removed.
        self.assertEqual(
            [m["content"] for m in agent.conversation_history],
            ["first question", "first answer"],
        )
        self.assertEqual(agent.input_handler.prefilled_text, "second question")
        self.assertEqual(agent.sync_calls, 1)
        mock_reload.assert_called_once_with(agent, "chat-1")

    def test_edit_negative_index_targets_last_user_message(self):
        agent = _FakeEditAgent(self._history())
        with (
            patch("src.controllers.chat_command_controller._reload_chat_from_top"),
            redirect_stdout(io.StringIO()),
        ):
            handle_chat_builtin_command(agent, "chat edit -1")
        self.assertEqual(
            [m["content"] for m in agent.conversation_history],
            ["first question", "first answer", "second question", "second answer"],
        )
        self.assertEqual(agent.input_handler.prefilled_text, "third question")

    def test_edit_negative_index_full_span(self):
        agent = _FakeEditAgent(self._history())
        with (
            patch("src.controllers.chat_command_controller._reload_chat_from_top"),
            redirect_stdout(io.StringIO()),
        ):
            handle_chat_builtin_command(agent, "chat edit -3")
        self.assertEqual(agent.conversation_history, [])
        self.assertEqual(agent.input_handler.prefilled_text, "first question")

    def test_edit_skips_internal_user_messages_when_counting(self):
        history = [
            {"role": "user", "content": "real one"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": f"{DIRECT_SHELL_USER_HISTORY_PREFIX}ls -la"},
            {"role": "assistant", "content": "[DIRECT_SHELL_RESULT]{}"},
            {"role": "user", "content": f"{INTERNAL_SLASH_USER_HISTORY_PREFIX}/help"},
            {"role": "assistant", "content": "[INTERNAL_SLASH_RESULT]{}"},
            {"role": "user", "content": "real two"},
            {"role": "assistant", "content": "answer two"},
        ]
        agent = _FakeEditAgent(history)
        with (
            patch("src.controllers.chat_command_controller._reload_chat_from_top"),
            redirect_stdout(io.StringIO()),
        ):
            handle_chat_builtin_command(agent, "chat edit -1")
        # The last genuine user message is "real two" at history index 6.
        self.assertEqual(agent.input_handler.prefilled_text, "real two")
        self.assertEqual(len(agent.conversation_history), 6)

    def test_edit_out_of_range_positive(self):
        agent = _FakeEditAgent(self._history())
        buf = io.StringIO()
        with (
            patch("src.controllers.chat_command_controller._reload_chat_from_top") as mock_reload,
            redirect_stdout(buf),
        ):
            handle_chat_builtin_command(agent, "chat edit 4")
        self.assertIn("❌", buf.getvalue())
        self.assertEqual(len(agent.conversation_history), 6)
        self.assertIsNone(agent.input_handler.prefilled_text)
        mock_reload.assert_not_called()

    def test_edit_out_of_range_negative(self):
        agent = _FakeEditAgent(self._history())
        buf = io.StringIO()
        with (
            patch("src.controllers.chat_command_controller._reload_chat_from_top"),
            redirect_stdout(buf),
        ):
            handle_chat_builtin_command(agent, "chat edit -4")
        self.assertIn("❌", buf.getvalue())
        self.assertEqual(len(agent.conversation_history), 6)

    def test_edit_invalid_index(self):
        agent = _FakeEditAgent(self._history())
        buf = io.StringIO()
        with redirect_stdout(buf):
            handle_chat_builtin_command(agent, "chat edit abc")
        self.assertIn("❌", buf.getvalue())
        self.assertEqual(len(agent.conversation_history), 6)

    def test_edit_zero_index_invalid(self):
        agent = _FakeEditAgent(self._history())
        buf = io.StringIO()
        with redirect_stdout(buf):
            handle_chat_builtin_command(agent, "chat edit 0")
        self.assertIn("❌", buf.getvalue())
        self.assertEqual(len(agent.conversation_history), 6)

    def test_edit_missing_index_shows_usage(self):
        agent = _FakeEditAgent(self._history())
        buf = io.StringIO()
        with redirect_stdout(buf):
            handle_chat_builtin_command(agent, "chat edit")
        self.assertIn("/chat edit", buf.getvalue())
        self.assertEqual(len(agent.conversation_history), 6)

    def test_edit_no_user_messages(self):
        agent = _FakeEditAgent(
            [{"role": "assistant", "content": "system note"}]
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            handle_chat_builtin_command(agent, "chat edit 1")
        self.assertIn("❌", buf.getvalue())

    def test_edit_is_in_slash_completions(self):
        out = slash_builtin_completions("/chat ed")
        self.assertIn("/chat edit ", out)


if __name__ == "__main__":
    unittest.main()
