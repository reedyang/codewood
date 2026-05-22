import sys
import types
import unittest
import subprocess
import re
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
        self.assertIn("\x1b[1A\r\x1b[2K\r", out)
        self.assertIn("You ran ", out)
        self.assertIn("git status", out)
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
        out_plain = ansi_re.sub("", out_buf.getvalue())
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
        out_plain = ansi_re.sub("", out_buf.getvalue())
        self.assertEqual(out_plain, "  └ 123456\n    78901")


if __name__ == "__main__":
    unittest.main()
