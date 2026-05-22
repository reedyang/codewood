import tempfile
import threading
import unittest
import json
from pathlib import Path

from src.managers.chat_state_manager import ChatStateManager


class _FakeAgent:
    def __init__(self, workspace: Path):
        self.ai_workspace_dir = workspace
        self._chat_state = {}
        self._chat_state_lock = threading.RLock()
        self.provider = "openai"
        self.model_name = "gpt-4.1"
        self.active_chat_id = ""
        self.active_chat_name = "New Chat"
        self.conversation_history = []
        self.operation_results = []
        self._session_summary_llm = ""
        self._session_summary_rolling = ""
        self._last_llm_summary_pair_count = 0
        self.applied_chat_model_calls = 0
        self.refresh_status_usage_calls = 0
        self._last_context_usage_percent = 0
        self._last_context_input_tokens = 0
        self._last_context_window = 0
        self._active_runtime_task_id = ""
        self._active_runtime_task_domains = []
        self.remembered_history_anchor_indexes = []

    def _apply_chat_model_from_entry(self, chat, persist_if_missing=False):
        self.applied_chat_model_calls += 1
        if persist_if_missing and not str(chat.get("model_provider") or "").strip():
            chat["model_provider"] = self.provider
        if persist_if_missing and not str(chat.get("model_name") or "").strip():
            chat["model_name"] = self.model_name

    def _print_chat_history(self):
        return None

    def _refresh_status_context_usage_snapshot(self):
        self.refresh_status_usage_calls += 1

    def _remember_active_chat_history_first_visible_index(self, index: int):
        self.remembered_history_anchor_indexes.append(int(index))


class ChatStateModelPersistenceTests(unittest.TestCase):
    def test_new_chat_entry_inherits_current_model(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeAgent(Path(td))
            manager = ChatStateManager(agent, "chats.json")
            entry = manager.new_chat_entry("chat-2", "Demo")
            self.assertEqual(entry.get("model_provider"), "openai")
            self.assertEqual(entry.get("model_name"), "gpt-4.1")

    def test_activate_chat_backfills_missing_model_and_calls_apply(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeAgent(Path(td))
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": 1,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Legacy",
                        "name_source": "manual",
                        "created_at": "",
                        "updated_at": "",
                        "tasks": [],
                        "active_task_id": "",
                        "messages": [],
                        "context_usage_percent": 44,
                        "context_input_tokens": 1234,
                        "context_window": 64000,
                    }
                ],
            }
            msg = manager.activate_chat("chat-1", announce=False, clear_screen=False, print_history=False)
            self.assertEqual(msg, "")
            self.assertEqual(agent.applied_chat_model_calls, 1)
            self.assertEqual(agent.refresh_status_usage_calls, 0)
            self.assertEqual(agent._last_context_usage_percent, 44)
            self.assertEqual(agent._last_context_input_tokens, 1234)
            self.assertEqual(agent._last_context_window, 64000)
            chat = manager.find_chat_by_id("chat-1")
            self.assertIsNotNone(chat)
            self.assertEqual(chat.get("model_provider"), "openai")
            self.assertEqual(chat.get("model_name"), "gpt-4.1")
            self.assertEqual(agent.remembered_history_anchor_indexes, [0])

    def test_activate_chat_without_print_history_records_next_visible_index(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeAgent(Path(td))
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": 1,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Legacy",
                        "name_source": "manual",
                        "created_at": "",
                        "updated_at": "",
                        "tasks": [],
                        "active_task_id": "",
                        "messages": [
                            {"role": "user", "content": "hi", "task_id": "", "created_at": ""},
                            {"role": "assistant", "content": "hello", "task_id": "", "created_at": ""},
                        ],
                        "context_usage_percent": 0,
                        "context_input_tokens": 0,
                        "context_window": 0,
                    }
                ],
            }
            msg = manager.activate_chat("chat-1", announce=False, clear_screen=False, print_history=False)
            self.assertEqual(msg, "")
            self.assertEqual(agent.remembered_history_anchor_indexes, [2])

    def test_compact_keeps_repeated_identical_user_turns(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeAgent(Path(td))
            manager = ChatStateManager(agent, "chats.json")
            messages = [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": '{"tool":"noop"}'},
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好！有什么我可以帮助您的？"},
            ]
            chat = {
                "id": "chat-1",
                "name": "demo",
                "name_source": "manual",
                "created_at": "",
                "updated_at": "",
                "model_provider": "openai",
                "model_name": "gpt-4.1",
                "tasks": [
                    {
                        "id": "task-1",
                        "status": "open",
                        "root_user_input": "你好",
                        "domains": ["general_other"],
                        "domain_scores": {},
                        "classifier": {},
                        "created_at": "",
                        "updated_at": "",
                        "closed_at": "",
                        "switched_from_task_id": "",
                    }
                ],
                "active_task_id": "task-1",
                "messages": [
                    {**messages[0], "task_id": "task-1", "created_at": ""},
                    {**messages[1], "task_id": "task-1", "created_at": ""},
                    {**messages[2], "task_id": "task-1", "created_at": ""},
                    {**messages[3], "task_id": "task-1", "created_at": ""},
                ],
                "context_usage_percent": 0,
                "context_input_tokens": 0,
                "context_window": 0,
            }
            normalized = manager._validate_chat_entry(chat)
            self.assertEqual(len(normalized["messages"]), 4)

    def test_load_chat_state_skips_save_when_state_is_clean(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            chat_path = workspace / "chats.json"
            payload = {
                "version": 2,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Clean",
                        "name_source": "manual",
                        "created_at": "",
                        "updated_at": "",
                        "model_provider": "openai",
                        "model_name": "gpt-4.1",
                        "tasks": [
                            {
                                "id": "task-1",
                                "status": "open",
                                "root_user_input": "hello",
                                "domains": ["general_other"],
                                "domain_scores": {},
                                "classifier": {},
                                "created_at": "",
                                "updated_at": "",
                                "closed_at": "",
                                "switched_from_task_id": "",
                            }
                        ],
                        "active_task_id": "task-1",
                        "messages": [
                            {"role": "user", "content": "hello", "task_id": "task-1", "created_at": ""}
                        ],
                        "context_usage_percent": 0,
                        "context_input_tokens": 0,
                        "context_window": 0,
                    }
                ],
            }
            chat_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

            agent = _FakeAgent(workspace)
            manager = ChatStateManager(agent, "chats.json")
            save_calls = []
            manager.save_chat_state = lambda: save_calls.append("saved")

            manager.load_chat_state()
            self.assertEqual(save_calls, [])
            self.assertEqual(agent.active_chat_id, "chat-1")
            self.assertEqual(agent._active_runtime_task_id, "task-1")

    def test_load_chat_state_invalid_schema_resets_and_persists_default(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            chat_path = workspace / "chats.json"
            payload = {
                "version": 1,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Legacy",
                        "name_source": "manual",
                        "created_at": "",
                        "updated_at": "",
                    }
                ],
            }
            chat_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

            agent = _FakeAgent(workspace)
            manager = ChatStateManager(agent, "chats.json")
            save_calls = []
            manager.save_chat_state = lambda: save_calls.append("saved")

            manager.load_chat_state()
            self.assertEqual(save_calls, ["saved"])
            self.assertEqual(agent.active_chat_id, "chat-1")
            self.assertEqual(agent._chat_state.get("version"), 2)


if __name__ == "__main__":
    unittest.main()
