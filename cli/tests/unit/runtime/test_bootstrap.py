import unittest
from pathlib import Path
from unittest.mock import patch

from cli.runtime import bootstrap


class _FakeAgent:
    def __init__(self):
        self.workspace_root = Path("D:/tmp/workspace")
        self.workspace_config_dir = Path("D:/tmp/workspace/.codewood")
        self.params = {"context_window": 32000}
        self.active_chat_id = "chat-1"
        self.memory_service = object()
        self.refresh_calls = 0
        self.persist_calls = 0
        self.validation_calls = 0
        self.memory_bg_calls = 0
        self.project_context_calls = 0
        self.all_workspaces_project_context_calls = 0
        self.cleanup_calls = 0

    def _schedule_project_context_refresh_background(self, force=False, reason=""):
        self.project_context_calls += 1

    def _schedule_project_context_refresh_for_all_workspaces(self):
        self.all_workspaces_project_context_calls += 1

    def _schedule_model_validation_background(self):
        self.validation_calls += 1

    def _schedule_memory_service_background(self):
        self.memory_bg_calls += 1

    def _execute_tool_call_legacy(self, *_args, **_kwargs):
        return None

    def _refresh_status_context_usage_snapshot(self, user_input_hint: str = "", context_hint: str = ""):
        _ = (user_input_hint, context_hint)
        self.refresh_calls += 1

    def _persist_active_chat_usage_snapshot(self):
        self.persist_calls += 1

    def _cleanup_workspace_shell_stashes_if_needed(self):
        self.cleanup_calls += 1


class BootstrapTests(unittest.TestCase):
    def test_setup_workspace_and_history_cleans_stale_shell_stashes(self):
        agent = _FakeAgent()
        agent.config_dir = Path("D:/tmp/config")
        agent.workspace_registry_path = agent.config_dir / "workspaces.json"
        agent.display_language = "en"
        agent._workspaces_state = {}
        agent._load_workspace_state = lambda: {
            "active": "default",
            "workspaces": {
                "default": {
                    "id": "default",
                    "name": "Default",
                    "kind": "default",
                    "root": "D:/tmp/workspace",
                }
            },
        }
        agent._default_workspace_entry = lambda: {
            "id": "default",
            "name": "Default",
            "kind": "default",
            "root": "D:/tmp/workspace",
        }
        agent._apply_workspace_entry = lambda entry, _fallback_dir: None
        agent._load_chat_state = lambda create_default_chat=True: None

        with patch("cli.runtime.bootstrap.HistoryManager"), patch("cli.runtime.bootstrap.setup_app_logging"):
            bootstrap.setup_workspace_and_history(
                agent,
                startup_work_directory=Path("D:/tmp/workspace"),
                workspace_state_file="workspaces.json",
                default_workspace_id="default",
            )

        self.assertEqual(agent.cleanup_calls, 1)

    def test_setup_runtime_services_refreshes_active_chat_usage_snapshot(self):
        agent = _FakeAgent()

        with (
            patch("cli.runtime.bootstrap.ProjectContextIndex"),
            patch("cli.runtime.bootstrap.ProjectContextIndexManager"),
            patch("cli.runtime.bootstrap.ToolDispatcher"),
        ):
            bootstrap.setup_runtime_services(agent)

        self.assertEqual(agent.refresh_calls, 1)
        self.assertEqual(agent.persist_calls, 1)
        self.assertEqual(agent.validation_calls, 1)
        self.assertEqual(agent.memory_bg_calls, 1)
        self.assertEqual(agent.project_context_calls, 1)
        self.assertEqual(agent.all_workspaces_project_context_calls, 1)


if __name__ == "__main__":
    unittest.main()
