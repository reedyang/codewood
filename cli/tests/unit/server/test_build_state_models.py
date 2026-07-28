import contextlib
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
        self._session_values = {
            "chat-1": {"window": 0, "tokens": 0, "percent": 0},
        }

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

    @contextlib.contextmanager
    def _session_scope(self, chat_id: str):
        prev_window = self._last_context_window
        prev_tokens = self._last_context_input_tokens
        prev_percent = self._last_context_usage_percent
        values = self._session_values.get(chat_id, {})
        self._last_context_window = int(values.get("window", 0) or 0)
        self._last_context_input_tokens = int(values.get("tokens", 0) or 0)
        self._last_context_usage_percent = int(values.get("percent", 0) or 0)
        try:
            yield
        finally:
            self._last_context_window = prev_window
            self._last_context_input_tokens = prev_tokens
            self._last_context_usage_percent = prev_percent


class BuildStateModelTests(unittest.TestCase):
    def test_active_chat_model_uses_provider_slash_model_selector(self):
        agent = _FakeAgent()
        with patch("cli.server.serve_app._compute_chat_cache_stats", return_value={}):
            state = _build_state_inner(agent)

        self.assertEqual(state["model"]["current"], "HappyCoding/Gemma-4-31B-IT")
        self.assertEqual(state["chats"][0]["model"], "HappyCoding/Gemma-4-31B-IT")

    def test_active_chat_context_usage_uses_active_chat_session_snapshot(self):
        agent = _FakeAgent()
        agent._last_context_window = 32000
        agent._last_context_input_tokens = 100
        agent._last_context_usage_percent = 1
        agent._session_values["chat-1"] = {
            "window": 128000,
            "tokens": 4096,
            "percent": 3,
        }
        with patch("cli.server.serve_app._compute_chat_cache_stats", return_value={}):
            state = _build_state_inner(agent)

        self.assertEqual(state["contextUsage"]["window"], 128000)
        self.assertEqual(state["contextUsage"]["tokens"], 4096)
        self.assertEqual(state["contextUsage"]["percent"], 3)


if __name__ == "__main__":
    unittest.main()
