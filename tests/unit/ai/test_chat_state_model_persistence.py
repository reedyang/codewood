import tempfile
import threading
import unittest
import json
from pathlib import Path

from src.managers.chat_state_manager import CHAT_STATE_VERSION, ChatStateManager


def _chat_index_entry(chat):
    record_file = chat.get("_record_file") or f"0123456789abcdef0123456789abcde{len(str(chat['id'])) % 10}.json"
    return {
        "id": chat["id"],
        "name": chat.get("name", "New Chat"),
        "name_source": chat.get("name_source", "default"),
        "created_at": chat.get("created_at", ""),
        "updated_at": chat.get("updated_at", ""),
        "model_provider": chat.get("model_provider", ""),
        "model_name": chat.get("model_name", ""),
        "record_file": record_file,
    }


def _write_chat_store(workspace: Path, payload):
    chats_dir = workspace / "chats"
    chats_dir.mkdir(parents=True, exist_ok=True)
    chats = [c for c in payload.get("chats", []) if isinstance(c, dict)]
    index = {
        "version": CHAT_STATE_VERSION,
        "active": payload.get("active", ""),
        "chats": [_chat_index_entry(c) for c in chats],
    }
    for chat in chats:
        record_file = _chat_index_entry(chat)["record_file"]
        record_payload = {k: v for k, v in chat.items() if not str(k).startswith("_")}
        (chats_dir / record_file).write_text(
            json.dumps(record_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    (chats_dir / "chats.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_chat_index(workspace: Path):
    return json.loads((workspace / "chats" / "chats.json").read_text(encoding="utf-8"))


def _read_first_chat_record(workspace: Path):
    index = _read_chat_index(workspace)
    record_file = index["chats"][0]["record_file"]
    return json.loads((workspace / "chats" / record_file).read_text(encoding="utf-8"))


def _assert_hash_record_file(testcase, record_file: str):
    testcase.assertRegex(record_file, r"^[0-9a-f]{32}\.json$")
    testcase.assertNotEqual(record_file, "chat-1.json")


class _FakeAgent:
    def __init__(self, workspace: Path):
        self.workspace_config_dir = workspace
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
        self.remembered_history_anchor_indexes = []
        self.session_memory_service = None

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
            schedule_calls = []

            class _FakeSessionMemoryService:
                def schedule_context_usage_refresh_async(self, user_input_hint="", context_hint=""):
                    schedule_calls.append(
                        {
                            "user_input_hint": str(user_input_hint or ""),
                            "context_hint": str(context_hint or ""),
                        }
                    )
                    return True

            agent.session_memory_service = _FakeSessionMemoryService()
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
            self.assertEqual(agent.refresh_status_usage_calls, 1)
            self.assertEqual(len(schedule_calls), 1)
            self.assertEqual(schedule_calls[0]["context_hint"], "chat activated")
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

    def test_activate_chat_reload_same_chat_keeps_operation_results(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeAgent(Path(td))
            manager = ChatStateManager(agent, "chats.json")
            agent.active_chat_id = "chat-1"
            agent.operation_results = [
                {
                    "command": {"tool": "shell", "args": {"command": "test"}},
                    "result": {"success": False, "error": "Command execution failed, exit code: 1"},
                }
            ]
            agent._chat_state = {
                "version": 2,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Current",
                        "name_source": "manual",
                        "created_at": "",
                        "updated_at": "",
                        "model_provider": "openai",
                        "model_name": "gpt-4.1",
                        "tasks": [],
                        "active_task_id": "",
                        "messages": [],
                        "context_usage_percent": 0,
                        "context_input_tokens": 0,
                        "context_window": 0,
                    }
                ],
            }
            msg = manager.activate_chat("chat-1", announce=False, clear_screen=False, print_history=False)
            self.assertEqual(msg, "")
            self.assertEqual(len(agent.operation_results), 1)
            self.assertFalse(bool(agent.operation_results[0].get("result", {}).get("success", True)))

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
            payload = {
                "version": CHAT_STATE_VERSION,
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
            _write_chat_store(workspace, payload)

            agent = _FakeAgent(workspace)
            manager = ChatStateManager(agent, "chats.json")
            save_calls = []
            manager.save_chat_state = lambda: save_calls.append("saved")

            manager.load_chat_state()
            self.assertEqual(save_calls, [])
            self.assertEqual(agent.active_chat_id, "chat-1")
            self.assertEqual(agent._active_runtime_task_id, "task-1")

    def test_close_task_clears_active_task_id_when_closed(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeAgent(Path(td))
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": 2,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Main",
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
                                "created_at": "",
                                "updated_at": "",
                                "closed_at": "",
                                "switched_from_task_id": "",
                            }
                        ],
                        "active_task_id": "task-1",
                        "messages": [],
                        "context_usage_percent": 0,
                        "context_input_tokens": 0,
                        "context_window": 0,
                    }
                ],
            }
            ok = manager.close_task("chat-1", "task-1", "cancelled")
            chat = manager.find_chat_by_id("chat-1")
            self.assertTrue(ok)
            self.assertEqual(chat.get("active_task_id"), "")

    def test_validate_task_discards_legacy_classification_fields(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeAgent(Path(td))
            manager = ChatStateManager(agent, "chats.json")
            task = manager._validate_task(
                {
                    "id": "task-1",
                    "status": "done",
                    "root_user_input": "hello",
                    "domains": ["software_development"],
                    "domain_scores": {"software_development": 0.8},
                    "classifier": {"primary_domain": "software_development"},
                    "created_at": "",
                    "updated_at": "",
                    "closed_at": "",
                    "switched_from_task_id": "",
                }
            )

            self.assertNotIn("domains", task)
            self.assertNotIn("domain_scores", task)
            self.assertNotIn("classifier", task)

    def test_load_chat_state_invalid_schema_resets_and_persists_default(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            chat_path = workspace / "chats" / "chats.json"
            chat_path.parent.mkdir(parents=True, exist_ok=True)
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
            self.assertEqual(agent._chat_state.get("version"), CHAT_STATE_VERSION)

    def test_clear_chat_context_and_tasks_clears_messages_and_task_records(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeAgent(Path(td))
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": 2,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Demo",
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
                        "context_usage_percent": 52,
                        "context_input_tokens": 123,
                        "context_window": 64000,
                    }
                ],
            }
            save_calls = []
            manager.save_chat_state = lambda: save_calls.append("saved")

            ok = manager.clear_chat_context_and_tasks("chat-1")

            self.assertTrue(ok)
            chat = manager.find_chat_by_id("chat-1")
            self.assertEqual(chat.get("messages"), [])
            self.assertEqual(chat.get("tasks"), [])
            self.assertEqual(chat.get("active_task_id"), "")
            self.assertEqual(chat.get("context_usage_percent"), 0)
            self.assertEqual(chat.get("context_input_tokens"), 0)
            self.assertEqual(save_calls, ["saved"])

    def test_persist_active_chat_usage_snapshot_writes_usage_to_file_immediately(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": 2,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Demo",
                        "name_source": "manual",
                        "created_at": "",
                        "updated_at": "",
                        "model_provider": "openai",
                        "model_name": "gpt-4.1",
                        "tasks": [],
                        "active_task_id": "",
                        "messages": [],
                        "context_usage_percent": 0,
                        "context_input_tokens": 0,
                        "context_window": 0,
                    }
                ],
            }
            agent.active_chat_id = "chat-1"
            agent._last_context_usage_percent = 67
            agent._last_context_input_tokens = 4321
            agent._last_context_window = 128000

            manager.persist_active_chat_usage_snapshot()

            chat = manager.find_chat_by_id("chat-1")
            self.assertIsNotNone(chat)
            self.assertEqual(chat.get("context_usage_percent"), 67)
            self.assertEqual(chat.get("context_input_tokens"), 4321)
            self.assertEqual(chat.get("context_window"), 128000)

            payload = _read_chat_index(workspace)
            _assert_hash_record_file(self, payload["chats"][0].get("record_file"))
            saved_chat = _read_first_chat_record(workspace)
            self.assertEqual(saved_chat.get("context_usage_percent"), 67)
            self.assertEqual(saved_chat.get("context_input_tokens"), 4321)
            self.assertEqual(saved_chat.get("context_window"), 128000)

    def test_sync_active_chat_messages_persists_exclude_from_model_context_flag(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": 2,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Main",
                        "name_source": "manual",
                        "created_at": "",
                        "updated_at": "",
                        "model_provider": "openai",
                        "model_name": "gpt-4.1",
                        "tasks": [],
                        "active_task_id": "",
                        "messages": [],
                        "context_usage_percent": 0,
                        "context_input_tokens": 0,
                        "context_window": 0,
                    }
                ],
            }
            agent.active_chat_id = "chat-1"
            agent._active_runtime_task_id = "task-1"
            agent.conversation_history = [
                {
                    "role": "user",
                    "content": "pending message",
                    "task_id": "task-1",
                    "exclude_from_model_context": True,
                }
            ]

            manager.sync_active_chat_messages()

            chat = manager.find_chat_by_id("chat-1")
            self.assertIsNotNone(chat)
            msgs = list(chat.get("messages") or [])
            self.assertEqual(len(msgs), 1)
            self.assertTrue(bool(msgs[0].get("exclude_from_model_context", False)))
            payload = _read_chat_index(workspace)
            _assert_hash_record_file(self, payload["chats"][0].get("record_file"))
            saved_msgs = list(_read_first_chat_record(workspace).get("messages") or [])
            self.assertEqual(len(saved_msgs), 1)
            self.assertTrue(bool(saved_msgs[0].get("exclude_from_model_context", False)))

    def test_sync_active_chat_messages_skips_memory_only_messages(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": 2,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Main",
                        "name_source": "manual",
                        "created_at": "",
                        "updated_at": "",
                        "model_provider": "openai",
                        "model_name": "gpt-4.1",
                        "tasks": [],
                        "active_task_id": "",
                        "messages": [],
                        "context_usage_percent": 0,
                        "context_input_tokens": 0,
                        "context_window": 0,
                    }
                ],
            }
            agent.active_chat_id = "chat-1"
            agent.conversation_history = [
                {
                    "role": "user",
                    "content": "persisted",
                },
                {
                    "role": "assistant",
                    "content": "memory only",
                    "persist_to_chat_state": False,
                },
            ]

            manager.sync_active_chat_messages()

            chat = manager.find_chat_by_id("chat-1")
            self.assertIsNotNone(chat)
            msgs = list(chat.get("messages") or [])
            self.assertEqual([m.get("content") for m in msgs], ["persisted"])
            payload = _read_chat_index(workspace)
            _assert_hash_record_file(self, payload["chats"][0].get("record_file"))
            saved_msgs = list(_read_first_chat_record(workspace).get("messages") or [])
            self.assertEqual([m.get("content") for m in saved_msgs], ["persisted"])

    def test_sync_active_chat_messages_does_not_fallback_to_closed_active_task(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": 2,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Main",
                        "name_source": "manual",
                        "created_at": "",
                        "updated_at": "",
                        "model_provider": "openai",
                        "model_name": "gpt-4.1",
                        "tasks": [
                            {
                                "id": "task-1",
                                "status": "cancelled",
                                "root_user_input": "old",
                                "created_at": "",
                                "updated_at": "",
                                "closed_at": "",
                                "switched_from_task_id": "",
                            }
                        ],
                        "active_task_id": "task-1",
                        "messages": [],
                        "context_usage_percent": 0,
                        "context_input_tokens": 0,
                        "context_window": 0,
                    }
                ],
            }
            agent.active_chat_id = "chat-1"
            agent._active_runtime_task_id = ""
            agent.conversation_history = [{"role": "user", "content": "direct shell", "task_id": ""}]

            manager.sync_active_chat_messages()

            chat = manager.find_chat_by_id("chat-1")
            msgs = list(chat.get("messages") or [])
            self.assertEqual(chat.get("active_task_id"), "")
            self.assertEqual(len(msgs), 1)
            self.assertEqual(msgs[0].get("task_id"), "")


if __name__ == "__main__":
    unittest.main()

