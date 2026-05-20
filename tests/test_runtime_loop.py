import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.runtime.runtime_loop import (
    _build_minimal_verification_command,
    _format_startup_directory,
    _shell_command_indicates_verification,
    _tool_change_and_verification_hints,
)


class RuntimeLoopTests(unittest.TestCase):
    def test_format_startup_directory_replaces_user_home_with_tilde(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            fake_home = base / "home_user"
            inside_path = fake_home / "projects" / "demo"
            outside_path = base / "outside" / "demo"
            inside_path.mkdir(parents=True, exist_ok=True)
            outside_path.parent.mkdir(parents=True, exist_ok=True)

            with patch("src.runtime.runtime_loop.Path.home", return_value=fake_home):
                self.assertEqual(
                    _format_startup_directory(str(inside_path)),
                    f"~{os.sep}projects{os.sep}demo",
                )
                self.assertEqual(_format_startup_directory(str(fake_home)), "~")
                self.assertEqual(
                    _format_startup_directory(str(outside_path)),
                    str(outside_path),
                )

    def test_shell_command_indicates_verification(self):
        self.assertTrue(_shell_command_indicates_verification("pytest -q"))
        self.assertTrue(_shell_command_indicates_verification("python -m py_compile a.py"))
        self.assertFalse(_shell_command_indicates_verification("echo hello"))

    def test_build_minimal_verification_command_prefers_py_compile(self):
        cmd = _build_minimal_verification_command(["a.py", "b.txt"])
        self.assertIn("python -m py_compile", cmd)
        self.assertIn("a.py", cmd)

    def test_tool_change_and_verification_hints(self):
        hints_change = _tool_change_and_verification_hints(
            "apply_patch",
            {"path": "helloworld.py"},
            {"success": True},
        )
        self.assertTrue(bool(hints_change.get("code_changed")))
        self.assertIn("helloworld.py", hints_change.get("changed_files") or [])

        hints_verify = _tool_change_and_verification_hints(
            "shell",
            {"command": "pytest -q"},
            {"success": True},
        )
        self.assertTrue(bool(hints_verify.get("verified")))


if __name__ == "__main__":
    unittest.main()
