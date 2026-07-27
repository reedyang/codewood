import unittest
from pathlib import Path
from unittest.mock import patch

from cli.agent import Agent


class _FakeInputHandler:
    def update_workspace_directory(self, _value):
        return None

    def update_work_directory(self, _value):
        return None

    def reset_command_history(self, _value):
        return None


class _FakeAgentRuntime:
    def __init__(self):
        self.workspace_root = Path("D:/tmp/workspace")
        self.workspace_config_dir = Path("D:/tmp/workspace/.codewood")
        self.work_directory = Path("D:/tmp/workspace")
        self.display_language = "en"
        self.input_handler = _FakeInputHandler()
        self.mcp_config = {}
        self.config_dir = Path("D:/tmp/config")
        self._workspace_runtime_generation = 0
        self.cleanup_calls = 0
        self.history_manager = None
        self.memory_service = None

        self._allowlist_shell_paths = {}
        self._allowlist_shell_exes = set()
        self._allowlist_script = set()
        self._confirm_allowlist_salt = ""
        self._freedom_script_review_entries = {}

    def _shutdown_workspace_services(self, wait=True):
        return None

    def _cleanup_workspace_shell_stashes_if_needed(self):
        self.cleanup_calls += 1

    def _ensure_workspace_dirs(self):
        return None

    def _load_chat_state(self, create_default_chat=True):
        return None

    def _load_confirm_allowlist(self):
        return None

    def _load_freedom_script_review_cache(self):
        return None

    def _shutdown_mcp_runtime(self):
        return None

    def _handle_mcp_elicitation_create(self, *_args, **_kwargs):
        return None

    def _compose_system_prompt_snapshot(self, include_tools=False):
        _ = include_tools
        return ""

    def _reload_skills(self):
        return None

    def _update_skills_watcher_workspace(self):
        return None

    def _schedule_memory_service_background(self):
        return None

    def _schedule_project_context_refresh_background(self, force=False, reason=""):
        _ = (force, reason)
        return None


class AgentWorkspaceStashCleanupTests(unittest.TestCase):
    def test_refresh_workspace_runtime_checks_for_stale_shell_stashes(self):
        agent = _FakeAgentRuntime()

        with patch("cli.agent.HistoryManager"), patch("cli.agent.McpManager"):
            Agent._refresh_workspace_runtime(agent)

        self.assertEqual(agent.cleanup_calls, 1)


if __name__ == "__main__":
    unittest.main()
