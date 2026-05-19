import unittest

from src.actions.command_actions import (
    _count_output_lines,
    _shell_command_may_wait_user_input,
    _should_suppress_large_output,
)


class ShellOutputSuppressionTests(unittest.TestCase):
    def test_count_output_lines_handles_crlf(self):
        self.assertEqual(_count_output_lines("a\r\nb\r\n"), 2)
        self.assertEqual(_count_output_lines("a\nb\nc"), 3)
        self.assertEqual(_count_output_lines(""), 0)

    def test_shell_command_wait_input_heuristics(self):
        self.assertTrue(_shell_command_may_wait_user_input("python"))
        self.assertTrue(_shell_command_may_wait_user_input("Read-Host 'token'"))
        self.assertFalse(_shell_command_may_wait_user_input("Get-Content -Path src\\smart_shell_agent.py -Raw"))

    def test_should_suppress_large_output(self):
        self.assertTrue(_should_suppress_large_output(101, False))
        self.assertFalse(_should_suppress_large_output(100, False))
        self.assertFalse(_should_suppress_large_output(1000, True))


if __name__ == "__main__":
    unittest.main()
