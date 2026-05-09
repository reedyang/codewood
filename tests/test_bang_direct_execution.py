import sys
import types
import unittest
from unittest.mock import patch


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.smart_shell_agent import SmartShellAgent


class BangDirectExecutionTests(unittest.TestCase):
    def setUp(self):
        self.agent = SmartShellAgent.__new__(SmartShellAgent)
        self.agent.work_directory = "."

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


if __name__ == "__main__":
    unittest.main()
