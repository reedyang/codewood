import tempfile
import threading
import unittest
import json
from pathlib import Path

from cli.managers.chat_state_manager import CHAT_STATE_VERSION, ChatStateManager


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
                        "messages": [
                            {"role": "user", "content": "hi", "created_at": ""},
                            {"role": "assistant", "content": "hello", "created_at": ""},
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
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": '{"tool":"noop"}'},
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hello! How can I help you?"},
            ]
            chat = {
                "id": "chat-1",
                "name": "demo",
                "name_source": "manual",
                "created_at": "",
                "updated_at": "",
                "model_provider": "openai",
                "model_name": "gpt-4.1",
                "messages": [
                    {**messages[0], "created_at": ""},
                    {**messages[1], "created_at": ""},
                    {**messages[2], "created_at": ""},
                    {**messages[3], "created_at": ""},
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
                        "messages": [
                            {"role": "user", "content": "hello", "created_at": ""}
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

    def test_load_chat_state_preserves_pseudo_tool_call_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            pseudo_text = '{"tool_calls":[{"tool":"shell","args":{"command":"echo hi"}}]}'
            payload = {
                "version": CHAT_STATE_VERSION,
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
                        "messages": [
                            {
                                "role": "assistant",
                                "content": "Plan: run a safe command.",
                                "created_at": "",
                                "pseudo_tool_call_text": pseudo_text,
                                "pseudo_tool_call_tools": ["shell", "", " shell "],
                            }
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

            manager.load_chat_state()

            chat = manager.find_chat_by_id("chat-1")
            self.assertIsNotNone(chat)
            msgs = list(chat.get("messages") or [])
            self.assertEqual(len(msgs), 1)
            self.assertEqual(msgs[0].get("content"), "Plan: run a safe command.")
            self.assertNotIn("tool_calls", msgs[0].get("content", ""))
            self.assertEqual(msgs[0].get("pseudo_tool_call_text"), pseudo_text)
            self.assertEqual(msgs[0].get("pseudo_tool_call_tools"), ["shell", "shell"])

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

    def test_clear_chat_context_clears_messages(self):
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
                        "messages": [
                            {"role": "user", "content": "hello", "created_at": ""}
                        ],
                        "context_usage_percent": 52,
                        "context_input_tokens": 123,
                        "context_window": 64000,
                    }
                ],
            }
            save_calls = []
            manager.save_chat_state = lambda: save_calls.append("saved")

            ok = manager.clear_chat_context("chat-1")

            self.assertTrue(ok)
            chat = manager.find_chat_by_id("chat-1")
            self.assertEqual(chat.get("messages"), [])
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
                    "content": "pending message",
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

    def test_sync_active_chat_messages_persists_pseudo_tool_call_metadata(self):
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
                        "messages": [],
                        "context_usage_percent": 0,
                        "context_input_tokens": 0,
                        "context_window": 0,
                    }
                ],
            }
            pseudo_text = '{"tool_calls":[{"tool":"shell","args":{"command":"echo hi"}}]}'
            agent.active_chat_id = "chat-1"
            agent.conversation_history = [
                {
                    "role": "assistant",
                    "content": "Plan: run a safe command.",
                    "pseudo_tool_call_text": pseudo_text,
                    "pseudo_tool_call_tools": ["shell", "", " shell "],
                }
            ]

            manager.sync_active_chat_messages()

            chat = manager.find_chat_by_id("chat-1")
            self.assertIsNotNone(chat)
            msgs = list(chat.get("messages") or [])
            self.assertEqual(len(msgs), 1)
            self.assertEqual(msgs[0].get("content"), "Plan: run a safe command.")
            self.assertNotIn("tool_calls", msgs[0].get("content", ""))
            self.assertEqual(msgs[0].get("pseudo_tool_call_text"), pseudo_text)
            self.assertEqual(msgs[0].get("pseudo_tool_call_tools"), ["shell", "shell"])
            payload = _read_chat_index(workspace)
            _assert_hash_record_file(self, payload["chats"][0].get("record_file"))
            saved_msgs = list(_read_first_chat_record(workspace).get("messages") or [])
            self.assertEqual(len(saved_msgs), 1)
            self.assertEqual(saved_msgs[0].get("content"), "Plan: run a safe command.")
            self.assertNotIn("tool_calls", saved_msgs[0].get("content", ""))
            self.assertEqual(saved_msgs[0].get("pseudo_tool_call_text"), pseudo_text)
            self.assertEqual(saved_msgs[0].get("pseudo_tool_call_tools"), ["shell", "shell"])

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


class RefreshChatRecordFromDiskTests(unittest.TestCase):
    """Cross-process refresh: pull a chat record from disk into memory.

    Codewood may run as multiple independent processes (e.g. TUI + GUI)
    against the same workspace. When one process amends a chat record on
    disk — most importantly when the TUI persists ``pending_request_user_input``
    while waiting on the user's selection — the other process must be able
    to refresh its in-memory ``_chat_state`` so the panel (or any other
    cross-process state) becomes visible without restarting.
    """

    def _bootstrap_two_processes(self, workspace: Path):
        """Return ``(driver_manager, reader_manager)`` that share a workspace."""
        driver_agent = _FakeAgent(workspace)
        driver_manager = ChatStateManager(driver_agent, "chats.json")
        driver_agent._chat_state = {
            "version": CHAT_STATE_VERSION,
            "active": "chat-1",
            "chats": [
                {
                    "id": "chat-1",
                    "name": "Shared",
                    "name_source": "manual",
                    "created_at": "",
                    "updated_at": "",
                    "model_provider": "openai",
                    "model_name": "gpt-4.1",
                    "messages": [
                        {
                            "role": "user",
                            "content": "search gmail skills",
                            "created_at": "2026-06-18 09:29:00",
                        }
                    ],
                    "context_usage_percent": 0,
                    "context_input_tokens": 0,
                    "context_window": 0,
                }
            ],
        }
        driver_agent.active_chat_id = "chat-1"
        driver_manager.save_chat_state()

        reader_agent = _FakeAgent(workspace)
        reader_manager = ChatStateManager(reader_agent, "chats.json")
        reader_manager.load_chat_state()
        return driver_manager, driver_agent, reader_manager, reader_agent

    def test_refresh_picks_up_pending_request_user_input_written_by_peer(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            driver_manager, driver_agent, reader_manager, reader_agent = (
                self._bootstrap_two_processes(workspace)
            )

            self.assertIsNone(
                driver_manager.find_chat_by_id("chat-1").get("pending_request_user_input")
            )
            self.assertIsNone(
                reader_manager.find_chat_by_id("chat-1").get("pending_request_user_input")
            )

            driver_chat = driver_manager.find_chat_by_id("chat-1")
            driver_chat["pending_request_user_input"] = {
                "id": "tui-prompt-1",
                "question": "pick a skill",
                "options": ["a", "b"],
                "multi_select": False,
                "created_at": "2026-06-18 09:29:21",
            }
            driver_manager.save_chat_state()

            self.assertIsNone(
                reader_manager.find_chat_by_id("chat-1").get("pending_request_user_input"),
                msg="reader should not see disk-only updates until it refreshes",
            )

            self.assertTrue(reader_manager.refresh_chat_record_from_disk("chat-1"))

            refreshed = reader_manager.find_chat_by_id("chat-1").get(
                "pending_request_user_input"
            )
            self.assertIsInstance(refreshed, dict)
            self.assertEqual(refreshed.get("id"), "tui-prompt-1")
            self.assertEqual(refreshed.get("options"), ["a", "b"])
            self.assertEqual(refreshed.get("multi_select"), False)

    def test_refresh_pulls_new_messages_written_by_peer(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            driver_manager, driver_agent, reader_manager, reader_agent = (
                self._bootstrap_two_processes(workspace)
            )

            driver_chat = driver_manager.find_chat_by_id("chat-1")
            driver_chat["messages"].append(
                {
                    "role": "assistant",
                    "content": "I found 8 skills, choose one.",
                    "created_at": "2026-06-18 09:29:21",
                }
            )
            driver_manager.save_chat_state()

            self.assertTrue(reader_manager.refresh_chat_record_from_disk("chat-1"))

            messages = reader_manager.find_chat_by_id("chat-1").get("messages") or []
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[-1].get("content"), "I found 8 skills, choose one.")

    def test_refresh_unknown_chat_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            _driver, _drag, reader_manager, _rag = self._bootstrap_two_processes(
                workspace
            )
            self.assertFalse(reader_manager.refresh_chat_record_from_disk("nope"))

    def test_refresh_does_not_drop_in_memory_only_data_for_other_chats(self):
        # When chat-1 is refreshed, chat-2 (which only exists in memory on
        # the reader side) must NOT be removed: ``refresh_chat_record_from_disk``
        # operates on a single record, never the whole index.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            _drv, _da, reader_manager, reader_agent = self._bootstrap_two_processes(
                workspace
            )
            reader_agent._chat_state["chats"].append(
                {
                    "id": "chat-2",
                    "name": "Local only",
                    "name_source": "manual",
                    "created_at": "",
                    "updated_at": "",
                    "model_provider": "",
                    "model_name": "",
                    "messages": [],
                    "context_usage_percent": 0,
                    "context_input_tokens": 0,
                    "context_window": 0,
                }
            )

            self.assertTrue(reader_manager.refresh_chat_record_from_disk("chat-1"))

            ids = [c.get("id") for c in reader_agent._chat_state["chats"]]
            self.assertIn("chat-1", ids)
            self.assertIn("chat-2", ids)


class CrossProcessSaveMergeTests(unittest.TestCase):
    """``save_chat_state`` must not clobber a peer's newer on-disk records.

    Reproduces: GUI opens chat A; TUI (a separate process) sends a message
    in chat A and persists it; the GUI then switches chats, which triggers
    a full ``save_chat_state``. With the naive full-overwrite the GUI's
    stale in-memory chat A wiped the TUI's freshly written message. The
    merge-on-save keeps the newer disk record for chats this process does
    not own.
    """

    def _make_chat(self, cid, name, updated_at, messages):
        return {
            "id": cid,
            "name": name,
            "name_source": "manual",
            "created_at": "2026-06-18 09:00:00",
            "updated_at": updated_at,
            "model_provider": "openai",
            "model_name": "gpt-4.1",
            "messages": messages,
            "context_usage_percent": 0,
            "context_input_tokens": 0,
            "context_window": 0,
        }

    def test_save_preserves_newer_disk_record_for_unowned_chat(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            # GUI process loads chat A + B.
            gui = _FakeAgent(workspace)
            gui_mgr = ChatStateManager(gui, "chats.json")
            gui._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-a",
                "chats": [
                    self._make_chat(
                        "chat-a",
                        "A",
                        "2026-06-18 09:10:00",
                        [{"role": "user", "content": "old A", "created_at": "2026-06-18 09:10:00"}],
                    ),
                    self._make_chat(
                        "chat-b",
                        "B",
                        "2026-06-18 09:05:00",
                        [{"role": "user", "content": "B msg", "created_at": "2026-06-18 09:05:00"}],
                    ),
                ],
            }
            gui.active_chat_id = "chat-a"
            gui_mgr.save_chat_state()

            # TUI process (separate ChatStateManager) sends a message in
            # chat A and persists with a NEWER updated_at.
            tui = _FakeAgent(workspace)
            tui_mgr = ChatStateManager(tui, "chats.json")
            tui_mgr.load_chat_state()
            tui.active_chat_id = "chat-a"
            tui_chat_a = tui_mgr.find_chat_by_id("chat-a")
            tui_chat_a["messages"].append(
                {"role": "assistant", "content": "TUI reply", "created_at": "2026-06-18 09:20:00"}
            )
            tui_chat_a["updated_at"] = "2026-06-18 09:20:00"
            tui_mgr.save_chat_state()

            # GUI switches focus to chat B and saves. Its in-memory chat A
            # is now stale (still "old A"); the save MUST NOT overwrite the
            # newer disk record for chat A.
            gui._chat_state["active"] = "chat-b"
            gui.active_chat_id = "chat-b"
            gui_mgr.save_chat_state()

            # Verify chat A on disk still has the TUI's message.
            fresh = _FakeAgent(workspace)
            fresh_mgr = ChatStateManager(fresh, "chats.json")
            fresh_mgr.load_chat_state()
            chat_a = fresh_mgr.find_chat_by_id("chat-a")
            contents = [m.get("content") for m in (chat_a.get("messages") or [])]
            self.assertIn("TUI reply", contents)
            # And the GUI's in-memory copy got refreshed to the disk version.
            gui_chat_a = gui_mgr.find_chat_by_id("chat-a")
            gui_contents = [m.get("content") for m in (gui_chat_a.get("messages") or [])]
            self.assertIn("TUI reply", gui_contents)

    def test_save_reloads_active_chat_when_disk_is_newer_and_idle(self):
        # n11: a TUI sitting idle on the active chat must NOT clobber a newer
        # record a peer (GUI) wrote for that same chat. With no in-flight turn
        # (``_in_task_execution`` False), the strictly-newer disk record wins
        # and is reloaded into memory.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            tui = _FakeAgent(workspace)
            tui_mgr = ChatStateManager(tui, "chats.json")
            tui._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-a",
                "chats": [
                    self._make_chat(
                        "chat-a",
                        "A",
                        "2026-06-18 09:10:00",
                        [{"role": "user", "content": "old", "created_at": "2026-06-18 09:10:00"}],
                    )
                ],
            }
            tui.active_chat_id = "chat-a"
            tui._in_task_execution = False
            tui_mgr.save_chat_state()

            # Peer (GUI) writes a strictly newer chat A.
            peer = _FakeAgent(workspace)
            peer_mgr = ChatStateManager(peer, "chats.json")
            peer_mgr.load_chat_state()
            peer.active_chat_id = "chat-a"
            pc = peer_mgr.find_chat_by_id("chat-a")
            pc["messages"] = [
                {"role": "user", "content": "old", "created_at": "2026-06-18 09:10:00"},
                {"role": "assistant", "content": "peer reply", "created_at": "2026-06-18 09:30:00"},
            ]
            pc["updated_at"] = "2026-06-18 09:30:00"
            peer_mgr.save_chat_state()

            # TUI saves again while idle on chat A (e.g. a background flush).
            # It must reload rather than overwrite the peer's newer record.
            tui_mgr.save_chat_state()

            fresh = _FakeAgent(workspace)
            fresh_mgr = ChatStateManager(fresh, "chats.json")
            fresh_mgr.load_chat_state()
            chat_a = fresh_mgr.find_chat_by_id("chat-a")
            contents = [m.get("content") for m in (chat_a.get("messages") or [])]
            self.assertIn("peer reply", contents)
            # And the TUI's in-memory copy got refreshed.
            tui_a = tui_mgr.find_chat_by_id("chat-a")
            tui_contents = [m.get("content") for m in (tui_a.get("messages") or [])]
            self.assertIn("peer reply", tui_contents)

    def test_save_overwrites_active_chat_during_in_flight_turn(self):
        # When a turn is in flight (``_in_task_execution`` True) the active
        # chat is authoritative and must overwrite even a newer-looking disk
        # record (e.g. a stale peer timestamp).
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            tui = _FakeAgent(workspace)
            tui_mgr = ChatStateManager(tui, "chats.json")
            tui._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-a",
                "chats": [
                    self._make_chat(
                        "chat-a",
                        "A",
                        "2026-06-18 09:10:00",
                        [{"role": "user", "content": "v1", "created_at": "2026-06-18 09:10:00"}],
                    )
                ],
            }
            tui.active_chat_id = "chat-a"
            tui_mgr.save_chat_state()

            peer = _FakeAgent(workspace)
            peer_mgr = ChatStateManager(peer, "chats.json")
            peer_mgr.load_chat_state()
            pc = peer_mgr.find_chat_by_id("chat-a")
            pc["messages"] = [{"role": "user", "content": "peer", "created_at": "2026-06-18 09:30:00"}]
            pc["updated_at"] = "2026-06-18 09:30:00"
            peer_mgr.save_chat_state()

            # TUI is mid-turn and writes its own newer content.
            tui._in_task_execution = True
            tui_a = tui_mgr.find_chat_by_id("chat-a")
            tui_a["messages"] = [{"role": "user", "content": "in-flight", "created_at": "2026-06-18 09:20:00"}]
            tui_a["updated_at"] = "2026-06-18 09:20:00"
            tui_mgr.save_chat_state()

            fresh = _FakeAgent(workspace)
            fresh_mgr = ChatStateManager(fresh, "chats.json")
            fresh_mgr.load_chat_state()
            chat_a = fresh_mgr.find_chat_by_id("chat-a")
            contents = [m.get("content") for m in (chat_a.get("messages") or [])]
            self.assertEqual(contents, ["in-flight"])

    def test_save_overwrites_owned_active_chat_even_if_disk_is_newer(self):
        # The active chat is owned by this process; its in-memory copy is
        # authoritative and must always be written, even when a stale disk
        # timestamp happens to look newer.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            gui = _FakeAgent(workspace)
            gui_mgr = ChatStateManager(gui, "chats.json")
            gui._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-a",
                "chats": [
                    self._make_chat(
                        "chat-a",
                        "A",
                        "2026-06-18 09:10:00",
                        [{"role": "user", "content": "v1", "created_at": "2026-06-18 09:10:00"}],
                    )
                ],
            }
            gui.active_chat_id = "chat-a"
            gui_mgr.save_chat_state()

            # Peer writes an even newer chat A.
            peer = _FakeAgent(workspace)
            peer_mgr = ChatStateManager(peer, "chats.json")
            peer_mgr.load_chat_state()
            peer.active_chat_id = "chat-a"
            pc = peer_mgr.find_chat_by_id("chat-a")
            pc["messages"] = [{"role": "user", "content": "peer", "created_at": "2026-06-18 09:30:00"}]
            pc["updated_at"] = "2026-06-18 09:30:00"
            peer_mgr.save_chat_state()

            # GUI keeps chat-a active and edits it, then saves. Because the
            # GUI owns the active chat, its content wins.
            gui_a = gui_mgr.find_chat_by_id("chat-a")
            gui_a["messages"] = [{"role": "user", "content": "gui edit", "created_at": "2026-06-18 09:40:00"}]
            gui_a["updated_at"] = "2026-06-18 09:40:00"
            gui_mgr.save_chat_state()

            fresh = _FakeAgent(workspace)
            fresh_mgr = ChatStateManager(fresh, "chats.json")
            fresh_mgr.load_chat_state()
            chat_a = fresh_mgr.find_chat_by_id("chat-a")
            contents = [m.get("content") for m in (chat_a.get("messages") or [])]
            self.assertEqual(contents, ["gui edit"])

    def test_save_does_not_delete_peer_created_record(self):
        # A record on disk that this process never loaded (a chat a peer
        # created) must survive this process's save-time stale sweep.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            gui = _FakeAgent(workspace)
            gui_mgr = ChatStateManager(gui, "chats.json")
            gui._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-a",
                "chats": [
                    self._make_chat("chat-a", "A", "2026-06-18 09:10:00", []),
                ],
            }
            gui.active_chat_id = "chat-a"
            gui_mgr.save_chat_state()

            # Peer creates chat-z directly on disk (record + index entry).
            peer_record = "ff00ff00ff00ff00ff00ff00ff00ff00.json"
            (workspace / "chats" / peer_record).write_text(
                json.dumps(
                    {
                        "id": "chat-z",
                        "name": "Peer",
                        "name_source": "manual",
                        "created_at": "",
                        "updated_at": "2026-06-18 09:15:00",
                        "messages": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            # GUI saves again (chat-z is unknown to it). The peer record
            # must NOT be swept.
            gui_mgr.save_chat_state()
            self.assertTrue((workspace / "chats" / peer_record).exists())


if __name__ == "__main__":
    unittest.main()
