import unittest

from src.main import _parse_startup_cli_args


class MainCliArgsTests(unittest.TestCase):
    def test_empty_args(self):
        parsed, err = _parse_startup_cli_args([])
        self.assertIsNone(err)
        self.assertEqual(parsed.get("workspace_selector"), None)
        self.assertEqual(parsed.get("exec_task"), None)
        self.assertEqual(parsed.get("model_selector"), None)

    def test_workspace_long_option(self):
        parsed, err = _parse_startup_cli_args(["--workspace", "my-workspace"])
        self.assertIsNone(err)
        self.assertEqual(parsed.get("workspace_selector"), "my-workspace")
        self.assertEqual(parsed.get("exec_task"), None)

    def test_workspace_short_option(self):
        parsed, err = _parse_startup_cli_args(["-w", "my-workspace"])
        self.assertIsNone(err)
        self.assertEqual(parsed.get("workspace_selector"), "my-workspace")
        self.assertEqual(parsed.get("exec_task"), None)

    def test_workspace_missing_value(self):
        parsed, err = _parse_startup_cli_args(["-w"])
        self.assertIsNone(parsed)
        self.assertIn("Missing workspace name", err or "")

    def test_bare_positional_is_unsupported(self):
        parsed, err = _parse_startup_cli_args(["my-workspace"])
        self.assertIsNone(parsed)
        self.assertIn("Unsupported arguments", err or "")

    def test_exec_only(self):
        parsed, err = _parse_startup_cli_args(["exec", "fix build issue"])
        self.assertIsNone(err)
        self.assertEqual(parsed.get("workspace_selector"), None)
        self.assertEqual(parsed.get("exec_task"), "fix build issue")

    def test_workspace_plus_exec(self):
        parsed, err = _parse_startup_cli_args(
            ["--workspace", "my-workspace", "exec", "run quick check now"]
        )
        self.assertIsNone(err)
        self.assertEqual(parsed.get("workspace_selector"), "my-workspace")
        self.assertEqual(parsed.get("exec_task"), "run quick check now")

    def test_model_and_exec(self):
        parsed, err = _parse_startup_cli_args(
            ["exec", "do something", "--model", "openai:gpt-4o-mini"]
        )
        self.assertIsNone(err)
        self.assertEqual(parsed.get("exec_task"), "do something")
        self.assertEqual(parsed.get("model_selector"), "openai:gpt-4o-mini")

    def test_model_short_option(self):
        parsed, err = _parse_startup_cli_args(["-m", "qwen2.5-coder:7b"])
        self.assertIsNone(err)
        self.assertEqual(parsed.get("model_selector"), "qwen2.5-coder:7b")

    def test_exec_missing_task(self):
        parsed, err = _parse_startup_cli_args(["exec"])
        self.assertIsNone(parsed)
        self.assertIn("Missing task text", err or "")

    def test_help_flag(self):
        parsed, err = _parse_startup_cli_args(["--help"])
        self.assertIsNone(err)
        self.assertEqual(bool(parsed.get("show_help")), True)

    def test_help_short_flag(self):
        parsed, err = _parse_startup_cli_args(["-h"])
        self.assertIsNone(err)
        self.assertEqual(bool(parsed.get("show_help")), True)

    def test_hidden_executable_name(self):
        parsed, err = _parse_startup_cli_args(
            ["--executable-name", "start.bat", "exec", "do work", "-m", "gpt-4o-mini"]
        )
        self.assertIsNone(err)
        self.assertEqual(parsed.get("executable_name"), "start.bat")
        self.assertEqual(parsed.get("exec_task"), "do work")
        self.assertEqual(parsed.get("model_selector"), "gpt-4o-mini")


if __name__ == "__main__":
    unittest.main()
