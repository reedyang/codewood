import unittest
from unittest.mock import patch

from cli.server.serve_app import _build_state_inner


class _FakeWorkspaceStateManager:
    _default_workspace_id = "ws-1"


class _FakeAgent:
    def __init__(self) -> None:
        self.workspace_id = "ws-1"
        self.workspace_name = "Workspace"
        self.workspace_root = "D:/workspace"
        self.work_directory = "D:/workspace"
        self.config_dir = "D:/workspace/.codewood"
        self.provider = "openai"
        self.model_name = "gpt-4o"
        self._workspaces_state = {
            "workspaces": {
                "ws-1": {
                    "id": "ws-1",
                    "name": "Workspace",
                    "root": "D:/workspace",
                    "kind": "default",
                }
            }
        }
        self._workspace_state_manager = _FakeWorkspaceStateManager()
        self._chat_state = {"active": "chat-1"}
        self._last_context_input_tokens = 0
        self._last_context_usage_percent = 0
        self._last_context_window = 0

    def _workspace_root_path(self, entry):
        return str(entry.get("root") or "")

    def _active_runtime_chat_ids(self):
        return []

    def _chat_entries(self):
        return [
            {
                "id": "chat-1",
                "name": "Chat 1",
                "messages": [],
                "model_provider": "HappyCoding",
                "model_name": "Gemma-4-31B-IT",
            }
        ]

    def _current_model_selector(self):
        return "openai/gpt-4o"

    def _get_configured_model_selectors(self):
        return ["HappyCoding/Gemma-4-31B-IT", "openai/gpt-4o"]

    def _load_runtime_config_data(self):
        return {}


class BuildStateModelTests(unittest.TestCase):
    def test_active_chat_model_uses_provider_slash_model_selector(self):
        agent = _FakeAgent()
        with patch("cli.server.serve_app._compute_chat_cache_stats", return_value={}):
            state = _build_state_inner(agent)

        self.assertEqual(state["model"]["current"], "HappyCoding/Gemma-4-31B-IT")
        self.assertEqual(state["chats"][0]["model"], "HappyCoding/Gemma-4-31B-IT")


if __name__ == "__main__":
    unittest.main()
