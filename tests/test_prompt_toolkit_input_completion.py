import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from src.config.app_info import get_app_runtime_attr_name
from src.completion import prompt_toolkit_input as pti
from src.completion.prompt_toolkit_input import FileCompleter

_RESIZE_ATTR_DRAFT = get_app_runtime_attr_name("resize_draft", leading_underscore=True)
_RESIZE_ATTR_CURSOR = get_app_runtime_attr_name("resize_cursor_position", leading_underscore=True)
_RESIZE_ATTR_INTERRUPTED = get_app_runtime_attr_name("resize_interrupted", leading_underscore=True)


class _Doc:
    def __init__(self, text: str):
        self.text_before_cursor = text


class _FakeSession:
    def __init__(self, returned_text: str):
        self.returned_text = returned_text
        self.calls = []

    def prompt(self, prompt_line: str, **kwargs):
        self.calls.append((prompt_line, kwargs))
        return self.returned_text


class _FakeOutput:
    def __init__(self, columns: int):
        self._columns = columns

    def get_size(self):
        class _Size:
            pass
        size = _Size()
        size.columns = self._columns
        return size


class _FakeEvent:
    def __init__(self):
        self._handlers = []

    def add_handler(self, handler):
        self._handlers.append(handler)

    def fire(self, app):
        for handler in list(self._handlers):
            handler(app)


class _FakeHookApp:
    def __init__(self, columns: int = 80):
        self.output = _FakeOutput(columns)
        self.before_render = _FakeEvent()
        self.after_render = _FakeEvent()
        self.current_buffer = _FakeBuffer("draft")
        self.exit_calls = []

    def exit(self, result=""):
        self.exit_calls.append(result)


class _FakeHookSession:
    def __init__(self, app: _FakeHookApp):
        self.app = app


class _FakeSessionWithHook:
    def __init__(self, returned_text: str, before_return=None):
        self.returned_text = returned_text
        self.before_return = before_return
        self.calls = []
        self.output = _FakeOutput(60)

    def prompt(self, prompt_line: str, **kwargs):
        self.calls.append((prompt_line, kwargs))
        if callable(self.before_return):
            self.before_return()
        return self.returned_text


class _FakeBuffer:
    def __init__(self, text: str):
        self.text = text
        self.cursor_position = len(text)


class _CursorAwareSession:
    def __init__(self, returned_text: str):
        self.returned_text = returned_text
        self.calls = []
        self.output = _FakeOutput(80)
        self.last_cursor_position = None

    def prompt(self, prompt_line: str, **kwargs):
        self.calls.append((prompt_line, kwargs))
        default_text = str(kwargs.get("default", "") or "")
        fake_buffer = _FakeBuffer(default_text)
        self.app = type("App", (), {"current_buffer": fake_buffer})()
        pre_run = kwargs.get("pre_run")
        if callable(pre_run):
            pre_run()
        self.last_cursor_position = fake_buffer.cursor_position
        return self.returned_text


class PromptToolkitInputCompletionTests(unittest.TestCase):
    def test_resize_hook_uses_initial_columns_snapshot_for_first_shrink(self):
        app = _FakeHookApp(columns=120)
        session = _FakeHookSession(app)
        calls = []

        def _resize_callback(prev_cols: int, new_cols: int) -> bool:
            calls.append((prev_cols, new_cols))
            return True

        with patch.object(pti, "_get_system_terminal_columns", return_value=0):
            pti._attach_blink_after_render_hook(
                session,
                status_provider=lambda: "",
                terminal_resize_callback=_resize_callback,
            )

        app.output._columns = 100
        with patch.object(pti, "_get_system_terminal_columns", return_value=0):
            app.before_render.fire(app)

        self.assertEqual(calls, [(120, 100)])
        self.assertTrue(bool(getattr(session, _RESIZE_ATTR_INTERRUPTED, False)))
        self.assertEqual(app.exit_calls, [""])

    def test_resize_hook_uses_system_width_when_output_width_is_stale_on_expand(self):
        app = _FakeHookApp(columns=100)
        session = _FakeHookSession(app)
        calls = []

        def _resize_callback(prev_cols: int, new_cols: int) -> bool:
            calls.append((prev_cols, new_cols))
            return True

        with patch.object(pti, "_get_system_terminal_columns", side_effect=[100, 120]):
            pti._attach_blink_after_render_hook(
                session,
                status_provider=lambda: "",
                terminal_resize_callback=_resize_callback,
            )
            # Keep prompt_toolkit output width stale to emulate occasional lag.
            app.output._columns = 100
            app.before_render.fire(app)

        self.assertEqual(calls, [(100, 120)])
        self.assertTrue(bool(getattr(session, _RESIZE_ATTR_INTERRUPTED, False)))
        self.assertEqual(app.exit_calls, [""])

    def test_windows_shift_state_detector_returns_false_on_non_windows(self):
        with patch.object(pti.os, "name", "posix"):
            self.assertFalse(pti._is_windows_shift_pressed())

    def test_windows_shift_state_detector_uses_native_key_state(self):
        with (
            patch.object(pti.os, "name", "nt"),
            patch.object(pti, "_windows_get_async_key_state", return_value=0),
            patch.object(pti, "_windows_get_key_state", side_effect=[0, 0x8000, 0]),
        ):
            self.assertTrue(pti._is_windows_shift_pressed())

    def test_windows_shift_state_detector_returns_false_when_no_shift_down(self):
        with (
            patch.object(pti.os, "name", "nt"),
            patch.object(pti, "_windows_get_async_key_state", return_value=0),
            patch.object(pti, "_windows_get_key_state", return_value=0),
        ):
            self.assertFalse(pti._is_windows_shift_pressed())

    def test_shift_enter_aliases_include_common_terminal_sequences(self):
        aliases = set(pti.SHIFT_ENTER_KEY_ALIASES)
        self.assertIn(("escape", "[", "1", "3", ";", "2", "u"), aliases)
        self.assertIn(("escape", "[", "2", "7", ";", "2", ";", "1", "3", "~"), aliases)
        self.assertIn(("escape", "enter"), aliases)
        self.assertIn(("escape", "O", "M"), aliases)

    def test_lone_slash_on_posix_uses_slash_commands_not_root_files(self):
        with tempfile.TemporaryDirectory() as td:
            completer = FileCompleter(Path(td))
            with patch.object(completer, "_get_path_completions", side_effect=AssertionError("path completion should not run for lone slash")):
                with patch.object(pti.os, "name", "posix"):
                    out = list(completer.get_completions(_Doc("/"), None))
        self.assertTrue(len(out) > 0)
        self.assertTrue(any(str(getattr(c, "text", "")).startswith("/") for c in out))

    def test_posix_absolute_path_still_uses_path_completion(self):
        with tempfile.TemporaryDirectory() as td:
            completer = FileCompleter(Path(td))
            with patch.object(completer, "_get_path_completions", return_value=["/tmp/demo.txt"]) as mocked:
                with patch.object(pti.os, "name", "posix"):
                    out = list(completer.get_completions(_Doc("/tmp/de"), None))
        self.assertTrue(mocked.called)
        self.assertTrue(any(getattr(c, "text", "") == "/tmp/demo.txt" for c in out))

    def test_local_completion_prefers_workspace_directory(self):
        with tempfile.TemporaryDirectory() as wd, tempfile.TemporaryDirectory() as ws:
            wd_path = Path(wd)
            ws_path = Path(ws)
            (wd_path / "from_work_dir.txt").write_text("x", encoding="utf-8")
            (ws_path / "from_workspace.txt").write_text("x", encoding="utf-8")
            completer = FileCompleter(wd_path, workspace_directory=ws_path)
            out = completer._get_local_completions("from_")
        self.assertIn("from_workspace.txt", out)
        self.assertNotIn("from_work_dir.txt", out)

    def test_get_input_uses_multiline_prompt_and_two_space_continuation(self):
        handler = pti.PromptToolkitInputHandler.__new__(pti.PromptToolkitInputHandler)
        handler.session = _FakeSession("line1\nline2")
        handler.history = []
        handler.work_directory = Path.cwd()
        handler._status_bar_text = ""
        handler._status_bar_fragments = []
        handler._status_bar_enabled = True
        handler._clear_status_overlay_line_if_possible = lambda: None

        out = handler.get_input_with_completion("› ")
        self.assertEqual(out, "line1\nline2")
        self.assertEqual(handler.history, ["line1\nline2"])

        _, kwargs = handler.session.calls[0]
        self.assertTrue(bool(kwargs.get("multiline", False)))
        continuation = kwargs.get("prompt_continuation")
        self.assertTrue(callable(continuation))
        self.assertEqual(continuation(2, 1, True), "  ")

    def test_get_input_normalizes_mixed_newlines(self):
        handler = pti.PromptToolkitInputHandler.__new__(pti.PromptToolkitInputHandler)
        handler.session = _FakeSession("a\r\nb\rc")
        handler.history = []
        handler.work_directory = Path.cwd()
        handler._status_bar_text = ""
        handler._status_bar_fragments = []
        handler._status_bar_enabled = True
        handler._clear_status_overlay_line_if_possible = lambda: None

        out = handler.get_input_with_completion("› ")
        self.assertEqual(out, "a\nb\nc")
        self.assertEqual(handler.history[-1], "a\nb\nc")

    def test_get_input_clears_overlay_after_prompt_submit(self):
        handler = pti.PromptToolkitInputHandler.__new__(pti.PromptToolkitInputHandler)
        handler.session = _FakeSession("echo hi")
        handler.history = []
        handler.work_directory = Path.cwd()
        handler._status_bar_text = ""
        handler._status_bar_fragments = []
        handler._status_bar_enabled = True
        calls = {"n": 0}
        def _clear():
            calls["n"] += 1
        handler._clear_status_overlay_line_if_possible = _clear

        out = handler.get_input_with_completion("› ")
        self.assertEqual(out, "echo hi")
        self.assertEqual(calls["n"], 1)

    def test_resize_interrupted_prompt_restores_unsent_draft_on_next_prompt(self):
        handler = pti.PromptToolkitInputHandler.__new__(pti.PromptToolkitInputHandler)
        interrupted_session = _FakeSessionWithHook("")
        stable_session = _FakeSession("hello\n你好 world")
        handler.session = interrupted_session
        handler.history = []
        handler.work_directory = Path.cwd()
        handler._status_bar_text = ""
        handler._status_bar_fragments = []
        handler._status_bar_enabled = True
        handler._pending_prefill_text = ""
        handler._clear_status_overlay_line_if_possible = lambda: None

        def _simulate_resize_interrupt():
            setattr(interrupted_session, _RESIZE_ATTR_INTERRUPTED, True)
            setattr(interrupted_session, _RESIZE_ATTR_DRAFT, "hello\r\n你好")

        interrupted_session.before_return = _simulate_resize_interrupt

        out1 = handler.get_input_with_completion("› ")
        self.assertEqual(out1, "")
        self.assertEqual(handler._pending_prefill_text, "hello\n你好")
        self.assertEqual(handler.history, [])

        handler.session = stable_session
        out2 = handler.get_input_with_completion("› ")
        self.assertEqual(out2, "hello\n你好 world")
        self.assertEqual(handler._pending_prefill_text, "")
        _, kwargs = stable_session.calls[0]
        self.assertEqual(kwargs.get("default"), "hello\n你好")
        self.assertEqual(handler.history, ["hello\n你好 world"])

    def test_shell_mode_is_preserved_after_resize_interrupted_reload(self):
        class _ResolvedPromptSession(_FakeSession):
            def prompt(self, prompt_line: str, **kwargs):
                resolved = prompt_line() if callable(prompt_line) else prompt_line
                self.calls.append((resolved, kwargs))
                return self.returned_text

        handler = pti.PromptToolkitInputHandler.__new__(pti.PromptToolkitInputHandler)
        interrupted_session = _FakeSessionWithHook("")
        stable_session = _ResolvedPromptSession("dir /b")
        handler.session = interrupted_session
        handler.history = []
        handler.work_directory = Path.cwd()
        handler._status_bar_text = ""
        handler._status_bar_fragments = []
        handler._status_bar_enabled = True
        handler._pending_prefill_text = ""
        handler._pending_shell_mode_active = False
        handler._shell_mode_active = False
        handler._clear_status_overlay_line_if_possible = lambda: None

        def _simulate_shell_resize_interrupt():
            handler._shell_mode_active = True
            setattr(interrupted_session, _RESIZE_ATTR_INTERRUPTED, True)
            setattr(interrupted_session, _RESIZE_ATTR_DRAFT, "dir")

        interrupted_session.before_return = _simulate_shell_resize_interrupt

        out1 = handler.get_input_with_completion("› ")
        self.assertEqual(out1, "")
        self.assertEqual(handler._pending_prefill_text, "dir")
        self.assertTrue(handler._pending_shell_mode_active)

        handler.session = stable_session
        out2 = handler.get_input_with_completion("› ")
        self.assertEqual(out2, "!dir /b")
        self.assertFalse(handler._pending_shell_mode_active)
        prompt_msg, kwargs = stable_session.calls[0]
        self.assertEqual(
            prompt_msg,
            [(f"fg:{pti.SHELL_MODE_COLOR_HEX}", pti.SHELL_MODE_PROMPT)],
        )
        self.assertEqual(kwargs.get("default"), "dir")
        self.assertEqual(handler.history, ["!dir /b"])

    def test_resize_interrupted_prompt_restores_cursor_position_on_next_prompt(self):
        handler = pti.PromptToolkitInputHandler.__new__(pti.PromptToolkitInputHandler)
        interrupted_session = _FakeSessionWithHook("")
        stable_session = _CursorAwareSession("hello world")
        handler.session = interrupted_session
        handler.history = []
        handler.work_directory = Path.cwd()
        handler._status_bar_text = ""
        handler._status_bar_fragments = []
        handler._status_bar_enabled = True
        handler._pending_prefill_text = ""
        handler._pending_prefill_cursor_position = 0
        handler._pending_shell_mode_active = False
        handler._shell_mode_active = False
        handler._clear_status_overlay_line_if_possible = lambda: None

        def _simulate_resize_interrupt():
            setattr(interrupted_session, _RESIZE_ATTR_INTERRUPTED, True)
            setattr(interrupted_session, _RESIZE_ATTR_DRAFT, "hello world")
            setattr(interrupted_session, _RESIZE_ATTR_CURSOR, 5)

        interrupted_session.before_return = _simulate_resize_interrupt

        out1 = handler.get_input_with_completion("› ")
        self.assertEqual(out1, "")
        self.assertEqual(handler._pending_prefill_text, "hello world")
        self.assertEqual(handler._pending_prefill_cursor_position, 5)

        handler.session = stable_session
        out2 = handler.get_input_with_completion("› ")
        self.assertEqual(out2, "hello world")
        self.assertEqual(stable_session.last_cursor_position, 5)
        self.assertEqual(handler._pending_prefill_cursor_position, 0)

    def test_shell_mode_prompt_message_is_red_bang_prompt(self):
        handler = pti.PromptToolkitInputHandler.__new__(pti.PromptToolkitInputHandler)
        handler._shell_mode_active = True
        handler._prompt_line = "› "
        msg = handler._shell_mode_prompt_message()
        self.assertEqual(msg, [(f"fg:{pti.SHELL_MODE_COLOR_HEX}", pti.SHELL_MODE_PROMPT)])

    def test_shell_mode_status_line_appends_right_label_with_padding(self):
        handler = pti.PromptToolkitInputHandler.__new__(pti.PromptToolkitInputHandler)
        handler.session = _FakeSessionWithHook("")
        rendered = handler._compose_shell_mode_status_line("\x1b[38;2;1;2;3mmodel\x1b[0m workspace")
        self.assertIn("Shell mode", rendered)
        self.assertIn("\x1b[38;2;1;2;3m", rendered)
        self.assertTrue(
            rendered.endswith(" " * pti._shell_mode_effective_right_padding())
        )
    
    def test_shell_mode_status_line_keeps_label_when_left_text_is_long(self):
        handler = pti.PromptToolkitInputHandler.__new__(pti.PromptToolkitInputHandler)
        s = _FakeSessionWithHook("")
        s.output = _FakeOutput(24)
        handler.session = s
        rendered = handler._compose_shell_mode_status_line("  very long left status text with colors")
        self.assertIn("Shell mode", rendered)
        plain = pti._strip_ansi_sgr(rendered)
        self.assertEqual(
            len(plain) - len(plain.rstrip(" ")),
            pti._shell_mode_effective_right_padding(),
        )

    def test_shell_mode_status_line_keeps_label_with_cjk_left_text(self):
        handler = pti.PromptToolkitInputHandler.__new__(pti.PromptToolkitInputHandler)
        s = _FakeSessionWithHook("")
        s.output = _FakeOutput(26)
        handler.session = s
        rendered = handler._compose_shell_mode_status_line("  默认工作区 聊天(12%)")
        self.assertIn("Shell mode", rendered)
        plain = pti._strip_ansi_sgr(rendered)
        self.assertEqual(
            len(plain) - len(plain.rstrip(" ")),
            pti._shell_mode_effective_right_padding(),
        )

    def test_shell_mode_submission_prefixes_command_with_bang(self):
        handler = pti.PromptToolkitInputHandler.__new__(pti.PromptToolkitInputHandler)
        def _activate_mode():
            handler._shell_mode_active = True
        handler.session = _FakeSessionWithHook("git status", before_return=_activate_mode)
        handler.history = []
        handler.work_directory = Path.cwd()
        handler._status_bar_text = ""
        handler._status_bar_fragments = []
        handler._status_bar_enabled = True
        out = handler.get_input_with_completion("› ")
        self.assertEqual(out, "!git status")

    def test_shell_mode_empty_submit_prints_hint_and_returns_empty(self):
        handler = pti.PromptToolkitInputHandler.__new__(pti.PromptToolkitInputHandler)
        def _activate_mode():
            handler._shell_mode_active = True
        handler.session = _FakeSessionWithHook("", before_return=_activate_mode)
        handler.history = []
        handler.work_directory = Path.cwd()
        handler._status_bar_text = ""
        handler._status_bar_fragments = []
        handler._status_bar_enabled = True
        with (
            patch("sys.stdout", new_callable=StringIO) as fake_out,
            patch.object(handler, "_erase_previous_prompt_line_if_tty") as erase_mock,
        ):
            out = handler.get_input_with_completion("› ")
        self.assertEqual(out, "")
        erase_mock.assert_called_once()
        rendered = fake_out.getvalue()
        self.assertIn("Prefix a command with ! to run it locally", rendered)
        self.assertIn("Example: !ls", rendered)

    def test_enter_on_empty_prompt_in_normal_mode_is_ignored(self):
        from prompt_toolkit.keys import Keys

        class _Buffer:
            def __init__(self, text: str):
                self.text = text
                self.validate_calls = 0

            def validate_and_handle(self):
                self.validate_calls += 1

        class _Event:
            def __init__(self, text: str):
                self.current_buffer = _Buffer(text)

        handler = pti.PromptToolkitInputHandler.__new__(pti.PromptToolkitInputHandler)
        handler._shell_mode_active = False
        kb = handler._create_key_bindings()
        enter_binding = next(
            b for b in kb.bindings if tuple(getattr(b, "keys", ())) == (Keys.ControlM,)
        )

        ev_empty = _Event("")
        enter_binding.handler(ev_empty)
        self.assertEqual(ev_empty.current_buffer.validate_calls, 0)

        ev_spaces = _Event("   ")
        enter_binding.handler(ev_spaces)
        self.assertEqual(ev_spaces.current_buffer.validate_calls, 0)

        ev_text = _Event("hello")
        enter_binding.handler(ev_text)
        self.assertEqual(ev_text.current_buffer.validate_calls, 1)

    def test_bang_at_line_start_switches_to_shell_mode_for_existing_text(self):
        class _Buffer:
            def __init__(self, text: str, cursor_position: int):
                self.text = text
                self.cursor_position = cursor_position

            def insert_text(self, text: str):
                left = self.text[: self.cursor_position]
                right = self.text[self.cursor_position :]
                self.text = f"{left}{text}{right}"
                self.cursor_position += len(text)

        class _App:
            def __init__(self):
                self.invalidate_calls = 0

            def invalidate(self):
                self.invalidate_calls += 1

        class _Event:
            def __init__(self, text: str, cursor_position: int):
                self.current_buffer = _Buffer(text, cursor_position)
                self.app = _App()

        handler = pti.PromptToolkitInputHandler.__new__(pti.PromptToolkitInputHandler)
        handler._shell_mode_active = False
        kb = handler._create_key_bindings()
        bang_binding = next(
            b for b in kb.bindings if tuple(getattr(b, "keys", ())) == ("!",)
        )

        ev = _Event("git status", 0)
        bang_binding.handler(ev)

        self.assertTrue(handler._shell_mode_active)
        self.assertEqual(ev.current_buffer.text, "git status")
        self.assertEqual(ev.app.invalidate_calls, 1)

    def test_bang_inside_existing_text_inserts_literal_bang(self):
        class _Buffer:
            def __init__(self, text: str, cursor_position: int):
                self.text = text
                self.cursor_position = cursor_position

            def insert_text(self, text: str):
                left = self.text[: self.cursor_position]
                right = self.text[self.cursor_position :]
                self.text = f"{left}{text}{right}"
                self.cursor_position += len(text)

        class _App:
            def __init__(self):
                self.invalidate_calls = 0

            def invalidate(self):
                self.invalidate_calls += 1

        class _Event:
            def __init__(self, text: str, cursor_position: int):
                self.current_buffer = _Buffer(text, cursor_position)
                self.app = _App()

        handler = pti.PromptToolkitInputHandler.__new__(pti.PromptToolkitInputHandler)
        handler._shell_mode_active = False
        kb = handler._create_key_bindings()
        bang_binding = next(
            b for b in kb.bindings if tuple(getattr(b, "keys", ())) == ("!",)
        )

        ev = _Event("git status", 3)
        bang_binding.handler(ev)

        self.assertFalse(handler._shell_mode_active)
        self.assertEqual(ev.current_buffer.text, "git! status")
        self.assertEqual(ev.app.invalidate_calls, 0)


if __name__ == "__main__":
    unittest.main()
