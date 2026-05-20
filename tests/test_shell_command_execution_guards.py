import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.actions.command_actions import action_shell_command
from src.actions.command_actions import parse_shell_invoked_script_path
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
        self.execution_policy = "confirmation"
        self.skills = []
        self._ephemeral_script_paths = set()
        self._ai_created_path_keys = set()
        self._manual_confirm_required_shell_once = False
        self.prompt_calls = []
        self.prompt_result = True
        self.allowlist_hit = False

    def _workspace_relative_script_triple(self, p: Path):
        return (self.work_directory / p, self.ai_workspace_dir / p, self.ai_workspace_dir / p)

    def _get_path_policy(self):
        return _Policy()

    def _load_confirm_allowlist(self):
        return None

    def _shell_command_in_allowlist(self, _command: str) -> bool:
        return self.allowlist_hit

    def _prompt_confirm_yes_no_maybe_always(self, _prompt, **kwargs):
        self.prompt_calls.append(kwargs)
        return self.prompt_result

    def _shell_confirm_should_offer_always(self, _command: str) -> bool:
        return True

    def _register_shell_output_for_auto_hide(self, _stdout_text, _stderr_text=""):
        return None

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


class ShellCommandExecutionGuardsTests(unittest.TestCase):
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
        self.assertEqual(result.get("error"), "用户取消了操作")
        self.assertEqual(len(agent.prompt_calls), 1)
        self.assertTrue(bool(agent.prompt_calls[0].get("offer_always", False)))
        self.assertFalse(bool(agent._manual_confirm_required_shell_once))

    def test_moderate_mode_manual_confirm_ignores_allowlist(self):
        agent = _DummyAgent()
        agent.execution_policy = "moderate"
        agent.allowlist_hit = False
        agent.prompt_result = False

        result = action_shell_command(agent, 'python -c "print(1)"', confirmed=False, interactive=True, input_data=None)

        self.assertFalse(result.get("success", True))
        self.assertEqual(result.get("error"), "用户取消了操作")
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

    def test_manual_confirm_marker_still_requires_prompt_when_confirmed_true(self):
        agent = _DummyAgent()
        agent.allowlist_hit = False
        agent.prompt_result = False
        agent._manual_confirm_required_shell_once = True
        command = 'python -c "print(42)"'

        result = action_shell_command(agent, command, confirmed=True, interactive=True, input_data=None)

        self.assertFalse(result.get("success", True))
        self.assertEqual(result.get("error"), "用户取消了操作")
        self.assertEqual(len(agent.prompt_calls), 1)
        self.assertTrue(bool(agent.prompt_calls[0].get("offer_always", False)))

    def test_interactive_stream_writes_output_immediately(self):
        agent = _DummyAgent()
        command = 'python -c "print(123)"'
        writes = []

        def _capture_write(text, _stream, append_newline=False):
            writes.append(str(text))
            return None

        with patch("subprocess.Popen", _FakePopen), patch("threading.Thread", _ImmediateThread), patch(
            "src.actions.command_actions._safe_console_write",
            side_effect=_capture_write,
        ):
            result = action_shell_command(agent, command, confirmed=False, interactive=True, input_data=None)

        self.assertTrue(result.get("success", False))
        self.assertIn("stream-line", "".join(writes))
        self.assertEqual("".join(writes).count("stream-line"), 1)

    def test_interactive_stream_appends_newline_boundary_when_output_has_no_trailing_newline(self):
        agent = _DummyAgent()
        command = 'python -c "import sys; sys.stdout.write(\'x\')"'
        writes = []

        def _capture_write(text, _stream, append_newline=False):
            writes.append(str(text))
            return None

        with patch("subprocess.Popen", _FakePopenNoTrailingNewline), patch("threading.Thread", _ImmediateThread), patch(
            "src.actions.command_actions._safe_console_write",
            side_effect=_capture_write,
        ):
            result = action_shell_command(agent, command, confirmed=False, interactive=True, input_data=None)

        self.assertTrue(result.get("success", False))
        self.assertIn("stream-no-nl", "".join(writes))
        self.assertIn("\n", writes)

    def test_interactive_stream_replays_when_tail_truncated(self):
        agent = _DummyAgent()
        command = 'python -c "print(123)"'
        writes = []

        def _capture_write(text, _stream, append_newline=False):
            writes.append(str(text))
            return None

        with patch("subprocess.Popen", _FakePopenMultiLine), patch("threading.Thread", _ImmediateThread), patch(
            "src.actions.command_actions._dynamic_tail_line_limit",
            return_value=2,
        ), patch(
            "src.actions.command_actions._safe_console_write",
            side_effect=_capture_write,
        ):
            result = action_shell_command(agent, command, confirmed=False, interactive=True, input_data=None)

        self.assertTrue(result.get("success", False))
        self.assertIn("... omitted", "".join(writes))


if __name__ == "__main__":
    unittest.main()
