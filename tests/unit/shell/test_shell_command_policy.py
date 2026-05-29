import unittest
from unittest.mock import patch

from src.actions.command_actions import _enforce_windows_powershell_command_prefix


class ShellCommandPolicyTests(unittest.TestCase):
    def test_windows_powershell_requires_bypass_command_prefix(self):
        with patch("src.actions.command_actions.os.name", "nt"):
            res = _enforce_windows_powershell_command_prefix(
                'powershell -Command "Get-ChildItem -Force"'
            )
        self.assertFalse(res.get("ok", True))
        self.assertIn("ExecutionPolicy Bypass -Command", str(res.get("error", "")))

    def test_windows_powershell_exe_is_normalized(self):
        with patch("src.actions.command_actions.os.name", "nt"):
            res = _enforce_windows_powershell_command_prefix(
                'powershell.exe -ExecutionPolicy Bypass -Command "Get-Date"'
            )
        self.assertTrue(res.get("ok"))
        self.assertEqual(
            res.get("command"),
            'powershell -ExecutionPolicy Bypass -Command "Get-Date"',
        )


if __name__ == "__main__":
    unittest.main()
