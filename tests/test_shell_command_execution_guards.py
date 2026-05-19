import unittest
from pathlib import Path
from unittest.mock import patch

from src.actions.command_actions import action_shell_command


class _Policy:
    def can_run_shell_in_workdir(self, **_kwargs):
        return {"allowed": True}


class _DummyAgent:
    def __init__(self):
        self.work_directory = Path.cwd()
        self.ai_workspace_dir = Path.cwd()
        self.skills = []
        self._ephemeral_script_paths = set()
        self._ai_created_path_keys = set()
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


class ShellCommandExecutionGuardsTests(unittest.TestCase):
    def test_skillhub_install_requires_prompt_even_if_allowlisted(self):
        agent = _DummyAgent()
        agent.allowlist_hit = True
        agent.prompt_result = False
        script = (Path.cwd() / "skills" / "skillhub-skill-installer" / "scripts" / "skillhub_installer.py").resolve()
        command = (
            f'python "{script}" install --query "gmail" '
            f'--config-dir "{Path.cwd()}"'
        )

        result = action_shell_command(agent, command, confirmed=False, interactive=True, input_data=None)

        self.assertFalse(result.get("success", True))
        self.assertEqual(result.get("error"), "用户取消了操作")
        self.assertEqual(len(agent.prompt_calls), 1)
        self.assertFalse(bool(agent.prompt_calls[0].get("offer_always", True)))

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


if __name__ == "__main__":
    unittest.main()
