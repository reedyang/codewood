import tempfile
import unittest
from pathlib import Path

from src.policy.path_policy import PathPolicy


class _DummyAgent:
    def __init__(self, *, work_directory: Path, workspace_root: Path, self_repo_root: Path):
        self.work_directory = work_directory
        self.workspace_root = workspace_root
        self._self_repo_root = self_repo_root
        self.ai_workspace_dir = workspace_root / ".smart-shell"
        self.config_dir = workspace_root / ".config"

    def _shell_execution_cwd(self):
        return self.workspace_root


class PathPolicyShellGuardTests(unittest.TestCase):
    def test_shell_guard_uses_effective_shell_cwd_for_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            repo_root = root / "smart-shell"
            repo_root.mkdir(parents=True, exist_ok=True)
            external_workspace = root / "tmp-workspace"
            external_workspace.mkdir(parents=True, exist_ok=True)
            # Simulate stale work_directory still under smart-shell root.
            stale_work_directory = repo_root

            agent = _DummyAgent(
                work_directory=stale_work_directory,
                workspace_root=external_workspace,
                self_repo_root=repo_root,
            )
            policy = PathPolicy(agent)

            decision = policy.can_run_shell_in_workdir(
                is_dependency_install=False,
                is_ai_workspace_script=False,
            )

        self.assertTrue(decision.get("allowed"))

    def test_shell_guard_still_blocks_when_effective_shell_cwd_under_repo(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            repo_root = root / "smart-shell"
            repo_root.mkdir(parents=True, exist_ok=True)

            agent = _DummyAgent(
                work_directory=repo_root,
                workspace_root=repo_root,
                self_repo_root=repo_root,
            )
            policy = PathPolicy(agent)

            decision = policy.can_run_shell_in_workdir(
                is_dependency_install=False,
                is_ai_workspace_script=False,
            )

        self.assertFalse(decision.get("allowed"))
        self.assertIn("已拦截 shell 命令", str(decision.get("error") or ""))


if __name__ == "__main__":
    unittest.main()
