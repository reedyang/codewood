import sys
import types
import unittest
import subprocess
import re
import os
import threading
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.smart_shell_agent import SmartShellAgent


class BangDirectExecutionTests(unittest.TestCase):
    def setUp(self):
        self.agent = SmartShellAgent.__new__(SmartShellAgent)
        self.agent.work_directory = Path(".")
        self.agent.workspace_root = Path.cwd()
        self.agent.startup_initial_directory = Path.cwd()
        self.agent._save_current_workspace_position = lambda: None
        self.agent.input_handler = None
        self.agent._resolve_path_lenient = lambda p: Path(p).resolve()
        self.agent._interrupt_state_lock = threading.RLock()
        self.agent._interruptible_processes = {}
        self.agent._task_interrupt_requested = False
        self.agent._aborted_process_keys = set()

    @patch("subprocess.Popen")
    def test_keep_command_with_args_for_python_invocation(self, popen_mock):
        proc = types.SimpleNamespace(wait=lambda: 0)
        popen_mock.return_value = proc

        ok = self.agent._execute_file_directly("python helloworld.py")
        self.assertTrue(ok)
        called_cmd = popen_mock.call_args[0][0]
        self.assertEqual(called_cmd, "python helloworld.py")

    @patch("subprocess.Popen")
    def test_bare_py_script_is_wrapped_with_python(self, popen_mock):
        proc = types.SimpleNamespace(wait=lambda: 0)
        popen_mock.return_value = proc

        ok = self.agent._execute_file_directly("helloworld.py --flag")
        self.assertTrue(ok)
        called_cmd = popen_mock.call_args[0][0]
        self.assertIn("python", called_cmd.lower())
        self.assertIn("helloworld.py", called_cmd.lower())
        self.assertIn("--flag", called_cmd.lower())

    @patch("subprocess.Popen")
    def test_execute_file_uses_workspace_root_and_restores_startup_initial_dir(self, popen_mock):
        root = Path.cwd()
        initial_dir = root / "tests"
        self.agent.workspace_root = root
        self.agent.startup_initial_directory = initial_dir
        self.agent.work_directory = initial_dir
        proc = types.SimpleNamespace(wait=lambda: 0)
        popen_mock.return_value = proc

        ok = self.agent._execute_file_directly("python helloworld.py")
        self.assertTrue(ok)
        self.assertEqual(self.agent.work_directory, initial_dir)
        self.assertEqual(popen_mock.call_args.kwargs.get("cwd"), str(root))
        self.assertEqual(popen_mock.call_args.kwargs.get("stdin"), subprocess.DEVNULL)

    def test_print_direct_shell_command_feedback_erases_and_prints_you_ran(self):
        class _TtyBuffer(StringIO):
            def isatty(self):
                return True

        buf = _TtyBuffer()
        with patch("src.smart_shell_agent.sys.stdout", buf):
            self.agent._print_direct_shell_command_feedback("git status")

        out = buf.getvalue()
        ansi_re = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
        out_plain = ansi_re.sub("", out)
        self.assertIn("\x1b[1A\r\x1b[2K\r", out)
        self.assertIn("You ran ", out)
        self.assertIn("git status", out_plain)
        self.assertIn("•", out)
        self.assertIn("\x1b[", out)

    def test_direct_shell_output_stream_indents_lines(self):
        class _TtyBuffer(StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return 1

        out_buf = _TtyBuffer()
        err_buf = _TtyBuffer()
        with patch("src.smart_shell_agent.sys.stdout", out_buf), patch(
            "src.smart_shell_agent.sys.stderr", err_buf
        ):
            out_stream, err_stream = self.agent._create_direct_shell_output_streams()
            out_stream.write("line1\nline2\n")
            err_stream.write("err-line\n")

        ansi_re = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
        out_plain = ansi_re.sub("", out_buf.getvalue()).lstrip("\r")
        err_plain = ansi_re.sub("", err_buf.getvalue())
        self.assertEqual(out_plain, "  └ line1\n    line2\n")
        self.assertEqual(err_plain, "    err-line\n")

    def test_direct_shell_output_stream_wraps_long_lines_with_indent(self):
        class _TtyBuffer(StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return 1

        out_buf = _TtyBuffer()
        with patch("src.smart_shell_agent.sys.stdout", out_buf), patch(
            "src.smart_shell_agent.SmartShellAgent._DirectShellOutputStream._terminal_columns",
            return_value=10,
        ):
            out_stream, _ = self.agent._create_direct_shell_output_streams()
            out_stream.write("12345678901")

        ansi_re = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
        out_plain = ansi_re.sub("", out_buf.getvalue()).lstrip("\r")
        self.assertEqual(out_plain, "  └ 123456\n    78901")

    def test_direct_shell_history_output_can_force_continuation_indent(self):
        class _TtyBuffer(StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return 1

        out_buf = _TtyBuffer()
        err_buf = _TtyBuffer()
        with patch("src.smart_shell_agent.sys.stdout", out_buf), patch(
            "src.smart_shell_agent.sys.stderr", err_buf
        ):
            self.agent._print_direct_shell_history_output(
                "command aborted by user\n",
                "",
                force_first_line_continuation=True,
            )

        ansi_re = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
        out_plain = ansi_re.sub("", out_buf.getvalue()).lstrip("\r")
        self.assertIn("    command aborted by user\n", out_plain)
        self.assertNotIn("└ command aborted by user", out_plain)

    def test_direct_shell_history_output_inside_slash_stream_does_not_emit_clear_line(self):
        class _TtyBuffer(StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return 1

        out_buf = _TtyBuffer()
        err_buf = _TtyBuffer()
        slash_out = self.agent._build_internal_slash_output_stream(out_buf, terminal_columns=80)
        slash_err = self.agent._build_internal_slash_output_stream(err_buf, terminal_columns=80)
        with patch("src.smart_shell_agent.sys.stdout", slash_out), patch(
            "src.smart_shell_agent.sys.stderr", slash_err
        ):
            self.agent._print_direct_shell_history_output("At Line:1 char:41\n", "")

        ansi_re = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
        out_plain = ansi_re.sub("", out_buf.getvalue()).lstrip("\r")
        self.assertEqual(out_plain, "    └ At Line:1 char:41\n")

    @patch("subprocess.Popen")
    def test_run_direct_shell_wraps_interrupt_monitor_and_process_registration(self, popen_mock):
        proc = types.SimpleNamespace(
            wait=lambda: 0,
            poll=lambda: None,
            pid=12345,
            stdout=None,
            stderr=None,
        )
        popen_mock.return_value = proc
        with (
            patch.object(self.agent, "_start_interrupt_monitor") as mock_start_monitor,
            patch.object(self.agent, "_stop_interrupt_monitor") as mock_stop_monitor,
            patch.object(self.agent, "_register_interruptible_process") as mock_register,
            patch.object(self.agent, "_unregister_interruptible_process") as mock_unregister,
        ):
            rc = self.agent._run_direct_shell_with_prefixed_output("echo hi", Path.cwd())
        self.assertEqual(rc, 0)
        mock_start_monitor.assert_called_once_with(cancel_task_on_interrupt=False)
        mock_stop_monitor.assert_called_once_with(cancel_task_on_interrupt=False)
        mock_register.assert_called_once_with(proc)
        mock_unregister.assert_called_once_with(proc)
        self.assertEqual(popen_mock.call_args.kwargs.get("stderr"), subprocess.STDOUT)

    def test_non_task_interrupt_does_not_set_task_interrupt_flag(self):
        self.agent._task_interrupt_requested = False
        with patch.object(self.agent, "_terminate_interruptible_processes") as mock_terminate:
            self.agent._request_task_interrupt(cancel_task=False)
        mock_terminate.assert_called_once_with()
        self.assertFalse(self.agent._task_interrupt_requested)

    def test_consume_conversation_interrupted_banner_recent_returns_true_when_fresh(self):
        self.agent._conversation_interrupt_banner_recent = True
        self.agent._conversation_interrupt_banner_recent_at = 100.0
        with patch("src.smart_shell_agent.time.monotonic", return_value=102.0):
            self.assertTrue(self.agent._consume_conversation_interrupted_banner_recent())
        self.assertFalse(bool(getattr(self.agent, "_conversation_interrupt_banner_recent", True)))
        self.assertEqual(float(getattr(self.agent, "_conversation_interrupt_banner_recent_at", -1.0)), 0.0)

    def test_consume_conversation_interrupted_banner_recent_ignores_stale_marker(self):
        self.agent._conversation_interrupt_banner_recent = True
        self.agent._conversation_interrupt_banner_recent_at = 100.0
        with patch("src.smart_shell_agent.time.monotonic", return_value=120.0):
            self.assertFalse(self.agent._consume_conversation_interrupted_banner_recent())
        self.assertFalse(bool(getattr(self.agent, "_conversation_interrupt_banner_recent", True)))
        self.assertEqual(float(getattr(self.agent, "_conversation_interrupt_banner_recent_at", -1.0)), 0.0)

    def test_poll_windows_escape_pressed_prefers_msvcrt_buffer(self):
        fake_msvcrt = types.SimpleNamespace(
            kbhit=lambda: True,
            getch=lambda: b"\x1b",
        )
        with (
            patch.object(os, "name", "nt"),
            patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
            patch.object(
                self.agent,
                "_poll_windows_escape_pressed_async_fallback",
                return_value=False,
            ) as mock_async_fallback,
        ):
            self.assertTrue(self.agent._poll_windows_escape_pressed())
        mock_async_fallback.assert_not_called()

    def test_poll_windows_escape_pressed_async_fallback_detects_press_edge_once(self):
        state_values = [0x8000, 0x8000, 0x0000]

        def _get_async_key_state(_vk):
            return state_values.pop(0) if state_values else 0

        fake_user32 = types.SimpleNamespace(
            GetAsyncKeyState=_get_async_key_state,
            GetForegroundWindow=lambda: 100,
        )
        fake_kernel32 = types.SimpleNamespace(GetConsoleWindow=lambda: 100)
        fake_ctypes = types.SimpleNamespace(
            windll=types.SimpleNamespace(user32=fake_user32, kernel32=fake_kernel32)
        )
        with patch.object(os, "name", "nt"), patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            self.assertTrue(self.agent._poll_windows_escape_pressed_async_fallback())
            self.assertFalse(self.agent._poll_windows_escape_pressed_async_fallback())
            self.assertFalse(self.agent._poll_windows_escape_pressed_async_fallback())

    def test_is_direct_shell_result_aborted_supports_legacy_stdout_marker(self):
        self.assertTrue(
            self.agent._is_direct_shell_result_aborted({"stdout": "...\ncommand aborted by user\n"})
        )
        self.assertFalse(self.agent._is_direct_shell_result_aborted({"stdout": "ok\n"}))

    def test_normalize_aborted_stdout_moves_abort_marker_to_tail(self):
        raw = (
            "Version: Platform(x86/x64): Branch name: command aborted by user\n"
            "02:00:41 [Info] Searching artifacts...\n"
        )
        normalized = self.agent._normalize_aborted_direct_shell_stdout_for_history(raw)
        self.assertIn("Version: Platform(x86/x64): Branch name:\n", normalized)
        self.assertIn("02:00:41 [Info] Searching artifacts...\n", normalized)
        self.assertTrue(normalized.endswith("command aborted by user\n"))

    @patch("subprocess.Popen")
    def test_run_direct_shell_appends_abort_line_when_process_was_forced_terminated(self, popen_mock):
        proc = types.SimpleNamespace(
            wait=lambda: 137,
            poll=lambda: None,
            pid=22334,
            stdout=None,
            stderr=None,
        )
        popen_mock.return_value = proc

        class _TtyBuffer(StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return 1

        out_buf = _TtyBuffer()
        err_buf = _TtyBuffer()
        with (
            patch("src.smart_shell_agent.sys.stdout", out_buf),
            patch("src.smart_shell_agent.sys.stderr", err_buf),
            patch.object(self.agent, "_consume_process_aborted", return_value=True),
        ):
            rc = self.agent._run_direct_shell_with_prefixed_output("echo hi", Path.cwd())
        self.assertEqual(rc, 137)
        self.assertIn("command aborted by user", out_buf.getvalue())
        ansi_re = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
        out_plain = ansi_re.sub("", out_buf.getvalue())
        self.assertIn("    command aborted by user", out_plain)
        self.assertNotIn("└ command aborted by user", out_plain)
        self.assertIn(
            "■ Conversation interrupted - tell the model what to do differently. Something went wrong?",
            out_buf.getvalue(),
        )
        last = getattr(self.agent, "_last_direct_shell_execution", {})
        self.assertIn("command aborted by user", str(last.get("stdout") or ""))
        self.assertGreaterEqual(int(last.get("rendered_output_lines") or 0), 4)
        self.assertTrue(bool(last.get("cursor_at_line_start", False)))

    @patch("subprocess.Popen")
    def test_run_direct_shell_abort_line_is_newline_separated_in_history_when_prev_output_has_no_newline(
        self, popen_mock
    ):
        class _FakePipe:
            def __init__(self, chunks):
                self._chunks = list(chunks)

            def read(self, _n):
                if self._chunks:
                    return self._chunks.pop(0)
                return b""

            def close(self):
                return None

        proc = types.SimpleNamespace(
            wait=lambda: 137,
            poll=lambda: None,
            pid=33445,
            stdout=_FakePipe([b"Version: Platform(x86/x64): Branch name: "]),
            stderr=None,
        )
        popen_mock.return_value = proc

        class _TtyBuffer(StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return 1

        with (
            patch("src.smart_shell_agent.sys.stdout", _TtyBuffer()),
            patch("src.smart_shell_agent.sys.stderr", _TtyBuffer()),
            patch.object(self.agent, "_consume_process_aborted", return_value=True),
        ):
            rc = self.agent._run_direct_shell_with_prefixed_output("echo hi", Path.cwd())

        self.assertEqual(rc, 137)
        last = getattr(self.agent, "_last_direct_shell_execution", {})
        out_text = str(last.get("stdout") or "")
        self.assertIn(
            "Version: Platform(x86/x64): Branch name: \ncommand aborted by user\n",
            out_text,
        )

    @patch("subprocess.Popen")
    def test_run_direct_shell_abort_line_stays_last_when_reader_finishes_late(self, popen_mock):
        class _DelayedPipe:
            def __init__(self):
                self._idx = 0

            def read(self, _n):
                self._idx += 1
                if self._idx == 1:
                    return b"Version: Platform(x86/x64): Branch name:\n"
                if self._idx == 2:
                    time.sleep(1.15)
                    return b"02:01:12 [Info] Searching artifacts...\n"
                return b""

            def close(self):
                return None

        proc = types.SimpleNamespace(
            wait=lambda: 137,
            poll=lambda: None,
            pid=44556,
            stdout=_DelayedPipe(),
            stderr=None,
        )
        popen_mock.return_value = proc

        class _TtyBuffer(StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return 1

        with (
            patch("src.smart_shell_agent.sys.stdout", _TtyBuffer()),
            patch("src.smart_shell_agent.sys.stderr", _TtyBuffer()),
            patch.object(self.agent, "_consume_process_aborted", return_value=True),
        ):
            rc = self.agent._run_direct_shell_with_prefixed_output("echo hi", Path.cwd())

        self.assertEqual(rc, 137)
        out_text = str(getattr(self.agent, "_last_direct_shell_execution", {}).get("stdout") or "")
        self.assertIn("02:01:12 [Info] Searching artifacts...\ncommand aborted by user\n", out_text)
        self.assertTrue(out_text.rstrip().endswith("command aborted by user"))


if __name__ == "__main__":
    unittest.main()
