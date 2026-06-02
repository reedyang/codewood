import re
import sys
import types
import unittest
from unittest.mock import patch


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.agent import Agent
from src.services.session_memory_service import SessionMemoryService


class _FakeHistoryManager:
    def reset_index(self):
        return None


class _FakeInputHandler:
    renders_prompt_separator_inline = False

    def get_input_with_completion(self, prompt, **kwargs):
        return "hello"


class _RecordingInputHandler(_FakeInputHandler):
    def __init__(self, events):
        self.events = events

    def get_input_with_completion(self, prompt, **kwargs):
        self.events.append("prompt")
        return "hello"


class _FakeInputHandlerWithColumns(_FakeInputHandler):
    def get_terminal_columns(self, default=80):
        return 6


class _FakeStdout:
    def __init__(self):
        self.writes = []

    def isatty(self):
        return True

    def write(self, text):
        self.writes.append(str(text))
        return len(str(text))

    def flush(self):
        return None


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class PromptSeparatorBehaviorTests(unittest.TestCase):
    def _build_agent(self) -> Agent:
        agent = Agent.__new__(Agent)
        agent._startup_prompt_pending = False
        agent._suppress_next_separator = False
        agent._show_separator_next_prompt = False
        agent._prompt_separator_rendered = False
        agent._chat_history_reload_last_terminal_width = 0
        agent._chat_history_first_visible_index_map = {}
        agent._force_reload_chat_history_from_anchor_once = False
        agent._suppress_next_prompt_chat_reload_once = False
        agent.active_chat_id = "chat-1"
        agent.workspace_id = "default"
        agent.conversation_history = []
        agent.active_chat_name = "Demo Chat"
        agent.input_handler = _FakeInputHandler()
        agent.history_manager = _FakeHistoryManager()
        agent._last_terminal_block_kind = ""
        agent._terminal_cursor_at_line_start = True
        return agent

    def test_separator_is_rendered_only_when_requested(self):
        agent = self._build_agent()
        agent._show_separator_next_prompt = True
        with (
            patch.object(agent, "_status_bar_render_data", return_value=([], "status")),
            patch.object(agent, "_print_prompt_separator") as mock_separator,
        ):
            out = agent._get_user_input_with_history()
        self.assertEqual(out, "hello")
        mock_separator.assert_called_once_with()
        self.assertFalse(agent._show_separator_next_prompt)

    def test_separator_is_not_rendered_by_default(self):
        agent = self._build_agent()
        with (
            patch.object(agent, "_status_bar_render_data", return_value=([], "status")),
            patch.object(agent, "_print_prompt_separator") as mock_separator,
        ):
            out = agent._get_user_input_with_history()
        self.assertEqual(out, "hello")
        mock_separator.assert_not_called()

    def test_pending_prompt_warning_prints_immediately_above_prompt(self):
        agent = self._build_agent()
        events = []
        agent.input_handler = _RecordingInputHandler(events)
        agent._set_pending_prompt_warning(
            "⚠️ Model context window is too small; only basic chat is supported."
        )

        def _capture_print(text="", *args, **kwargs):
            _ = (args, kwargs)
            events.append(str(text))

        with (
            patch.object(agent, "_status_bar_render_data", return_value=([], "status")),
            patch("src.agent._ansi_yellow", side_effect=lambda s: s),
            patch("builtins.print", side_effect=_capture_print),
        ):
            out = agent._get_user_input_with_history()

        self.assertEqual(out, "hello")
        self.assertEqual(
            events,
            [
                "⚠️ Model context window is too small; only basic chat is supported.",
                "",
                "prompt",
            ],
        )
        self.assertEqual(getattr(agent, "_pending_prompt_warning_line", ""), "")

    def test_chat_history_replays_context_compaction_notice_banner(self):
        agent = self._build_agent()
        agent.session_memory_service = SessionMemoryService(agent)
        summary = agent.session_memory_service.build_context_compaction_summary_content(
            summary="这段摘要只给模型上下文使用",
            mode="manual",
            covered_message_count=2,
        )
        notice = agent.session_memory_service.build_context_compaction_notice_content(
            "Context automatically compacted"
        )
        agent.conversation_history = [
            {"role": "user", "content": "reload 前的普通消息"},
            {"role": "assistant", "content": summary},
            {"role": "assistant", "content": notice},
            {"role": "assistant", "content": "reload 后的普通回复"},
        ]

        with (
            patch.object(agent.session_memory_service, "_print_compaction_banner") as mock_banner,
            patch("builtins.print") as mock_print,
        ):
            agent._print_chat_history()

        mock_banner.assert_called_once_with("Context automatically compacted")
        rendered = "\n".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
        self.assertIn("reload 前的普通消息", rendered)
        self.assertIn("reload 后的普通回复", rendered)
        self.assertNotIn("这段摘要只给模型上下文使用", rendered)

    def test_compact_slash_history_is_replayed_before_compacted_notice(self):
        agent = self._build_agent()
        agent.session_memory_service = SessionMemoryService(agent)
        summary = agent.session_memory_service.build_context_compaction_summary_content(
            summary="压缩摘要",
            mode="manual",
            covered_message_count=2,
        )
        notice = agent.session_memory_service.build_context_compaction_notice_content(
            "Context automatically compacted"
        )
        agent.conversation_history = [
            {"role": "user", "content": "旧消息"},
            {"role": "assistant", "content": summary},
            {"role": "assistant", "content": notice},
        ]

        agent._record_internal_slash_execution_history("/compact", "")

        roles_and_contents = [
            (msg.get("role"), str(msg.get("content") or ""))
            for msg in agent.conversation_history
        ]
        compact_idx = next(
            idx for idx, (_role, content) in enumerate(roles_and_contents) if "/compact" in content
        )
        notice_idx = next(
            idx
            for idx, (_role, content) in enumerate(roles_and_contents)
            if "CONTEXT_COMPACTION_NOTICE" in content
        )
        self.assertLess(compact_idx, notice_idx)

        events = []

        def fake_print(*args, **_kwargs):
            if args:
                events.append(str(args[0]))

        def fake_banner(text):
            events.append(str(text))

        with (
            patch.object(agent.session_memory_service, "_print_compaction_banner", side_effect=fake_banner),
            patch("builtins.print", side_effect=fake_print),
        ):
            agent._print_chat_history()

        compact_event_idx = next(idx for idx, text in enumerate(events) if "/compact" in text)
        compacted_event_idx = next(
            idx for idx, text in enumerate(events) if "Context automatically compacted" in text
        )
        self.assertLess(compact_event_idx, compacted_event_idx)

    def test_failed_compact_slash_history_is_not_moved_before_old_notice(self):
        agent = self._build_agent()
        agent.session_memory_service = SessionMemoryService(agent)
        summary = agent.session_memory_service.build_context_compaction_summary_content(
            summary="旧摘要",
            mode="manual",
            covered_message_count=2,
        )
        notice = agent.session_memory_service.build_context_compaction_notice_content(
            "Context automatically compacted"
        )
        agent.conversation_history = [
            {"role": "assistant", "content": summary},
            {"role": "assistant", "content": notice},
        ]

        agent._record_internal_slash_execution_history("/compact", "No context available to compact.\n")

        compact_idx = next(
            idx
            for idx, msg in enumerate(agent.conversation_history)
            if "/compact" in str(msg.get("content") or "")
        )
        notice_idx = next(
            idx
            for idx, msg in enumerate(agent.conversation_history)
            if "CONTEXT_COMPACTION_NOTICE" in str(msg.get("content") or "")
        )
        self.assertGreater(compact_idx, notice_idx)

    def test_separator_has_blank_line_above_and_below(self):
        agent = self._build_agent()
        agent.input_handler = _FakeInputHandlerWithColumns()
        fake_stdout = _FakeStdout()
        with (
            patch("src.agent._ansi_gray", side_effect=lambda s: s),
            patch("src.agent.sys.stdout", fake_stdout),
        ):
            agent._print_prompt_separator()
        out = "".join(fake_stdout.writes)
        self.assertEqual(out, "\n──────\n\n")

    def test_chat_history_direct_shell_output_always_prints_separator(self):
        agent = self._build_agent()
        agent.active_chat_name = "Demo Chat"
        agent.conversation_history = [
            {
                "role": "user",
                "content": agent._build_direct_shell_user_history_content("git status"),
            },
            {
                "role": "assistant",
                "content": agent._build_direct_shell_result_history_content(
                    "!git status",
                    "git status",
                    "D:/ws",
                    0,
                    "ok\n",
                    "",
                ),
            },
        ]
        with (
            patch("src.agent._ansi_gray", side_effect=lambda s: s),
            patch.object(agent, "_print_direct_shell_command_feedback"),
            patch.object(agent, "_print_direct_shell_history_output"),
            patch.object(agent, "_print_direct_shell_history_separator") as mock_sep,
            patch("builtins.print"),
        ):
            agent._print_chat_history()
        mock_sep.assert_called_once_with()
        self.assertFalse(agent._show_separator_next_prompt)

    def test_chat_history_multiple_direct_shell_outputs_each_get_separator(self):
        agent = self._build_agent()
        agent.active_chat_name = "Demo Chat"
        agent.conversation_history = [
            {
                "role": "user",
                "content": agent._build_direct_shell_user_history_content("git status"),
            },
            {
                "role": "assistant",
                "content": agent._build_direct_shell_result_history_content(
                    "!git status",
                    "git status",
                    "D:/ws",
                    0,
                    "ok\n",
                    "",
                ),
            },
            {"role": "user", "content": "普通消息"},
            {
                "role": "assistant",
                "content": agent._build_direct_shell_result_history_content(
                    "!echo hi",
                    "echo hi",
                    "D:/ws",
                    0,
                    "hi\n",
                    "",
                ),
            },
            {"role": "assistant", "content": "普通回复"},
        ]
        with (
            patch("src.agent._ansi_gray", side_effect=lambda s: s),
            patch.object(agent, "_print_direct_shell_command_feedback"),
            patch.object(agent, "_print_direct_shell_history_output"),
            patch.object(agent, "_print_direct_shell_history_separator") as mock_sep,
            patch("builtins.print"),
        ):
            agent._print_chat_history()
        self.assertEqual(mock_sep.call_count, 2)
        self.assertFalse(agent._show_separator_next_prompt)

    def test_chat_history_direct_shell_result_rebuilds_tail_from_full_output(self):
        agent = self._build_agent()
        agent.active_chat_name = "Demo Chat"
        agent.conversation_history = [
            {
                "role": "user",
                "content": agent._build_direct_shell_user_history_content("echo many"),
            },
            {
                "role": "assistant",
                "content": agent._build_direct_shell_result_history_content(
                    "!echo many",
                    "echo many",
                    "D:/ws",
                    0,
                    "line one\nline two\nline three\nline four\n",
                    "",
                ),
            },
        ]
        with (
            patch("src.agent.command_actions._dynamic_tail_line_limit", return_value=2),
            patch.object(agent, "_print_direct_shell_command_feedback"),
            patch.object(agent, "_print_direct_shell_history_output") as mock_shell_output,
            patch.object(agent, "_print_direct_shell_history_separator"),
            patch("builtins.print"),
        ):
            agent._print_chat_history()
        mock_shell_output.assert_called_once()
        replay_out, replay_err = mock_shell_output.call_args.args
        self.assertIn("omitted 3 lines", replay_out)
        self.assertIn("line four\n", replay_out)
        self.assertNotIn("line three\n", replay_out)
        self.assertNotIn("line one\n", replay_out)
        self.assertEqual(replay_err, "")

    def test_chat_history_aborted_direct_shell_output_does_not_print_separator(self):
        agent = self._build_agent()
        agent.active_chat_name = "Demo Chat"
        agent.conversation_history = [
            {
                "role": "user",
                "content": agent._build_direct_shell_user_history_content("sleep 10"),
            },
            {
                "role": "assistant",
                "content": agent._build_direct_shell_result_history_content(
                    "!sleep 10",
                    "sleep 10",
                    "D:/ws",
                    137,
                    "command aborted by user\n",
                    "",
                    aborted_by_user=True,
                ),
            },
        ]
        with (
            patch("src.agent._ansi_gray", side_effect=lambda s: s),
            patch.object(agent, "_print_direct_shell_command_feedback"),
            patch.object(agent, "_print_direct_shell_history_output"),
            patch.object(agent, "_print_direct_shell_history_separator") as mock_sep,
            patch.object(agent, "_print_conversation_interrupted_banner") as mock_banner,
            patch("builtins.print"),
        ):
            agent._print_chat_history()
        mock_sep.assert_not_called()
        mock_banner.assert_called_once_with()
        self.assertFalse(agent._show_separator_next_prompt)

    def test_chat_history_aborted_direct_shell_output_always_moves_abort_marker_to_final_tail(self):
        agent = self._build_agent()
        agent.active_chat_name = "Demo Chat"
        raw_stdout = "Version: Platform(x86/x64): Branch name: command aborted by user\n"
        raw_stderr = "02:00:41 [Info] Searching artifacts...\n"
        agent.conversation_history = [
            {
                "role": "user",
                "content": agent._build_direct_shell_user_history_content("d:/tmp/builds/install-zr.bat"),
            },
            {
                "role": "assistant",
                "content": agent._build_direct_shell_result_history_content(
                    "!d:/tmp/builds/install-zr.bat",
                    "d:/tmp/builds/install-zr.bat",
                    "D:/ws",
                    137,
                    raw_stdout,
                    raw_stderr,
                    aborted_by_user=True,
                ),
            },
        ]
        with (
            patch("src.agent._ansi_gray", side_effect=lambda s: s),
            patch.object(agent, "_print_direct_shell_command_feedback"),
            patch.object(agent, "_print_direct_shell_history_separator") as mock_sep,
            patch.object(agent, "_print_conversation_interrupted_banner") as mock_banner,
            patch("builtins.print"),
        ):
            captured = {}

            def _capture_output(stdout_text, stderr_text, force_first_line_continuation=False):
                captured["stdout"] = str(stdout_text)
                captured["stderr"] = str(stderr_text)
                captured["force"] = bool(force_first_line_continuation)

            with patch.object(agent, "_print_direct_shell_history_output", side_effect=_capture_output):
                agent._print_chat_history()
        self.assertEqual(captured.get("stderr"), "")
        rendered = str(captured.get("stdout") or "")
        self.assertTrue(rendered.endswith("command aborted by user\n"))
        self.assertIn("02:00:41 [Info] Searching artifacts...\n", rendered)
        self.assertFalse(bool(captured.get("force", False)))
        mock_sep.assert_not_called()
        mock_banner.assert_called_once_with()

    def test_chat_history_task_interrupted_event_prints_banner(self):
        agent = self._build_agent()
        agent.active_chat_name = "Demo Chat"
        agent.conversation_history = [
            {"role": "user", "content": "继续执行上一个任务"},
            {
                "role": "assistant",
                "content": agent._build_conversation_interrupted_history_content(
                    interrupted_kind="task",
                    reason="user_interrupt",
                    detail="修复构建脚本",
                ),
            },
        ]
        with (
            patch("src.agent._ansi_gray", side_effect=lambda s: s),
            patch.object(agent, "_print_direct_shell_history_separator") as mock_sep,
            patch.object(agent, "_print_conversation_interrupted_banner") as mock_banner,
            patch("builtins.print"),
        ):
            agent._print_chat_history()
        mock_sep.assert_not_called()
        mock_banner.assert_called_once_with()

    def test_chat_history_replays_internal_slash_command_and_output(self):
        agent = self._build_agent()
        agent.active_chat_name = "Demo Chat"
        agent.conversation_history = [
            {
                "role": "user",
                "content": agent._build_internal_slash_user_history_content("/chat reload"),
            },
            {
                "role": "assistant",
                "content": agent._build_internal_slash_result_history_content(
                    raw_user_command="/chat reload",
                    output_text="line-1\nline-2\n",
                ),
            },
        ]
        with (
            patch("src.agent._ansi_gray", side_effect=lambda s: s),
            patch.object(agent, "_print_internal_slash_history_output") as mock_slash_out,
            patch("builtins.print") as mock_print,
        ):
            agent._print_chat_history()
        joined = "\n".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
        self.assertIn("› /chat reload", joined)
        mock_slash_out.assert_called_once_with("line-1\nline-2\n")

    def test_record_internal_slash_execution_history_skips_chat_reload(self):
        agent = self._build_agent()
        with patch.object(agent, "_append_chat_message") as mock_append:
            agent._record_internal_slash_execution_history(
                raw_user_command="/chat reload",
                output_text="should-not-be-recorded\n",
            )
        mock_append.assert_not_called()

    def test_record_internal_slash_execution_history_skips_clear_context_and_clear_screen(self):
        agent = self._build_agent()
        with patch.object(agent, "_append_chat_message") as mock_append:
            agent._record_internal_slash_execution_history(
                raw_user_command="/clear context",
                output_text="should-not-be-recorded\n",
            )
            agent._record_internal_slash_execution_history(
                raw_user_command="/clear screen",
                output_text="should-not-be-recorded\n",
            )
        mock_append.assert_not_called()

    def test_record_internal_slash_execution_history_skips_chat_switch(self):
        agent = self._build_agent()
        with patch.object(agent, "_append_chat_message") as mock_append:
            agent._record_internal_slash_execution_history(
                raw_user_command="/chat switch demo",
                output_text="should-not-be-recorded\n",
            )
            agent._record_internal_slash_execution_history(
                raw_user_command="/CHAT   SWITCH   2",
                output_text="should-not-be-recorded\n",
            )
        mock_append.assert_not_called()

    def test_record_internal_slash_execution_history_records_other_commands(self):
        agent = self._build_agent()
        agent._record_internal_slash_execution_history(
            raw_user_command="/chat list",
            output_text="listed\n",
        )
        self.assertEqual(len(agent.conversation_history), 2)
        self.assertEqual(
            agent._parse_internal_slash_user_history_content(
                agent.conversation_history[0]["content"]
            ),
            "/chat list",
        )
        payload = agent._parse_internal_slash_result_history_content(
            agent.conversation_history[1]["content"]
        )
        self.assertIsNotNone(payload)
        self.assertEqual(payload.get("output"), "listed\n")
        self.assertFalse(agent.conversation_history[0].get("persist_to_chat_state", True))
        self.assertFalse(agent.conversation_history[1].get("persist_to_chat_state", True))
        self.assertTrue(agent.conversation_history[0].get("exclude_from_model_context"))
        self.assertTrue(agent.conversation_history[1].get("exclude_from_model_context"))

    def test_auto_chat_name_ignores_internal_slash_user_history(self):
        agent = self._build_agent()
        agent.active_chat_name = "New Chat"
        chat = {
            "id": "chat-1",
            "name": "New Chat",
            "name_source": "default",
            "messages": [
                {
                    "role": "user",
                    "content": agent._build_internal_slash_user_history_content("/chat new"),
                },
                {
                    "role": "assistant",
                    "content": agent._build_internal_slash_result_history_content(
                        raw_user_command="/chat new",
                        output_text="created\n",
                    ),
                },
            ],
        }
        agent._chat_state_lock = _NoopLock()
        agent._find_chat_by_id = lambda chat_id: chat if str(chat_id) == "chat-1" else None
        agent._save_chat_state = lambda: None
        with patch.object(agent, "call_ai", return_value="INTERNAL_SLASH_USE") as mock_call_ai:
            agent._maybe_schedule_auto_chat_name()
        mock_call_ai.assert_not_called()
        self.assertEqual(chat.get("name"), "New Chat")
        self.assertEqual(chat.get("name_source"), "default")
        self.assertEqual(agent.active_chat_name, "New Chat")

    def test_chat_history_replays_task_worked_summary_line(self):
        agent = self._build_agent()
        agent.active_chat_name = "Demo Chat"
        agent.conversation_history = [
            {
                "role": "assistant",
                "content": agent._build_task_worked_summary_history_content(125),
            },
        ]
        with (
            patch.object(agent, "_print_task_worked_summary_line") as mock_worked,
            patch("builtins.print"),
        ):
            agent._print_chat_history()
        mock_worked.assert_called_once_with(125)

    def test_resize_reload_uses_recorded_history_anchor(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old-reply"},
            {"role": "user", "content": "new"},
        ]
        agent._remember_active_chat_history_first_visible_index(2)
        widths = iter([100, 100])
        with (
            patch.object(agent, "_status_bar_render_data", return_value=([], "status")),
            patch.object(agent, "_terminal_columns_for_prompt_separator", side_effect=lambda default=80: next(widths)),
            patch.object(agent, "_print_chat_history") as mock_history,
            patch("src.agent.os.system"),
            patch("src.runtime.runtime_loop._print_startup_overview"),
        ):
            agent._chat_history_reload_last_terminal_width = 80
            out = agent._get_user_input_with_history()
        self.assertEqual(out, "hello")
        mock_history.assert_called_once_with(start_index=2)

    def test_chat_reload_tail_anchor_uses_latest_user_turn(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old-reply"},
            {"role": "user", "content": "latest"},
            {"role": "assistant", "content": "latest-reply"},
            {
                "role": "user",
                "content": agent._build_internal_slash_user_history_content("/chat reload"),
            },
            {"role": "assistant", "content": "reload-output"},
        ]

        start = agent._remember_active_chat_history_tail_anchor()

        self.assertEqual(start, 2)
        self.assertEqual(agent._get_active_chat_history_first_visible_index(), 2)

    def test_resize_check_first_snapshot_does_not_reload(self):
        agent = self._build_agent()
        with (
            patch.object(agent, "_status_bar_render_data", return_value=([], "status")),
            patch.object(agent, "_terminal_columns_for_prompt_separator", return_value=120),
            patch.object(agent, "_reload_chat_history_from_anchor_on_resize") as mock_reload,
        ):
            out = agent._get_user_input_with_history()
        self.assertEqual(out, "hello")
        mock_reload.assert_not_called()

    def test_chat_history_with_end_anchor_does_not_print_empty_hint(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
        ]
        with patch("builtins.print") as mock_print:
            agent._print_chat_history(start_index=2)
        printed_text = "\n".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
        self.assertNotIn("当前 Chat 暂无历史消息", printed_text)

    def test_resize_detection_uses_same_input_handler_width_source_as_separator(self):
        agent = self._build_agent()
        class _InputHandlerCols100(_FakeInputHandler):
            def get_terminal_columns(self, default=80):
                return 100
        agent.input_handler = _InputHandlerCols100()
        with (
            patch.object(agent, "_status_bar_render_data", return_value=([], "status")),
            patch("src.agent.os.get_terminal_size", return_value=types.SimpleNamespace(columns=80)),
            patch.object(agent, "_reload_chat_history_from_anchor_on_resize") as mock_reload,
        ):
            agent._chat_history_reload_last_terminal_width = 80
            out = agent._get_user_input_with_history()
        self.assertEqual(out, "hello")
        mock_reload.assert_called_once_with()

    def test_force_reload_flag_triggers_reload_before_prompt(self):
        agent = self._build_agent()
        agent._force_reload_chat_history_from_anchor_once = True
        with (
            patch.object(agent, "_status_bar_render_data", return_value=([], "status")),
            patch.object(agent, "_reload_chat_history_from_anchor_on_resize") as mock_reload,
            patch.object(agent, "_maybe_reload_chat_history_on_terminal_resize") as mock_maybe,
        ):
            out = agent._get_user_input_with_history()
        self.assertEqual(out, "hello")
        mock_reload.assert_called_once_with()
        mock_maybe.assert_not_called()
        self.assertFalse(agent._force_reload_chat_history_from_anchor_once)

    def test_prompt_reload_suppression_consumes_forced_reload_after_command_abort(self):
        agent = self._build_agent()
        agent._force_reload_chat_history_from_anchor_once = True
        agent._suppress_next_prompt_chat_reload_once = True
        agent._chat_history_reload_last_terminal_width = 80

        class _InputHandlerCols120(_FakeInputHandler):
            def get_terminal_columns(self, default=80):
                return 120

        agent.input_handler = _InputHandlerCols120()
        with (
            patch.object(agent, "_status_bar_render_data", return_value=([], "status")),
            patch.object(agent, "_reload_chat_history_from_anchor_on_resize") as mock_reload,
            patch.object(agent, "_maybe_reload_chat_history_on_terminal_resize") as mock_maybe,
        ):
            out = agent._get_user_input_with_history()

        self.assertEqual(out, "hello")
        mock_reload.assert_not_called()
        mock_maybe.assert_not_called()
        self.assertFalse(agent._force_reload_chat_history_from_anchor_once)
        self.assertFalse(agent._suppress_next_prompt_chat_reload_once)
        self.assertEqual(agent._chat_history_reload_last_terminal_width, 120)

    def test_prompt_reload_suppression_consumes_next_width_reload_after_command_abort(self):
        agent = self._build_agent()
        agent._suppress_next_prompt_chat_reload_once = True
        agent._chat_history_reload_last_terminal_width = 80

        class _InputHandlerCols120(_FakeInputHandler):
            def get_terminal_columns(self, default=80):
                return 120

        agent.input_handler = _InputHandlerCols120()
        with (
            patch.object(agent, "_status_bar_render_data", return_value=([], "status")),
            patch.object(agent, "_reload_chat_history_from_anchor_on_resize") as mock_reload,
            patch.object(agent, "_maybe_reload_chat_history_on_terminal_resize") as mock_maybe,
        ):
            out = agent._get_user_input_with_history()

        self.assertEqual(out, "hello")
        mock_reload.assert_not_called()
        mock_maybe.assert_not_called()
        self.assertFalse(agent._suppress_next_prompt_chat_reload_once)
        self.assertEqual(agent._chat_history_reload_last_terminal_width, 120)

    def test_terminal_resize_callback_sets_force_reload_flag(self):
        agent = self._build_agent()
        should_interrupt = agent._handle_terminal_columns_changed_during_input(80, 120)
        self.assertTrue(should_interrupt)
        self.assertTrue(agent._force_reload_chat_history_from_anchor_once)
        self.assertEqual(agent._chat_history_reload_last_terminal_width, 120)

    def test_rewrite_previous_prompt_as_user_clears_all_multiline_rows_and_indents_continuation(self):
        agent = self._build_agent()
        fake_stdout = _FakeStdout()
        with (
            patch("src.agent._ansi_gray", side_effect=lambda s: s),
            patch("src.agent.sys.stdout", fake_stdout),
            patch.object(agent, "_estimate_rendered_line_count", return_value=2),
        ):
            agent._rewrite_previous_prompt_as_user("hello\n你好")
        out = "".join(fake_stdout.writes)
        self.assertIn("\x1b[1A\r\x1b[2K\x1b[1A\r\x1b[2K", out)
        self.assertIn("› hello\n  你好\n", out)
        self.assertNotIn("› hello\n你好\n", out)

    def test_chat_history_tool_feedback_marks_failed_from_operation_results(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {"role": "assistant", "content": "{\"tool\":\"read\",\"args\":{\"path\":\"a.txt\"}}"},
        ]
        agent.operation_results = [
            {
                "command": {"tool": "read", "args": {"path": "a.txt"}},
                "result": {"success": False},
            }
        ]
        with (
            patch("builtins.print"),
            patch.object(agent, "_print_tool_call_feedback") as mock_feedback,
        ):
            agent._print_chat_history()
        mock_feedback.assert_called_once_with("read", {"path": "a.txt"}, failed=True)

    def test_chat_history_replays_model_shell_output_using_direct_shell_renderer(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {
                "role": "assistant",
                "content": "{\"tool\":\"shell\",\"args\":{\"command\":\"echo hi\"}}",
            },
        ]
        agent.operation_results = [
            {
                "command": {"tool": "shell", "args": {"command": "echo hi"}},
                "result": {
                    "success": True,
                    "output": "hi\n",
                },
            }
        ]
        with (
            patch("builtins.print"),
            patch.object(agent, "_print_tool_call_feedback") as mock_feedback,
            patch.object(agent, "_print_direct_shell_history_output") as mock_shell_output,
            patch.object(agent, "_print_direct_shell_history_separator") as mock_sep,
        ):
            agent._print_chat_history()
        mock_feedback.assert_called_once_with("shell", {"command": "echo hi"}, failed=False)
        mock_shell_output.assert_called_once_with("hi\n", "")
        mock_sep.assert_not_called()

    def test_extract_model_shell_replay_output_uses_single_raw_output_field(self):
        agent = self._build_agent()
        out, err = agent._extract_model_shell_replay_output(
            {
                "success": True,
                "output": "first\nsecond\n",
            }
        )
        self.assertEqual(out, "first\nsecond\n")
        self.assertEqual(err, "")

    def test_chat_history_model_shell_placeholder_result_does_not_synthesize_no_output(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {
                "role": "assistant",
                "content": "{\"tool\":\"shell\",\"args\":{\"command\":\"ping example.com\"}}",
            },
        ]
        agent.operation_results = [
            {
                "command": {"tool": "shell", "args": {"command": "ping example.com"}},
                "result": {},
            }
        ]
        with (
            patch("builtins.print"),
            patch.object(agent, "_print_tool_call_feedback") as mock_feedback,
            patch.object(agent, "_print_direct_shell_history_output") as mock_shell_output,
        ):
            agent._print_chat_history()
        mock_feedback.assert_called_once_with(
            "shell",
            {"command": "ping example.com"},
            failed=False,
        )
        mock_shell_output.assert_not_called()

    def test_chat_history_repeated_model_shell_plan_does_not_reuse_prior_result(self):
        agent = self._build_agent()
        command = {"command": "ping example.com"}
        agent.conversation_history = [
            {
                "role": "assistant",
                "content": "{\"tool\":\"shell\",\"args\":{\"command\":\"ping example.com\"}}",
            },
            {
                "role": "assistant",
                "content": agent._build_model_tool_result_history_content(
                    "shell",
                    command,
                    {
                        "success": False,
                        "output": "old output\ncommand aborted by user\n",
                    },
                ),
            },
            {
                "role": "assistant",
                "content": "{\"tool\":\"shell\",\"args\":{\"command\":\"ping example.com\"}}",
            },
        ]
        agent.operation_results = [
            {
                "command": {"tool": "shell", "args": command},
                "result": {
                    "success": False,
                    "output": "old output\ncommand aborted by user\n",
                },
            }
        ]
        with (
            patch("builtins.print"),
            patch.object(agent, "_print_tool_call_feedback") as mock_feedback,
            patch.object(agent, "_print_direct_shell_history_output") as mock_shell_output,
        ):
            agent._print_chat_history()
        self.assertEqual(mock_feedback.call_count, 2)
        mock_shell_output.assert_called_once_with(
            "old output\ncommand aborted by user\n",
            "",
        )

    def test_chat_history_model_shell_result_replays_failed_output_without_operation_results(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {
                "role": "assistant",
                "content": "{\"tool\":\"shell\",\"args\":{\"command\":\"test\"}}",
            },
            {
                "role": "assistant",
                "content": agent._build_model_tool_result_history_content(
                    "shell",
                    {"command": "test"},
                    {
                        "success": False,
                        "output": "'test' is not recognized\n",
                    },
                ),
            },
        ]
        agent.operation_results = []
        with (
            patch("builtins.print"),
            patch.object(agent, "_print_tool_call_feedback") as mock_feedback,
            patch.object(agent, "_print_direct_shell_history_output") as mock_shell_output,
            patch.object(agent, "_print_direct_shell_history_separator") as mock_sep,
        ):
            agent._print_chat_history()
        mock_feedback.assert_called_once_with("shell", {"command": "test"}, failed=True)
        mock_shell_output.assert_called_once_with("'test' is not recognized\n", "")
        mock_sep.assert_not_called()

    def test_chat_history_aborted_model_shell_result_prints_interrupt_banner_without_event(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {
                "role": "assistant",
                "content": agent._build_model_tool_result_history_content(
                    "shell",
                    {"command": "ping example.com"},
                    {
                        "success": False,
                        "output": "line\ncommand aborted by user\n",
                    },
                ),
            },
        ]
        with (
            patch("builtins.print"),
            patch.object(agent, "_print_tool_call_feedback") as mock_feedback,
            patch.object(agent, "_print_direct_shell_history_output") as mock_shell_output,
            patch.object(agent, "_print_conversation_interrupted_banner") as mock_banner,
        ):
            agent._print_chat_history()

        mock_feedback.assert_called_once_with(
            "shell",
            {"command": "ping example.com"},
            failed=True,
        )
        mock_shell_output.assert_called_once_with("line\ncommand aborted by user\n", "")
        mock_banner.assert_called_once_with()

    def test_chat_history_aborted_model_shell_result_does_not_duplicate_interrupt_banner_event(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {
                "role": "assistant",
                "content": agent._build_model_tool_result_history_content(
                    "shell",
                    {"command": "ping example.com"},
                    {
                        "success": False,
                        "output": "line\ncommand aborted by user\n",
                    },
                ),
            },
            {
                "role": "assistant",
                "content": agent._build_conversation_interrupted_history_content(
                    interrupted_kind="task",
                    reason="user_interrupt",
                ),
            },
        ]
        with (
            patch("builtins.print"),
            patch.object(agent, "_print_tool_call_feedback"),
            patch.object(agent, "_print_direct_shell_history_output"),
            patch.object(agent, "_print_conversation_interrupted_banner") as mock_banner,
        ):
            agent._print_chat_history()

        mock_banner.assert_called_once_with()

    def test_chat_history_assistant_after_shell_output_starts_on_new_line(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {
                "role": "assistant",
                "content": agent._build_model_tool_result_history_content(
                    "shell",
                    {"command": "test"},
                    {
                        "success": True,
                        "output": "partial",
                    },
                ),
            },
            {
                "role": "assistant",
                "content": "final answer",
            },
        ]

        class _TtyBuffer:
            def __init__(self):
                self.parts = []

            def isatty(self):
                return True

            def fileno(self):
                return 1

            def write(self, text):
                self.parts.append(str(text))
                return len(str(text))

            def flush(self):
                return None

            def getvalue(self):
                return "".join(self.parts)

        out_buf = _TtyBuffer()
        err_buf = _TtyBuffer()
        with (
            patch("src.agent.sys.stdout", out_buf),
            patch("src.agent.sys.stderr", err_buf),
        ):
            agent._print_chat_history()

        ansi_re = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
        out_plain = ansi_re.sub("", out_buf.getvalue()).lstrip("\r")
        self.assertIn("  └ partial\n• final answer\n", out_plain)

    def test_chat_history_model_shell_result_rebuilds_tail_from_full_output(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {
                "role": "assistant",
                "content": agent._build_model_tool_result_history_content(
                    "shell",
                    {"command": "test"},
                    {
                        "success": True,
                        "output": "line one\nline two\nline three\n",
                        "display_output": "old formatted display\n",
                    },
                ),
            },
        ]
        agent.operation_results = []
        with (
            patch("builtins.print"),
            patch("src.agent.command_actions._dynamic_tail_line_limit", return_value=2),
            patch.object(agent, "_print_tool_call_feedback") as mock_feedback,
            patch.object(agent, "_print_direct_shell_history_output") as mock_shell_output,
        ):
            agent._print_chat_history()
        mock_feedback.assert_called_once_with("shell", {"command": "test"}, failed=False)
        mock_shell_output.assert_called_once()
        replay_out, replay_err = mock_shell_output.call_args.args
        self.assertIn("omitted 2 lines", replay_out)
        self.assertIn("line three\n", replay_out)
        self.assertNotIn("line two\n", replay_out)
        self.assertNotIn("line one\n", replay_out)
        self.assertNotIn("old formatted display", replay_out)
        self.assertEqual(replay_err, "")

    def test_model_shell_result_history_stores_raw_output_without_display_payload(self):
        agent = self._build_agent()
        content = agent._build_model_tool_result_history_content(
            "shell",
            {"command": "test"},
            {
                "success": True,
                "output": "full output\n",
                "display_output": "old display\n",
            },
        )
        payload = agent._parse_model_tool_result_history_content(content)
        self.assertIsNotNone(payload)
        self.assertEqual(payload.get("output"), "full output\n")
        self.assertNotIn("display_output", payload)
        self.assertNotIn("stderr", payload)

    def test_chat_history_model_shell_result_synthesizes_no_output_on_replay(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {
                "role": "assistant",
                "content": agent._build_model_tool_result_history_content(
                    "shell",
                    {"command": "true"},
                    {"success": True, "output": ""},
                ),
            },
        ]
        with (
            patch("builtins.print"),
            patch.object(agent, "_print_tool_call_feedback"),
            patch.object(agent, "_print_direct_shell_history_output") as mock_shell_output,
        ):
            agent._print_chat_history()
        mock_shell_output.assert_called_once_with("(no output)\n", "")

    def test_refresh_after_tool_output_keeps_history_anchor_position(self):
        agent = self._build_agent()
        agent.conversation_history = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ]
        agent._remember_active_chat_history_first_visible_index(2)
        with (
            patch("src.agent.os.system"),
            patch("src.runtime.runtime_loop._print_startup_overview"),
            patch.object(agent, "_sync_active_chat_messages") as mock_sync,
            patch.object(agent, "_print_chat_history") as mock_history,
        ):
            agent._refresh_chat_history_after_tool_output()
        mock_sync.assert_called_once_with()
        mock_history.assert_called_once_with(start_index=2)


if __name__ == "__main__":
    unittest.main()
