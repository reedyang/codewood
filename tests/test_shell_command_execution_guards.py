import unittest
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch

from src.actions.command_actions import action_shell_command
from src.actions.command_actions import parse_shell_invoked_script_path
from src.core.security.command_security import shell_command_in_allowlist
from src.core.security.command_security import shell_executable_allowlist_key
from src.core.security.command_security import shell_script_allowlist_key
from src.services.execution_policy_service import freedom_auto_confirm


class _Policy:
    def can_run_shell_in_workdir(self, **_kwargs):
        return {"allowed": True}


class _DummyAgent:
    def __init__(self):
        self.work_directory = Path.cwd()
        self.ai_workspace_dir = Path.cwd()
        self.workspace_root = Path.cwd()
        self.startup_initial_directory = Path.cwd()
        self.execution_policy = "confirmation"
        self.skills = []
        self._ephemeral_script_paths = set()
        self._ai_created_path_keys = set()
        self._manual_confirm_required_shell_once = False
        self.prompt_calls = []
        self.prompt_result = True
        self.allowlist_hit = False
        self.reset_calls = 0
        self.auto_hide_register_calls = 0
        self._allowlist_shell_paths = {}
        self._allowlist_shell_exes = set()
        self._confirm_allowlist_salt = ""

    def _workspace_relative_script_triple(self, p: Path):
        return (self.work_directory / p, self.ai_workspace_dir / p, self.ai_workspace_dir / p)

    def _get_path_policy(self):
        return _Policy()

    def _load_confirm_allowlist(self):
        return None

    def _shell_command_in_allowlist(self, _command: str) -> bool:
        return self.allowlist_hit or shell_command_in_allowlist(self, _command)

    def _prompt_confirm_yes_no_maybe_always(self, _prompt, **kwargs):
        self.prompt_calls.append(kwargs)
        return self.prompt_result

    def _shell_confirm_should_offer_always(self, _command: str) -> bool:
        return True

    def _register_shell_output_for_auto_hide(self, _stdout_text, _stderr_text=""):
        self.auto_hide_register_calls += 1
        return None

    def _shell_execution_cwd(self):
        return self.workspace_root

    def _reset_work_directory_to_startup_initial(self):
        self.reset_calls += 1

    def _is_workspace_skill_path(self, _path: Path) -> bool:
        return False

    def _reload_skills_if_workspace_skill_changed(self, _paths):
        return None

    def _ephemeral_path_key(self, path: Path) -> str:
        return str(path.resolve())

    def _is_path_under(self, path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except Exception:
            return False

    def _parse_shell_invoked_script_path(self, command: str):
        from src.actions.command_actions import parse_shell_invoked_script_path

        return parse_shell_invoked_script_path(self, command)


class _ImmediateThread:
    def __init__(self, target, args=(), daemon=None):
        self._target = target
        self._args = args
        self.daemon = daemon

    def start(self):
        self._target(*self._args)

    def join(self, timeout=None):
        return None


class _FakePipe:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def read(self, _n):
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def close(self):
        return None


class _FakePopen:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.stdout = _FakePipe([b"stream-line\n"])
        self.stderr = _FakePipe([])

    def wait(self):
        return 0


class _FakePopenNoTrailingNewline:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.stdout = _FakePipe([b"stream-no-nl"])
        self.stderr = _FakePipe([])

    def wait(self):
        return 0


class _FakePopenMultiLine:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.stdout = _FakePipe([b"line-1\nline-2\nline-3\n"])
        self.stderr = _FakePipe([])

    def wait(self):
        return 0


class _FakeCompleted:
    def __init__(self, stdout_text: str, stderr_text: str = "", return_code: int = 0):
        self.returncode = int(return_code)
        self.stdout = stdout_text.encode("utf-8")
        self.stderr = stderr_text.encode("utf-8")


class ShellCommandExecutionGuardsTests(unittest.TestCase):
    def _assert_cancelled_error(self, result):
        self.assertIn(
            result.get("error"),
            {"用户取消了操作", "Operation cancelled by user"},
        )

    def test_parse_shell_invoked_script_path_unwraps_powershell(self):
        agent = _DummyAgent()
        tf = tempfile.NamedTemporaryFile(suffix=".py", delete=False)
        tf.close()
        script_path = Path(tf.name).resolve()
        command = (
            f'powershell -ExecutionPolicy Bypass -Command '
            f'"python \\"{script_path}\\" --query \\"gmail\\""'
        )
        try:
            parsed = parse_shell_invoked_script_path(agent, command)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.resolve(), script_path)
        finally:
            script_path.unlink(missing_ok=True)

    def test_shell_script_allowlist_key_uses_inner_script_when_powershell_wrapped(self):
        agent = _DummyAgent()
        tf = tempfile.NamedTemporaryFile(suffix=".py", delete=False)
        tf.close()
        script_path = Path(tf.name).resolve()
        command = (
            f'powershell -ExecutionPolicy Bypass -Command '
            f'"python \\"{script_path}\\" install --confirm \\"YES\\""'
        )
        try:
            key = shell_script_allowlist_key(agent, command)
            self.assertEqual(key, str(script_path).lower())
        finally:
            script_path.unlink(missing_ok=True)

    def test_parse_shell_invoked_script_path_unwraps_cmd_pwsh_python(self):
        agent = _DummyAgent()
        tf = tempfile.NamedTemporaryFile(suffix=".py", delete=False)
        tf.close()
        script_path = Path(tf.name).resolve()
        command = f'cmd /c pwsh -c "python \\"{script_path}\\" --flag 1"'
        try:
            parsed = parse_shell_invoked_script_path(agent, command)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.resolve(), script_path)
        finally:
            script_path.unlink(missing_ok=True)

    def test_parse_shell_invoked_script_path_unwraps_bash_lc_python(self):
        agent = _DummyAgent()
        tf = tempfile.NamedTemporaryFile(suffix=".py", delete=False)
        tf.close()
        script_path = Path(tf.name).resolve()
        command = f"bash -lc 'python \"{script_path.as_posix()}\" --query demo'"
        try:
            with patch("src.actions.command_actions.os.name", "posix"):
                parsed = parse_shell_invoked_script_path(agent, command)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.resolve(), script_path)
        finally:
            script_path.unlink(missing_ok=True)

    def test_parse_shell_invoked_script_path_unwraps_env_prefix(self):
        agent = _DummyAgent()
        tf = tempfile.NamedTemporaryFile(suffix=".py", delete=False)
        tf.close()
        script_path = Path(tf.name).resolve()
        command = f'env FOO=bar python "{script_path.as_posix()}" --check'
        try:
            with patch("src.actions.command_actions.os.name", "posix"):
                parsed = parse_shell_invoked_script_path(agent, command)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.resolve(), script_path)
        finally:
            script_path.unlink(missing_ok=True)

    def test_parse_shell_invoked_script_path_recognizes_node_js_script(self):
        agent = _DummyAgent()
        tf = tempfile.NamedTemporaryFile(suffix=".js", delete=False)
        tf.close()
        script_path = Path(tf.name).resolve()
        command = f'node "{script_path}" --mode prod'
        try:
            parsed = parse_shell_invoked_script_path(agent, command)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.resolve(), script_path)
        finally:
            script_path.unlink(missing_ok=True)

    def test_shell_script_allowlist_key_uses_inner_script_when_bash_wrapped(self):
        agent = _DummyAgent()
        tf = tempfile.NamedTemporaryFile(suffix=".py", delete=False)
        tf.close()
        script_path = Path(tf.name).resolve()
        command = f"bash -c 'python \"{script_path.as_posix()}\" install --confirm YES'"
        try:
            with patch("src.actions.command_actions.os.name", "posix"), patch(
                "src.core.security.command_security.os.name", "posix"
            ):
                key = shell_script_allowlist_key(agent, command)
            self.assertEqual(key, str(script_path))
        finally:
            script_path.unlink(missing_ok=True)

    def test_shell_executable_allowlist_key_unwraps_powershell_command(self):
        agent = _DummyAgent()
        command = 'powershell -ExecutionPolicy Bypass -Command "git status --short"'
        key = shell_executable_allowlist_key(agent, command)
        self.assertEqual(key, "git")

    def test_parse_shell_invoked_script_path_unwraps_pwsh_file(self):
        agent = _DummyAgent()
        tf = tempfile.NamedTemporaryFile(suffix=".ps1", delete=False)
        tf.close()
        script_path = Path(tf.name).resolve()
        command = f'pwsh -File "{script_path}" -Task Build'
        try:
            parsed = parse_shell_invoked_script_path(agent, command)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.resolve(), script_path)
        finally:
            script_path.unlink(missing_ok=True)

    def test_parse_shell_invoked_script_path_unwraps_env_s_sudo_pwsh_file(self):
        agent = _DummyAgent()
        tf = tempfile.NamedTemporaryFile(suffix=".ps1", delete=False)
        tf.close()
        script_path = Path(tf.name).resolve()
        command = (
            f'env -S "sudo -u root pwsh -File {script_path.as_posix()} --mode dry-run"'
        )
        try:
            with patch("src.actions.command_actions.os.name", "posix"):
                parsed = parse_shell_invoked_script_path(agent, command)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.resolve(), script_path)
        finally:
            script_path.unlink(missing_ok=True)

    def test_parse_shell_invoked_script_path_unwraps_sudo_u_user_python(self):
        agent = _DummyAgent()
        tf = tempfile.NamedTemporaryFile(suffix=".py", delete=False)
        tf.close()
        script_path = Path(tf.name).resolve()
        command = f'sudo -u buildbot python "{script_path.as_posix()}" --check'
        try:
            with patch("src.actions.command_actions.os.name", "posix"):
                parsed = parse_shell_invoked_script_path(agent, command)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.resolve(), script_path)
        finally:
            script_path.unlink(missing_ok=True)

    def test_parse_shell_invoked_script_path_unwraps_cmd_cscript_nologo(self):
        agent = _DummyAgent()
        tf = tempfile.NamedTemporaryFile(suffix=".vbs", delete=False)
        tf.close()
        script_path = Path(tf.name).resolve()
        command = f'cmd /c cscript //nologo "{script_path}" //B'
        try:
            parsed = parse_shell_invoked_script_path(agent, command)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.resolve(), script_path)
        finally:
            script_path.unlink(missing_ok=True)

    def test_parse_shell_invoked_script_path_unwraps_wscript_with_options(self):
        agent = _DummyAgent()
        tf = tempfile.NamedTemporaryFile(suffix=".vbs", delete=False)
        tf.close()
        script_path = Path(tf.name).resolve()
        command = f'wscript //nologo //B "{script_path}" /job:main'
        try:
            parsed = parse_shell_invoked_script_path(agent, command)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.resolve(), script_path)
        finally:
            script_path.unlink(missing_ok=True)

    def test_shell_script_allowlist_key_uses_inner_script_when_env_s_nested(self):
        agent = _DummyAgent()
        tf = tempfile.NamedTemporaryFile(suffix=".ps1", delete=False)
        tf.close()
        script_path = Path(tf.name).resolve()
        command = (
            f'env -S "sudo -u root pwsh -File {script_path.as_posix()} --sync"'
        )
        try:
            with patch("src.actions.command_actions.os.name", "posix"), patch(
                "src.core.security.command_security.os.name", "posix"
            ):
                key = shell_script_allowlist_key(agent, command)
            self.assertEqual(key, str(script_path))
        finally:
            script_path.unlink(missing_ok=True)

    def test_freedom_auto_confirm_marks_manual_when_ai_says_not_reversible(self):
        agent = _DummyAgent()
        agent.execution_policy = "moderate"

        with patch("src.services.execution_policy_service.ai_assess_reversible", return_value=(False, "not reversible")), patch(
            "src.services.execution_policy_service._print_with_auto_hide_tracking"
        ), patch(
            "src.services.execution_policy_service.shell_command_in_allowlist", return_value=False
        ):
            ok = freedom_auto_confirm(
                agent,
                {"action": "shell", "params": {"command": "echo hi"}},
            )

        self.assertFalse(ok)
        self.assertTrue(bool(agent._manual_confirm_required_shell_once))

    def test_freedom_auto_confirm_skips_ai_review_when_shell_allowlisted(self):
        agent = _DummyAgent()
        agent.execution_policy = "moderate"

        with patch("src.services.execution_policy_service.ai_assess_reversible") as assess, patch(
            "src.services.execution_policy_service._print_with_auto_hide_tracking"
        ), patch(
            "src.services.execution_policy_service.shell_command_in_allowlist", return_value=True
        ):
            ok = freedom_auto_confirm(
                agent,
                {"action": "shell", "params": {"command": "echo hi"}},
            )

        self.assertTrue(ok)
        self.assertFalse(bool(agent._manual_confirm_required_shell_once))
        assess.assert_not_called()

    def test_manual_confirm_marker_forces_prompt_even_with_allowlist(self):
        agent = _DummyAgent()
        agent.allowlist_hit = False
        agent._manual_confirm_required_shell_once = True
        agent.prompt_result = False

        result = action_shell_command(agent, 'python -c "print(2)"', confirmed=False, interactive=True, input_data=None)

        self.assertFalse(result.get("success", True))
        self._assert_cancelled_error(result)
        self.assertEqual(len(agent.prompt_calls), 1)
        self.assertTrue(bool(agent.prompt_calls[0].get("offer_always", False)))
        self.assertFalse(bool(agent._manual_confirm_required_shell_once))
        self.assertEqual(agent.reset_calls, 0)

    def test_shell_command_execution_calls_startup_initial_reset(self):
        agent = _DummyAgent()

        with patch("subprocess.run", return_value=_FakeCompleted("ok\n")):
            result = action_shell_command(agent, 'python -c "print(1)"', confirmed=False, interactive=False, input_data=None)

        self.assertTrue(result.get("success", False))
        self.assertEqual(agent.reset_calls, 1)

    def test_shell_command_execution_uses_workspace_root_as_cwd(self):
        agent = _DummyAgent()
        agent.work_directory = Path.cwd() / "tests"
        agent.workspace_root = Path.cwd()

        with patch("subprocess.run", return_value=_FakeCompleted("ok\n")) as run_mock:
            result = action_shell_command(agent, 'python -c "print(1)"', confirmed=False, interactive=False, input_data=None)

        self.assertTrue(result.get("success", False))
        self.assertEqual(run_mock.call_args.kwargs.get("cwd"), str(agent.workspace_root))

    def test_moderate_mode_manual_confirm_ignores_allowlist(self):
        agent = _DummyAgent()
        agent.execution_policy = "moderate"
        agent.allowlist_hit = False
        agent.prompt_result = False

        result = action_shell_command(agent, 'python -c "print(1)"', confirmed=False, interactive=True, input_data=None)

        self.assertFalse(result.get("success", True))
        self._assert_cancelled_error(result)
        self.assertEqual(len(agent.prompt_calls), 1)
        self.assertTrue(bool(agent.prompt_calls[0].get("offer_always", False)))

    def test_allowlisted_command_skips_prompt_even_when_non_reversible(self):
        agent = _DummyAgent()
        agent.execution_policy = "unlimited"
        agent.allowlist_hit = True
        agent.prompt_result = True

        result = action_shell_command(agent, 'python -c "print(1)"', confirmed=False, interactive=True, input_data=None)

        self.assertTrue(result.get("success", False))
        self.assertEqual(len(agent.prompt_calls), 0)

    def test_workspace_read_command_skips_prompt_under_confirmation_policy(self):
        agent = _DummyAgent()
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td).resolve()
            target = ws / "demo.txt"
            target.write_text("hello", encoding="utf-8")
            agent.workspace_root = ws
            agent.work_directory = ws
            agent.ai_workspace_dir = ws
            agent.prompt_result = False

            with patch("subprocess.run", return_value=_FakeCompleted("hello\n")):
                result = action_shell_command(agent, f'type "{target}"', confirmed=False, interactive=False, input_data=None)

        self.assertTrue(result.get("success", False))
        self.assertEqual(len(agent.prompt_calls), 0)

    def test_workspace_read_command_skips_prompt_under_moderate_policy(self):
        agent = _DummyAgent()
        agent.execution_policy = "moderate"
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td).resolve()
            target = ws / "demo.txt"
            target.write_text("hello", encoding="utf-8")
            agent.workspace_root = ws
            agent.work_directory = ws
            agent.ai_workspace_dir = ws
            agent.prompt_result = False

            with patch("subprocess.run", return_value=_FakeCompleted("hello\n")):
                result = action_shell_command(agent, f'Get-Content "{target}"', confirmed=False, interactive=False, input_data=None)

        self.assertTrue(result.get("success", False))
        self.assertEqual(len(agent.prompt_calls), 0)

    def test_workspace_read_command_does_not_bypass_when_path_outside_workspace(self):
        agent = _DummyAgent()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ws = root / "ws"
            other = root / "other"
            ws.mkdir(parents=True, exist_ok=True)
            other.mkdir(parents=True, exist_ok=True)
            outside = other / "outside.txt"
            outside.write_text("x", encoding="utf-8")
            agent.workspace_root = ws
            agent.work_directory = ws
            agent.ai_workspace_dir = ws
            agent.prompt_result = False

            result = action_shell_command(agent, f'type "{outside}"', confirmed=False, interactive=False, input_data=None)

        self.assertFalse(result.get("success", True))
        self._assert_cancelled_error(result)
        self.assertEqual(len(agent.prompt_calls), 1)

    def test_manual_confirm_marker_still_requires_prompt_when_confirmed_true(self):
        agent = _DummyAgent()
        agent.allowlist_hit = False
        agent.prompt_result = False
        agent._manual_confirm_required_shell_once = True
        command = 'python -c "print(42)"'

        result = action_shell_command(agent, command, confirmed=True, interactive=True, input_data=None)

        self.assertFalse(result.get("success", True))
        self._assert_cancelled_error(result)
        self.assertEqual(len(agent.prompt_calls), 1)
        self.assertTrue(bool(agent.prompt_calls[0].get("offer_always", False)))

    def test_shell_execution_forces_non_interactive_even_when_interactive_true(self):
        agent = _DummyAgent()
        command = 'python -c "print(123)"'
        writes = []

        def _capture_write(text, _stream, append_newline=False):
            writes.append(str(text))
            return None

        with patch("subprocess.run", return_value=_FakeCompleted("stream-line\n")) as run_mock, patch(
            "src.actions.command_actions._dynamic_tail_line_limit",
            return_value=2,
        ), patch(
            "src.actions.command_actions._safe_console_write",
            side_effect=_capture_write,
        ), patch("subprocess.Popen") as popen_mock:
            result = action_shell_command(agent, command, confirmed=False, interactive=True, input_data=None)

        self.assertTrue(result.get("success", False))
        self.assertEqual(result.get("interactive"), False)
        self.assertIn("stream-line", "".join(writes))
        self.assertEqual(run_mock.call_args.kwargs.get("stdin"), subprocess.DEVNULL)
        popen_mock.assert_not_called()

    def test_non_interactive_mode_displays_only_tail_summary(self):
        agent = _DummyAgent()
        command = 'python -c "print(123)"'
        writes = []
        big_out = "\n".join(f"line{i}" for i in range(1, 80)) + "\n"

        def _capture_write(text, _stream, append_newline=False):
            writes.append(str(text))
            return None

        with patch("subprocess.run", return_value=_FakeCompleted(big_out)), patch(
            "src.actions.command_actions._dynamic_tail_line_limit", return_value=5
        ), patch(
            "src.actions.command_actions._safe_console_write",
            side_effect=_capture_write,
        ), patch("subprocess.Popen") as popen_mock:
            result = action_shell_command(agent, command, confirmed=False, interactive=False, input_data=None)

        self.assertTrue(result.get("success", False))
        self.assertIn("... omitted", "".join(writes))
        self.assertNotIn("line1\n", "".join(writes))
        popen_mock.assert_not_called()

    def test_non_interactive_mode_does_not_register_auto_hide_lines(self):
        agent = _DummyAgent()
        command = 'python -c "print(1)"'

        with patch("subprocess.run", return_value=_FakeCompleted("line1\nline2\n")), patch(
            "src.actions.command_actions._dynamic_tail_line_limit", return_value=5
        ), patch("subprocess.Popen") as popen_mock:
            result = action_shell_command(agent, command, confirmed=False, interactive=False, input_data=None)

        self.assertTrue(result.get("success", False))
        self.assertEqual(agent.auto_hide_register_calls, 0)
        popen_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
