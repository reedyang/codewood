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
        self._last_context_parts = []
        self._session_values = {
            "chat-1": {"window": 0, "tokens": 0, "percent": 0, "parts": []},
        }

    def _workspace_root_path(self, entry):
        return str(entry.get("root") or "")

    def _workspace_current_dir_path(self, entry):
        return entry.get("current_dir") or entry.get("root")

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
        prev_parts = self._last_context_parts
        values = self._session_values.get(chat_id, {})
        self._last_context_window = int(values.get("window", 0) or 0)
        self._last_context_input_tokens = int(values.get("tokens", 0) or 0)
        self._last_context_usage_percent = int(values.get("percent", 0) or 0)
        self._last_context_parts = list(values.get("parts", []) or [])
        try:
            yield
        finally:
            self._last_context_window = prev_window
            self._last_context_input_tokens = prev_tokens
            self._last_context_usage_percent = prev_percent
            self._last_context_parts = prev_parts


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
            "parts": [
                {"key": "system", "tokens": 3000},
                {"key": "history", "tokens": 1096},
            ],
        }
        with patch("cli.server.serve_app._compute_chat_cache_stats", return_value={}):
            state = _build_state_inner(agent)

        self.assertEqual(state["contextUsage"]["window"], 128000)
        self.assertEqual(state["contextUsage"]["tokens"], 4096)
        self.assertEqual(state["contextUsage"]["percent"], 3)
        self.assertEqual(state["contextUsage"]["parts"], [
            {"key": "system", "tokens": 3000},
            {"key": "history", "tokens": 1096},
        ])

    def test_context_usage_parts_default_to_empty(self):
        agent = _FakeAgent()
        with patch("cli.server.serve_app._compute_chat_cache_stats", return_value={}):
            state = _build_state_inner(agent)
        self.assertEqual(state["contextUsage"]["parts"], [])

    def test_explicit_background_workspace_uses_its_own_metadata(self):
        agent = _FakeAgent()
        agent._workspaces_state["workspaces"]["ws-2"] = {
            "id": "ws-2",
            "name": "Workspace B",
            "root": "D:/workspace-b",
            "current_dir": "D:/workspace-b/subdir",
            "kind": "custom",
        }

        with patch("cli.server.serve_app._compute_chat_cache_stats", return_value={}):
            state = _build_state_inner(agent, workspace_id="ws-2")

        self.assertEqual(state["workspace"]["id"], "ws-2")
        self.assertEqual(state["workspace"]["name"], "Workspace B")
        self.assertEqual(state["workspace"]["root"], "D:/workspace-b")
        self.assertEqual(state["workspace"]["workDirectory"], "D:/workspace-b/subdir")


if __name__ == "__main__":
    unittest.main()
