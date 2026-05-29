import unittest
from pathlib import Path
from unittest.mock import patch

from src.actions.command_actions import _enforce_windows_powershell_command_prefix
from src.actions.command_actions import enforce_workspace_rg_for_shell_command
from src.actions.command_actions import normalize_shell_command_for_summary


class ShellCommandPolicyTests(unittest.TestCase):
    class _DummyAgent:
        def __init__(self, repo_root: str):
            self._self_repo_root = repo_root

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

    def test_enforce_workspace_rg_for_shell_command_rewrites_plain_rg(self):
        agent = self._DummyAgent("D:/repo")
        with patch("src.actions.command_actions.os.name", "nt"), patch(
            "src.actions.command_actions._workspace_rg_executable_path",
            return_value=Path("D:/repo/bin/rg.exe"),
        ):
            rewritten = enforce_workspace_rg_for_shell_command(agent, "rg -n TODO src")
        self.assertTrue(rewritten.lower().startswith('"d:\\repo\\bin\\rg.exe"') or rewritten.lower().startswith("d:\\repo\\bin\\rg.exe"))
        self.assertIn("-n TODO src", rewritten)

    def test_enforce_workspace_rg_for_shell_command_rewrites_powershell_wrapped_rg(self):
        agent = self._DummyAgent("D:/repo")
        with patch("src.actions.command_actions.os.name", "nt"), patch(
            "src.actions.command_actions._workspace_rg_executable_path",
            return_value=Path("D:/repo/bin/rg.exe"),
        ):
            rewritten = enforce_workspace_rg_for_shell_command(
                agent,
                'powershell -ExecutionPolicy Bypass -Command "rg -n TODO src"',
            )
        self.assertIn("powershell -ExecutionPolicy Bypass -Command", rewritten)
        self.assertIn("D:\\repo\\bin\\rg.exe", rewritten)

    def test_normalize_shell_command_for_summary_hides_rg_executable_path(self):
        with patch("src.actions.command_actions.os.name", "nt"):
            summary = normalize_shell_command_for_summary(
                'D:\\repo\\bin\\rg.exe -n TODO src'
            )
        self.assertEqual(summary, "rg -n TODO src")

    def test_normalize_shell_command_for_summary_hides_rg_path_in_powershell_payload(self):
        with patch("src.actions.command_actions.os.name", "nt"):
            summary = normalize_shell_command_for_summary(
                'powershell -ExecutionPolicy Bypass -Command "D:\\repo\\bin\\rg.exe -n TODO src"'
            )
        self.assertIn("powershell -ExecutionPolicy Bypass -Command", summary)
        self.assertIn("rg -n TODO src", summary)
        self.assertNotIn("D:\\repo\\bin\\rg.exe", summary)


if __name__ == "__main__":
    unittest.main()
