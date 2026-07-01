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

    def _schedule_project_context_refresh_background(self, force=False, reason=""):
        self.project_context_calls += 1

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


class BootstrapTests(unittest.TestCase):
    def test_setup_runtime_services_refreshes_active_chat_usage_snapshot(self):
        agent = _FakeAgent()

        with (
            patch("cli.runtime.bootstrap.ProjectContextIndex"),
            patch("cli.runtime.bootstrap.ToolDispatcher"),
        ):
            bootstrap.setup_runtime_services(agent)

        self.assertEqual(agent.refresh_calls, 1)
        self.assertEqual(agent.persist_calls, 1)
        self.assertEqual(agent.validation_calls, 1)
        self.assertEqual(agent.memory_bg_calls, 1)
        self.assertEqual(agent.project_context_calls, 1)


if __name__ == "__main__":
    unittest.main()
