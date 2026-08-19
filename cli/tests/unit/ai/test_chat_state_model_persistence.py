import tempfile
import threading
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from cli.agent import Agent
from cli.managers.chat_state_manager import CHAT_STATE_VERSION, ChatStateManager


def _chat_index_entry(chat):
    # Record files now live in ``<YYYY>/<MM>/<DD>/`` date directories of the
    # global chats root; index entries carry the full relative path.
    record_file = chat.get("_record_file") or (
        f"2026/01/02/0123456789abcdef0123456789abcde{len(str(chat['id'])) % 10}.json"
    )
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


def _write_chat_store(workspace: Path, payload, workspace_id: str = "", index_name: str = ""):
    chats_dir = workspace / "chats"
    chats_dir.mkdir(parents=True, exist_ok=True)
    chats = [c for c in payload.get("chats", []) if isinstance(c, dict)]
    index = {
        "version": CHAT_STATE_VERSION,
        "active": payload.get("active", ""),
        "chats": [_chat_index_entry(c) for c in chats],
    }
    if workspace_id:
        index["workspace_id"] = workspace_id
    for chat in chats:
        record_file = _chat_index_entry(chat)["record_file"]
        record_payload = {k: v for k, v in chat.items() if not str(k).startswith("_")}
        record_path = chats_dir / record_file
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps(record_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    index_name = (
        index_name or (f"{workspace_id}.json" if workspace_id else "chats.json")
    )
    (chats_dir / index_name).write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_chat_index(workspace: Path, workspace_id: str = ""):
    chats_dir = workspace / "chats"
    if workspace_id:
        index_name = f"{workspace_id}.json"
        path = chats_dir / index_name
    else:
        path = chats_dir / "chats.json"
        if not path.exists():
            # Auto-discover the per-workspace index: tests set
            # ``agent.workspace_id`` and the manager then writes ``<id>.json``.
            candidates = sorted(
                p
                for p in chats_dir.glob("*.json")
                if p.name != "chats.json" and not p.name.startswith("chats.json.")
            )
            if candidates:
                path = candidates[0]
    return json.loads(path.read_text(encoding="utf-8"))


def _read_first_chat_record(workspace: Path):
    index = _read_chat_index(workspace)
    record_file = index["chats"][0]["record_file"]
    return json.loads((workspace / "chats" / record_file).read_text(encoding="utf-8"))


def _assert_hash_record_file(testcase, record_file: str):
    testcase.assertRegex(record_file, r"^\d{4}/\d{2}/\d{2}/[0-9a-f]{32}\.json$")
    testcase.assertNotEqual(record_file, "chat-1.json")


class _FakeAgent:
    def __init__(self, workspace: Path):
        self.workspace_config_dir = workspace
        self._chats_root_override = workspace / "chats"
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
        self.workspace_id = ""

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
    def test_load_file_changes_preserves_new_ref_keyed_dict_format(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeAgent(Path(td))
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": 1,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Chat 1",
                        "_record_file": "2026/01/02/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json",
                    }
                ],
            }
            changes_path = manager.chat_file_changes_path("chat-1")
            assert changes_path is not None
            changes_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "ref-123": {
                    "ref": "ref-123",
                    "totalFiles": 1,
                    "files": [{"filePath": "D:/workspace/demo.txt"}],
                }
            }
            changes_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            loaded = manager.load_file_changes("chat-1")

            self.assertEqual(loaded, payload)

    def test_reconcile_session_injected_from_history_tolerates_missing_skills_attr(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeAgent(Path(td))
            agent._canonical_skill_id = Agent._canonical_skill_id.__get__(agent, _FakeAgent)
            agent._session_injected_skills = set()
            agent._session_injected_mcp_prompts = set()
            agent.conversation_history = [
                {
                    "role": "system",
                    "content": (
                        "----- BEGIN SKILL PROMPT (skill_id=My-Skill) -----\n"
                        "body\n"
                        "----- END SKILL PROMPT -----"
                    ),
                }
            ]
            manager = ChatStateManager(agent, "chats.json")

            manager._reconcile_session_injected_from_history()

            self.assertEqual(agent._session_injected_skills, {"my-skill"})

    def test_reconcile_session_injected_drops_skills_absent_from_history(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeAgent(Path(td))
            agent._canonical_skill_id = Agent._canonical_skill_id.__get__(agent, _FakeAgent)
            agent._session_injected_skills = {"stale-skill", "my-skill"}
            agent._session_injected_mcp_prompts = set()
            agent.conversation_history = [
                {
                    "role": "user",
                    "content": (
                        "----- BEGIN SKILL PROMPT (skill_id=My-Skill) -----\n"
                        "body\n"
                        "----- END SKILL PROMPT -----"
                    ),
                }
            ]
            manager = ChatStateManager(agent, "chats.json")

            manager._reconcile_session_injected_from_history()

            # The stale skill (injected live before an edit rewound history) must
            # be dropped so its prompt can be re-injected when the message is
            # re-sent. Only skills whose marker survives in history remain.
            self.assertEqual(agent._session_injected_skills, {"my-skill"})

    def test_new_chat_entry_inherits_current_model(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeAgent(Path(td))
            manager = ChatStateManager(agent, "chats.json")
            entry = manager.new_chat_entry("chat-2", "Demo")
            self.assertEqual(entry.get("model_provider"), "openai")
            self.assertEqual(entry.get("model_name"), "gpt-4.1")

    def test_new_chat_entry_inherits_last_chat_reasoning_level(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeAgent(Path(td))
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": 1,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Latest",
                        "updated_at": "2026-01-02 10:00:00",
                        "model_provider": "openai",
                        "model_name": "gpt-4.1",
                        "reasoning_level": "high",
                    },
                    {
                        "id": "chat-0",
                        "name": "Older",
                        "updated_at": "2026-01-01 10:00:00",
                        "model_provider": "openai",
                        "model_name": "gpt-4.1",
                        "reasoning_level": "",
                    },
                ],
            }
            entry = manager.new_chat_entry("chat-2", "Demo")
            self.assertEqual(entry.get("model_provider"), "openai")
            self.assertEqual(entry.get("model_name"), "gpt-4.1")
            # A fresh chat inherits the last used chat's reasoning effort,
            # mirroring the model inheritance (GUI draft "inherit" semantics).
            self.assertEqual(entry.get("reasoning_level"), "high")

    def test_new_chat_entry_reasoning_empty_when_last_chat_has_none(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeAgent(Path(td))
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": 1,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Latest",
                        "updated_at": "2026-01-02 10:00:00",
                        "model_provider": "openai",
                        "model_name": "gpt-4.1",
                        "reasoning_level": "",
                    },
                ],
            }
            entry = manager.new_chat_entry("chat-2", "Demo")
            self.assertEqual(entry.get("reasoning_level"), "")

    def test_activate_chat_backfills_missing_model_and_calls_apply(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeAgent(Path(td))
            schedule_calls = []
            gui_usage_notifications = []

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
            agent._gui_context_usage_changed = lambda: gui_usage_notifications.append("notify")
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
                    }
                ],
            }
            msg = manager.activate_chat("chat-1", announce=False, clear_screen=False, print_history=False)
            self.assertEqual(msg, "")
            self.assertEqual(agent.applied_chat_model_calls, 1)
            self.assertEqual(agent.refresh_status_usage_calls, 1)
            self.assertEqual(len(schedule_calls), 1)
            self.assertEqual(schedule_calls[0]["context_hint"], "chat activated")
            self.assertEqual(len(gui_usage_notifications), 1)
            # Usage is no longer restored from the (removed) disk fields; it is
            # rebuilt by the runtime refresh triggered on activate. The fake
            # refresh is a no-op, so the snapshot stays at its default 0.
            self.assertEqual(agent._last_context_usage_percent, 0)
            self.assertEqual(agent._last_context_input_tokens, 0)
            self.assertEqual(agent._last_context_window, 0)
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

    def test_save_round_trips_workspace_id(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-A"
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-1",
                "workspace_id": "ws-A",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Chat 1",
                        "_record_file": "2026/01/02/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json",
                    }
                ],
            }
            manager.save_chat_state()
            index = _read_chat_index(workspace, "ws-A")
            self.assertEqual(index.get("workspace_id"), "ws-A")

    def test_save_refuses_when_disk_index_belongs_to_other_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            _write_chat_store(
                workspace,
                {"active": "chat-1", "chats": [{"id": "chat-1", "name": "Existing"}]},
                workspace_id="ws-B",
                index_name="ws-A.json",
            )
            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-A"
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-1",
                "workspace_id": "ws-A",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Foreign",
                        "_record_file": "2026/01/02/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.json",
                    }
                ],
            }
            manager.save_chat_state()
            index = _read_chat_index(workspace, "ws-A")
            # The on-disk index must be untouched — no whole-index replacement.
            self.assertEqual(index.get("workspace_id"), "ws-B")
            self.assertEqual(index["chats"][0]["name"], "Existing")
            # And no foreign record file may have been copied over.
            self.assertFalse(
                (workspace / "chats" / "2026/01/02/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.json").exists()
            )

    def test_save_refuses_when_in_memory_state_belongs_to_other_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            _write_chat_store(
                workspace,
                {"active": "chat-1", "chats": [{"id": "chat-1", "name": "Existing"}]},
                workspace_id="ws-A",
                index_name="ws-A.json",
            )
            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-A"
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-1",
                "workspace_id": "ws-B",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Foreign",
                        "_record_file": "2026/01/02/cccccccccccccccccccccccccccccccc.json",
                    }
                ],
            }
            manager.save_chat_state()
            index = _read_chat_index(workspace, "ws-A")
            self.assertEqual(index["chats"][0]["name"], "Existing")
            self.assertFalse(
                (workspace / "chats" / "2026/01/02/cccccccccccccccccccccccccccccccc.json").exists()
            )

    def test_load_quarantines_foreign_workspace_index(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            _write_chat_store(
                workspace,
                {"active": "chat-1", "chats": [{"id": "chat-1", "name": "Foreign"}]},
                workspace_id="ws-B",
                index_name="ws-A.json",
            )
            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-A"
            manager = ChatStateManager(agent, "chats.json")
            manager.load_chat_state()
            index = _read_chat_index(workspace, "ws-A")
            # The workspace recovers with a fresh index stamped for itself.
            self.assertEqual(index.get("workspace_id"), "ws-A")
            self.assertEqual(agent._chat_state.get("workspace_id"), "ws-A")
            # The foreign index is preserved for forensics, never deleted.
            backups = list((workspace / "chats").glob("ws-A.json.foreign-ws.ws-B.*"))
            self.assertEqual(len(backups), 1)

    def test_load_chat_state_snapshot_refuses_foreign_index(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            _write_chat_store(
                workspace,
                {"active": "chat-1", "chats": [{"id": "chat-1", "name": "Foreign"}]},
                workspace_id="ws-B",
                index_name="ws-A.json",
            )
            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-A"
            manager = ChatStateManager(agent, "chats.json")
            snapshot = manager.load_chat_state_snapshot(
                workspace, expected_workspace_id="ws-A"
            )
            # The foreign chat list must NOT be loaded; the snapshot falls back
            # to a fresh default stamped for the expected workspace.
            self.assertEqual(snapshot.get("workspace_id"), "ws-A")
            self.assertEqual([c.get("id") for c in snapshot.get("chats", [])], ["chat-1"])

    def test_load_chat_state_snapshot_lazy_records_skips_record_reads(self):
        # The background-workspace persistence context must not read every
        # record file under its lock (multi-MB records stalled select_chat by
        # ~0.3-0.8s); lazy placeholders from the index are sufficient because
        # save_chat_state hydrates them on demand before writing.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            manager = ChatStateManager(agent, "chats.json")
            agent.workspace_id = "ws-A"
            _write_chat_store(
                workspace,
                {
                    "active": "chat-1",
                    "chats": [
                        {
                            "id": "chat-1",
                            "name": "Big",
                            "updated_at": "2026-07-08 15:00:00",
                            "messages": [
                                {"role": "user", "content": "x", "created_at": "2026-07-08 15:10:00"}
                            ],
                        }
                    ],
                },
                workspace_id="ws-A",
            )

            snapshot = manager.load_chat_state_snapshot(
                workspace, expected_workspace_id="ws-A", lazy_records=True
            )

            chat = snapshot["chats"][0]
            self.assertEqual(chat.get("id"), "chat-1")
            self.assertEqual(chat.get("name"), "Big")
            self.assertTrue(chat.get("_lazy_placeholder"))
            self.assertEqual(chat.get("messages"), [])
            self.assertTrue(chat.get("_record_file"))

    def test_save_hydrates_placeholder_preserving_pending_inputs(self):
        # A lazy placeholder that received a pending input queue before the
        # save must keep that queue when save_chat_state hydrates the full
        # record from disk (background-workspace ctx scenario).
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            manager = ChatStateManager(agent, "chats.json")
            agent.workspace_id = "ws-A"
            _write_chat_store(
                workspace,
                {
                    "active": "chat-1",
                    "chats": [
                        {
                            "id": "chat-1",
                            "name": "Big",
                            "updated_at": "2026-07-08 15:00:00",
                            "messages": [
                                {"role": "user", "content": "persisted", "created_at": "2026-07-08 15:10:00"}
                            ],
                        }
                    ],
                },
                workspace_id="ws-A",
            )
            snapshot = manager.load_chat_state_snapshot(
                workspace, expected_workspace_id="ws-A", lazy_records=True
            )
            agent._chat_state = snapshot
            agent.active_chat_id = "chat-1"
            chat = manager.find_chat_by_id("chat-1")
            self.assertTrue(chat.get("_lazy_placeholder"))
            chat["pending_inputs"] = ["draft message"]
            manager.mark_chat_dirty("chat-1")

            manager.save_chat_state()

            hydrated = manager.find_chat_by_id("chat-1")
            self.assertFalse(hydrated.get("_lazy_placeholder"))
            self.assertEqual(hydrated.get("pending_inputs"), ["draft message"])
            self.assertEqual(
                [m.get("content") for m in hydrated.get("messages") or []],
                ["persisted"],
            )

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
                    }
                ],
            }
            save_calls = []
            manager.save_chat_state = lambda: save_calls.append("saved")

            ok = manager.clear_chat_context("chat-1")

            self.assertTrue(ok)
            chat = manager.find_chat_by_id("chat-1")
            self.assertEqual(chat.get("messages"), [])
            # Usage is no longer persisted on the chat record; clearing resets
            # the in-memory snapshot instead, so the fields are absent here.
            self.assertNotIn("context_usage_percent", chat)
            self.assertNotIn("context_input_tokens", chat)
            self.assertNotIn("context_window", chat)
            self.assertEqual(agent._last_context_usage_percent, 0)
            self.assertEqual(agent._last_context_input_tokens, 0)
            self.assertEqual(save_calls, ["saved"])

    def test_persist_active_chat_usage_snapshot_notifies_gui_without_persisting_usage(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            gui_usage_notifications = []
            agent._gui_context_usage_changed = lambda: gui_usage_notifications.append("notify")
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
                    }
                ],
            }
            agent.active_chat_id = "chat-1"
            agent._last_context_usage_percent = 3
            agent._last_context_input_tokens = 4321
            agent._last_context_window = 128000

            manager.persist_active_chat_usage_snapshot()

            chat = manager.find_chat_by_id("chat-1")
            self.assertIsNotNone(chat)
            # Usage is no longer written to the chat record; the in-memory
            # snapshot (set by the runtime refresh) is authoritative, and
            # ``persist`` only surfaces it to the GUI.
            self.assertNotIn("context_usage_percent", chat)
            self.assertNotIn("context_input_tokens", chat)
            self.assertNotIn("context_window", chat)
            self.assertEqual(len(gui_usage_notifications), 1)

    def test_persist_active_chat_usage_snapshot_record_lacks_usage_after_sync(self):
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
                    }
                ],
            }
            agent.active_chat_id = "chat-1"
            agent._last_context_usage_percent = 3
            agent._last_context_input_tokens = 4321
            agent._last_context_window = 128000
            agent.conversation_history = [
                {"role": "user", "content": "hello", "created_at": "2026-07-08 15:10:00"}
            ]

            # The usage snapshot is only surfaced to the GUI; a subsequent sync
            # (which does persist the record) must not reintroduce the fields.
            manager.persist_active_chat_usage_snapshot()
            manager.sync_active_chat_messages()

            saved_chat = _read_first_chat_record(workspace)
            self.assertNotIn("context_usage_percent", saved_chat)
            self.assertNotIn("context_input_tokens", saved_chat)
            self.assertNotIn("context_window", saved_chat)

    def test_persist_active_chat_usage_snapshot_does_not_touch_updated_at_when_usage_is_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            manager = ChatStateManager(agent, "chats.json")
            original_updated_at = "2026-07-08 15:20:00"
            agent._chat_state = {
                "version": 2,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Demo",
                        "name_source": "manual",
                        "created_at": "",
                        "updated_at": original_updated_at,
                        "model_provider": "openai",
                        "model_name": "gpt-4.1",
                        "messages": [],
                        "context_usage_percent": 3,
                        "context_input_tokens": 4321,
                        "context_window": 128000,
                    }
                ],
            }
            agent.active_chat_id = "chat-1"
            agent._last_context_usage_percent = 3
            agent._last_context_input_tokens = 4321
            agent._last_context_window = 128000

            save_calls = []
            manager.save_chat_state = lambda: save_calls.append("saved")

            manager.persist_active_chat_usage_snapshot()

            chat = manager.find_chat_by_id("chat-1")
            self.assertIsNotNone(chat)
            self.assertEqual(chat.get("updated_at"), original_updated_at)
            self.assertEqual(save_calls, [])

    def test_persist_active_chat_usage_snapshot_does_not_persist_usage_when_changed(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            manager = ChatStateManager(agent, "chats.json")
            original_updated_at = "2026-07-08 15:20:00"
            agent._chat_state = {
                "version": 2,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Demo",
                        "name_source": "manual",
                        "created_at": "",
                        "updated_at": original_updated_at,
                        "model_provider": "openai",
                        "model_name": "gpt-4.1",
                        "messages": [],
                    }
                ],
            }
            agent.active_chat_id = "chat-1"
            agent._last_context_usage_percent = 3
            agent._last_context_input_tokens = 4321
            agent._last_context_window = 128000

            manager.persist_active_chat_usage_snapshot()

            chat = manager.find_chat_by_id("chat-1")
            self.assertIsNotNone(chat)
            # Changing usage must not write it to the chat record.
            self.assertNotIn("context_usage_percent", chat)
            self.assertNotIn("context_input_tokens", chat)
            self.assertNotIn("context_window", chat)
            self.assertEqual(chat.get("updated_at"), original_updated_at)

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

    def test_sync_active_chat_messages_persists_messages_without_usage_fields(self):
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
                    }
                ],
            }
            agent.active_chat_id = "chat-1"
            agent._last_context_window = 131072
            agent._last_context_input_tokens = 29516
            agent._last_context_usage_percent = 23
            agent.conversation_history = [
                {
                    "role": "user",
                    "content": "查看我的Codex用量",
                    "_token_count": 14,
                },
                {
                    "role": "assistant",
                    "content": '{"tool_calls": [{"id": "a"}]}',
                    "_cache_stats": {"input_tokens": 15097},
                    "_output_tokens": 135,
                    "_reasoning_tokens": 0,
                    "_token_count_includes_reasoning": False,
                },
                {
                    "role": "user",
                    "content": "...",
                    "_internal": True,
                    "_token_count": 981,
                },
                {
                    "role": "assistant",
                    "content": '{"tool_calls": [{"id": "b"}]}',
                    "_cache_stats": {"input_tokens": 16060},
                    "_output_tokens": 253,
                    "_reasoning_tokens": 0,
                    "_token_count_includes_reasoning": False,
                },
            ]

            manager.sync_active_chat_messages()

            chat = manager.find_chat_by_id("chat-1")
            self.assertIsNotNone(chat)
            # Messages are persisted (including the internal flag and cache
            # stats needed for later usage recompute).
            msgs = list(chat.get("messages") or [])
            self.assertEqual(len(msgs), 4)
            self.assertTrue(bool(msgs[2].get("_internal", False)))
            self.assertIsInstance(msgs[1].get("_cache_stats"), dict)
            # Usage is no longer written to the chat record; the in-memory
            # snapshot (rebuilt from history by the runtime refresh) is used.
            self.assertNotIn("context_usage_percent", chat)
            self.assertNotIn("context_input_tokens", chat)
            self.assertNotIn("context_window", chat)

            saved_chat = _read_first_chat_record(workspace)
            self.assertNotIn("context_usage_percent", saved_chat)
            self.assertNotIn("context_input_tokens", saved_chat)
            self.assertNotIn("context_window", saved_chat)

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

    def test_sync_active_chat_messages_does_not_touch_updated_at_when_messages_are_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            manager = ChatStateManager(agent, "chats.json")
            original_updated_at = "2026-07-08 15:20:00"
            agent._chat_state = {
                "version": 2,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Main",
                        "name_source": "manual",
                        "created_at": "",
                        "updated_at": original_updated_at,
                        "model_provider": "openai",
                        "model_name": "gpt-4.1",
                        "messages": [
                            {"role": "user", "content": "persisted", "created_at": "2026-07-08 15:10:00"}
                        ],
                        "context_usage_percent": 0,
                        "context_input_tokens": 0,
                        "context_window": 0,
                    }
                ],
            }
            agent.active_chat_id = "chat-1"
            agent.conversation_history = [
                {"role": "user", "content": "persisted", "created_at": "2026-07-08 15:10:00"}
            ]

            save_calls = []
            manager.save_chat_state = lambda: save_calls.append("saved")

            manager.sync_active_chat_messages()

            chat = manager.find_chat_by_id("chat-1")
            self.assertIsNotNone(chat)
            self.assertEqual(chat.get("updated_at"), original_updated_at)
            self.assertEqual(save_calls, [])

    def test_sync_active_chat_messages_sets_updated_at_to_current_time(self):
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
                        "updated_at": "2026-07-08 15:00:00",
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
                {"role": "user", "content": "persisted", "created_at": "2026-07-08 15:10:00"},
                {"role": "assistant", "content": "reply", "created_at": "2026-07-08 15:12:34"},
            ]

            fake_now = "2026-07-23 16:00:00"
            with patch.object(ChatStateManager, "_now_text", return_value=fake_now):
                manager.sync_active_chat_messages()

            chat = manager.find_chat_by_id("chat-1")
            self.assertIsNotNone(chat)
            self.assertEqual(chat.get("updated_at"), fake_now)

    def test_sync_active_chat_messages_refuses_empty_history_over_in_memory_messages(self):
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
                        "updated_at": "2026-07-08 15:00:00",
                        "model_provider": "openai",
                        "model_name": "gpt-4.1",
                        "messages": [
                            {"role": "user", "content": "persisted", "created_at": "2026-07-08 15:10:00"}
                        ],
                    }
                ],
            }
            agent.active_chat_id = "chat-1"
            agent.conversation_history = []

            save_calls = []
            manager.save_chat_state = lambda: save_calls.append("saved")

            manager.sync_active_chat_messages()

            # An empty live history must never truncate a chat that already
            # holds messages in memory (stale active_chat_id after a workspace
            # switch whose conversation_history was never hydrated).
            self.assertEqual(save_calls, [])
            chat = manager.find_chat_by_id("chat-1")
            self.assertEqual(len(chat.get("messages") or []), 1)

    def test_sync_active_chat_messages_refuses_empty_history_over_disk_record(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            manager = ChatStateManager(agent, "chats.json")
            record_file = "2026/01/02/0123456789abcdef0123456789abcde6.json"
            agent._chat_state = {
                "version": 2,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Main",
                        "name_source": "manual",
                        "created_at": "",
                        "updated_at": "2026-07-08 15:00:00",
                        "model_provider": "openai",
                        "model_name": "gpt-4.1",
                        "messages": [],
                        "_record_file": record_file,
                    }
                ],
            }
            agent.active_chat_id = "chat-1"
            agent.conversation_history = []
            # The on-disk record holds a real conversation (e.g. loaded lazily
            # as an empty placeholder during a workspace switch); the empty
            # in-memory session must never overwrite it.
            record_path = workspace / "chats" / record_file
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_text(
                json.dumps(
                    {
                        "id": "chat-1",
                        "name": "Main",
                        "updated_at": "2026-07-08 15:00:00",
                        "messages": [
                            {"role": "user", "content": "persisted", "created_at": "2026-07-08 15:10:00"},
                            {"role": "assistant", "content": "reply", "created_at": "2026-07-08 15:11:00"},
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            save_calls = []
            manager.save_chat_state = lambda: save_calls.append("saved")

            manager.sync_active_chat_messages()

            self.assertEqual(save_calls, [])
            disk_record = json.loads(
                (workspace / "chats" / record_file).read_text(encoding="utf-8")
            )
            self.assertEqual(len(disk_record.get("messages") or []), 2)
            chat = manager.find_chat_by_id("chat-1")
            self.assertEqual(chat.get("messages"), [])

    def test_sync_active_chat_messages_still_clears_truly_empty_fresh_chat(self):
        # A brand-new chat that never persisted anything may still be synced
        # with an empty history: there is no real record to protect.
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
                    }
                ],
            }
            agent.active_chat_id = "chat-1"
            agent.conversation_history = []

            manager.sync_active_chat_messages()

            chat = manager.find_chat_by_id("chat-1")
            self.assertEqual(chat.get("messages"), [])

    def test_sync_active_chat_messages_allow_empty_explicitly_clears_record(self):
        # The explicit edit path (``/chat edit`` truncating the first user
        # message) passes ``allow_empty=True``: the empty history is intended
        # and must clear the on-disk record, bypassing the data-safety guard.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            manager = ChatStateManager(agent, "chats.json")
            record_file = "2026/01/02/0123456789abcdef0123456789abcde6.json"
            agent._chat_state = {
                "version": 2,
                "active": "chat-1",
                "chats": [
                    {
                        "id": "chat-1",
                        "name": "Main",
                        "name_source": "manual",
                        "created_at": "",
                        "updated_at": "2026-07-08 15:00:00",
                        "model_provider": "openai",
                        "model_name": "gpt-4.1",
                        "messages": [
                            {"role": "user", "content": "persisted", "created_at": "2026-07-08 15:10:00"}
                        ],
                        "_record_file": record_file,
                    }
                ],
            }
            agent.active_chat_id = "chat-1"
            agent.conversation_history = []
            record_path = workspace / "chats" / record_file
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_text(
                json.dumps(
                    {"id": "chat-1", "name": "Main", "updated_at": "2026-07-08 15:00:00", "messages": []},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            save_calls = []
            manager.save_chat_state = lambda: save_calls.append("saved")

            manager.sync_active_chat_messages(allow_empty=True)

            self.assertEqual(save_calls, ["saved"])
            chat = manager.find_chat_by_id("chat-1")
            self.assertEqual(chat.get("messages"), [])


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
            peer_record = "2026/06/18/ff00ff00ff00ff00ff00ff00ff00ff00.json"
            peer_path = workspace / "chats" / peer_record
            peer_path.parent.mkdir(parents=True, exist_ok=True)
            peer_path.write_text(
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


    def test_workspace_switch_to_missing_index_does_not_sweep_previous_workspace_records(self):
        # Regression: an agent that loaded workspace A's chats then switched
        # to workspace B, whose index does not exist, used to treat A's
        # record files as stale ("not in current_index, known=True") and
        # delete them all. Every workspace shares ONE global chats root, so
        # the switch's save must never sweep records that belonged to the
        # previously focused workspace. Reproduces the "GUI switch wiped
        # every chat record" data-loss bug.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            _write_chat_store(
                workspace,
                {
                    "active": "chat-a",
                    "chats": [
                        self._make_chat("chat-a", "A", "2026-06-18 09:10:00", []),
                        self._make_chat("chat-bb", "B", "2026-06-18 09:05:00", []),
                    ],
                },
                workspace_id="ws-A",
            )
            index = _read_chat_index(workspace, workspace_id="ws-A")
            rfs = {c["id"]: c["record_file"] for c in index["chats"]}

            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-A"
            manager = ChatStateManager(agent, "chats.json")
            manager.load_chat_state()
            self.assertEqual(
                set(getattr(agent, "_known_record_files_seen", set())),
                set(rfs.values()),
            )

            # GUI workspace-switch fast path: target workspace has no index,
            # no default chat is created, and the caller saves right after.
            agent.workspace_id = "ws-B"
            manager.load_chat_state(create_default_chat=False)
            self.assertEqual(getattr(agent, "_known_record_files_seen", None), set())
            manager.save_chat_state()

            for rf in rfs.values():
                self.assertTrue(
                    (workspace / "chats" / rf).exists(),
                    f"record {rf} of the previously focused workspace was swept",
                )

    def test_workspace_switch_to_empty_index_does_not_sweep_previous_workspace_records(self):
        # Same regression as above, but workspace B's index EXISTS and lists
        # zero chats (the on-disk empty-index branch of ``load_chat_state``).
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            _write_chat_store(
                workspace,
                {
                    "active": "chat-a",
                    "chats": [
                        self._make_chat("chat-a", "A", "2026-06-18 09:10:00", [])
                    ],
                },
                workspace_id="ws-A",
            )
            rf = _read_chat_index(workspace, workspace_id="ws-A")["chats"][0][
                "record_file"
            ]
            _write_chat_store(
                workspace, {"active": "", "chats": []}, workspace_id="ws-B"
            )

            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-A"
            manager = ChatStateManager(agent, "chats.json")
            manager.load_chat_state()
            agent.workspace_id = "ws-B"
            manager.load_chat_state(create_default_chat=False)
            self.assertEqual(getattr(agent, "_known_record_files_seen", None), set())
            manager.save_chat_state()

            self.assertTrue((workspace / "chats" / rf).exists())

    def test_sweep_skips_record_still_referenced_by_another_workspace_index(self):
        # Defense-in-depth: even if the seen-set still carries a record that
        # is missing from the current in-memory index, the sweep must not
        # delete it when ANY on-disk index (a peer process's workspace,
        # sharing the same global chats root) still references it.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            _write_chat_store(
                workspace,
                {
                    "active": "chat-x",
                    "chats": [
                        self._make_chat("chat-x", "X", "2026-06-18 09:10:00", [])
                    ],
                },
                workspace_id="ws-C",
            )
            rf = _read_chat_index(workspace, workspace_id="ws-C")["chats"][0][
                "record_file"
            ]
            record_path = workspace / "chats" / rf
            self.assertTrue(record_path.exists())

            # This process has an empty in-memory index but (as after a
            # workspace switch) still remembers the record file.
            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-D"
            agent._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "",
                "chats": [],
            }
            agent._known_record_files_seen = {rf}
            manager = ChatStateManager(agent, "chats.json")
            manager.save_chat_state()

            self.assertTrue(
                record_path.exists(), "peer-referenced record was swept"
            )

    def test_sweep_still_deletes_chat_removed_in_this_process(self):
        # The sweep keeps working for its intended case: a chat the user
        # deleted HERE (removed from the in-memory index, no on-disk index
        # references it anymore) has its record file cleaned up.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            _write_chat_store(
                workspace,
                {
                    "active": "chat-a",
                    "chats": [
                        self._make_chat("chat-a", "A", "2026-06-18 09:10:00", []),
                        self._make_chat("chat-bb", "B", "2026-06-18 09:05:00", []),
                    ],
                },
                workspace_id="ws-A",
            )
            index = _read_chat_index(workspace, workspace_id="ws-A")
            rfs = {c["id"]: c["record_file"] for c in index["chats"]}

            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-A"
            manager = ChatStateManager(agent, "chats.json")
            manager.load_chat_state()
            # The user deletes chat-bb in this process.
            agent._chat_state["chats"] = [
                c for c in agent._chat_state["chats"] if c["id"] != "chat-bb"
            ]
            manager.save_chat_state()

            self.assertFalse(
                (workspace / "chats" / rfs["chat-bb"]).exists(),
                "locally-deleted chat record should be swept",
            )
            self.assertTrue((workspace / "chats" / rfs["chat-a"]).exists())


    def test_history_context_usage_not_persisted_on_record(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            agent = _FakeAgent(Path(td))
            manager = ChatStateManager(agent, "chats.json")
            entry = manager.new_chat_entry("chat-x")
            self.assertNotIn("context_usage_percent", entry)
            self.assertNotIn("context_input_tokens", entry)
            self.assertNotIn("context_window", entry)

    def _read_chat_record(self, workspace: Path, chat_id: str) -> dict:
        index = _read_chat_index(workspace)
        entry = next(c for c in index["chats"] if c["id"] == chat_id)
        return json.loads(
            (workspace / "chats" / entry["record_file"]).read_text(encoding="utf-8")
        )

    def test_set_chat_unread_persists_to_record_and_index(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-1"
            manager = ChatStateManager(agent, "chats.json")
            c1 = manager.new_chat_entry("chat-1", name="One")
            c2 = manager.new_chat_entry("chat-2", name="Two")
            agent._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-1",
                "chats": [c1, c2],
            }
            manager.save_chat_state()

            self.assertFalse(bool(manager.find_chat_by_id("chat-2").get("has_unread", False)))

            manager.set_chat_unread("chat-2", True)

            self.assertTrue(bool(manager.find_chat_by_id("chat-2").get("has_unread", False)))
            index = _read_chat_index(workspace)
            entry = next(c for c in index["chats"] if c["id"] == "chat-2")
            self.assertTrue(bool(entry.get("has_unread", False)))
            record = self._read_chat_record(workspace, "chat-2")
            self.assertTrue(bool(record.get("has_unread", False)))

            # Clearing is persisted too.
            manager.set_chat_unread("chat-2", False)
            self.assertFalse(bool(manager.find_chat_by_id("chat-2").get("has_unread", False)))
            record = self._read_chat_record(workspace, "chat-2")
            self.assertFalse(bool(record.get("has_unread", False)))

    def test_set_chat_unread_restores_across_restart(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-1"
            manager = ChatStateManager(agent, "chats.json")
            c1 = manager.new_chat_entry("chat-1", name="One")
            c2 = manager.new_chat_entry("chat-2", name="Two")
            agent._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-1",
                "chats": [c1, c2],
            }
            manager.save_chat_state()
            manager.set_chat_unread("chat-2", True)

            # Simulate a restart: a fresh agent + manager over the same files.
            agent2 = _FakeAgent(workspace)
            agent2.workspace_id = "ws-1"
            manager2 = ChatStateManager(agent2, "chats.json")
            manager2.load_chat_state(create_default_chat=False)

            # chat-2's unread flag survives the restart (persistent blue dot).
            self.assertTrue(bool(manager2.find_chat_by_id("chat-2").get("has_unread", False)))
            # The chat being displayed on load (chat-1, the active chat) is
            # cleared — it is being viewed, so no dot.
            self.assertFalse(bool(manager2.find_chat_by_id("chat-1").get("has_unread", False)))

    def test_message_timestamp_sequence_edges(self):
        from cli.managers.chat_state_manager import (
            _message_timestamp_diffs as diffs,
            _message_timestamp_sequence_allowed as allowed,
        )

        def msg(ts):
            return {"role": "user", "content": "x", "created_at": ts}

        # Identical / append / truncate / full-clear / first-create sequences.
        self.assertTrue(allowed([msg("t1")], [msg("t1")]))
        self.assertTrue(allowed([msg("t1")], [msg("t1"), msg("t2")]))
        self.assertTrue(allowed([msg("t1"), msg("t2")], [msg("t1")]))
        self.assertTrue(allowed([msg("t1")], []))
        self.assertTrue(allowed([], [msg("t1")]))
        # A single shared-position difference (one message concurrently edited
        # by an authoritative in-flight/owned chat) is tolerated.
        self.assertTrue(allowed([msg("t1"), msg("t2")], [msg("t1"), msg("t9")]))
        self.assertTrue(allowed([msg("t1"), msg("t2"), msg("t3")], [msg("t1"), msg("t9"), msg("t3")]))
        # Two or more diverging positions (whole-content replacement from
        # another workspace's same-id chat) are refused.
        self.assertFalse(allowed([msg("t1"), msg("t2")], [msg("t9"), msg("t8")]))
        # Reordered entries diverge at the first position.
        self.assertEqual(diffs([msg("t1"), msg("t2")], [msg("t2"), msg("t1")]), (0, 2))
        # Missing disk timestamp acts as a wildcard (legacy record).
        self.assertTrue(allowed([{"role": "user", "content": "legacy"}], [msg("t1")]))
        # Same-second appends are still a prefix.
        self.assertTrue(allowed([msg("t1")], [msg("t1"), msg("t1")]))
        # Non-list inputs are treated as empty sequences.
        self.assertTrue(allowed(None, [msg("t1")]))
        self.assertTrue(allowed([msg("t1")], None))
        self.assertEqual(diffs(None, [msg("t1")]), (-1, 0))

    def test_save_refuses_cross_workspace_timestamp_replacement(self):
        # A chat whose in-memory messages were replaced by another workspace's
        # same-id chat carries a divergent created_at sequence. The save must
        # refuse to overwrite the on-disk record — even though the id check
        # and the first_user_message_at anchor check pass, because the anchor
        # was refreshed from the contaminated content — and log an error with
        # a stack trace.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-1"
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-a",
                "chats": [
                    self._make_chat(
                        "chat-a",
                        "A",
                        "2026-06-18 09:10:00",
                        [
                            {"role": "user", "content": "real msg", "created_at": "2026-06-18 09:10:00"},
                            {"role": "assistant", "content": "real reply", "created_at": "2026-06-18 09:20:00"},
                        ],
                    )
                ],
            }
            agent.active_chat_id = "chat-a"
            manager.save_chat_state()

            # Simulate cross-workspace contamination: the in-memory chat now
            # holds another workspace's messages. The first-user anchor is
            # refreshed from the contaminated content (like sync_active_chat
            # does), so the existing anchor guard cannot catch it — only the
            # timestamp-sequence guard can.
            chat_a = manager.find_chat_by_id("chat-a")
            chat_a["messages"] = [
                {"role": "user", "content": "foreign", "created_at": "2026-06-18 10:00:00"},
                {"role": "assistant", "content": "foreign reply", "created_at": "2026-06-18 10:01:00"},
            ]
            chat_a["first_user_message_at"] = "2026-06-18 10:00:00"
            chat_a["updated_at"] = "2026-06-18 09:30:00"

            with self.assertLogs("codewood.chat_state", level="ERROR") as cm:
                manager.save_chat_state()

            # The on-disk record must still hold the original message.
            record = _read_first_chat_record(workspace)
            contents = [m.get("content") for m in (record.get("messages") or [])]
            self.assertEqual(contents, ["real msg", "real reply"])
            self.assertTrue(any("created_at sequence diverges" in line for line in cm.output))

    def test_save_allows_appended_messages_with_newer_timestamps(self):
        # Appending new messages (old sequence is a prefix of the new one) is
        # the normal save path and must always be written.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-1"
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-a",
                "chats": [
                    self._make_chat(
                        "chat-a",
                        "A",
                        "2026-06-18 09:10:00",
                        [{"role": "user", "content": "hello", "created_at": "2026-06-18 09:10:00"}],
                    )
                ],
            }
            agent.active_chat_id = "chat-a"
            manager.save_chat_state()

            chat_a = manager.find_chat_by_id("chat-a")
            chat_a["messages"].append(
                {"role": "assistant", "content": "reply", "created_at": "2026-06-18 09:20:00"}
            )
            chat_a["updated_at"] = "2026-06-18 09:20:00"
            manager.save_chat_state()

            record = _read_first_chat_record(workspace)
            contents = [m.get("content") for m in (record.get("messages") or [])]
            self.assertEqual(contents, ["hello", "reply"])

    def test_save_allows_truncation_of_latest_messages(self):
        # Removing the latest N messages (new sequence is a prefix of the
        # old) is a legitimate save (e.g. clear-chat / undo), not a sign of
        # cross-workspace contamination.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-1"
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-a",
                "chats": [
                    self._make_chat(
                        "chat-a",
                        "A",
                        "2026-06-18 09:10:00",
                        [
                            {"role": "user", "content": "hello", "created_at": "2026-06-18 09:10:00"},
                            {"role": "assistant", "content": "reply", "created_at": "2026-06-18 09:20:00"},
                        ],
                    )
                ],
            }
            agent.active_chat_id = "chat-a"
            manager.save_chat_state()

            chat_a = manager.find_chat_by_id("chat-a")
            chat_a["messages"] = [
                {"role": "user", "content": "hello", "created_at": "2026-06-18 09:10:00"}
            ]
            chat_a["updated_at"] = "2026-06-18 09:25:00"
            manager.save_chat_state()

            record = _read_first_chat_record(workspace)
            contents = [m.get("content") for m in (record.get("messages") or [])]
            self.assertEqual(contents, ["hello"])

    def test_save_allows_in_place_edit_with_unchanged_timestamps(self):
        # Edits that keep created_at stable (pseudo-tool-call retries,
        # thinking injection, plan updates) must not be rejected: the guard
        # compares the timestamp sequence, never the message content.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-1"
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-a",
                "chats": [
                    self._make_chat(
                        "chat-a",
                        "A",
                        "2026-06-18 09:10:00",
                        [
                            {"role": "user", "content": "hello", "created_at": "2026-06-18 09:10:00"},
                            {"role": "assistant", "content": "v1", "created_at": "2026-06-18 09:20:00"},
                        ],
                    )
                ],
            }
            agent.active_chat_id = "chat-a"
            manager.save_chat_state()

            chat_a = manager.find_chat_by_id("chat-a")
            chat_a["messages"][1]["content"] = "v2"
            chat_a["updated_at"] = "2026-06-18 09:25:00"
            manager.save_chat_state()

            record = _read_first_chat_record(workspace)
            contents = [m.get("content") for m in (record.get("messages") or [])]
            self.assertEqual(contents, ["hello", "v2"])

    def test_save_tolerates_legacy_records_missing_created_at(self):
        # Records that predate created_at have no timestamps on disk. The
        # sync assigns fresh ones in memory; the guard must treat the missing
        # disk timestamp as a wildcard instead of refusing the save.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-1"
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-a",
                "chats": [
                    self._make_chat(
                        "chat-a",
                        "A",
                        "2026-06-18 09:10:00",
                        [{"role": "user", "content": "legacy"}],
                    )
                ],
            }
            agent.active_chat_id = "chat-a"
            manager.save_chat_state()

            chat_a = manager.find_chat_by_id("chat-a")
            chat_a["messages"] = [
                {"role": "user", "content": "legacy", "created_at": "2026-06-18 09:10:00"},
                {"role": "assistant", "content": "new", "created_at": "2026-06-18 09:20:00"},
            ]
            chat_a["updated_at"] = "2026-06-18 09:20:00"
            manager.save_chat_state()

            record = _read_first_chat_record(workspace)
            contents = [m.get("content") for m in (record.get("messages") or [])]
            self.assertEqual(contents, ["legacy", "new"])

    def test_save_allows_full_clear_of_messages(self):
        # clear_chat_context empties the message list; the new (empty)
        # sequence is a prefix of the old one and must be written.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-1"
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-a",
                "chats": [
                    self._make_chat(
                        "chat-a",
                        "A",
                        "2026-06-18 09:10:00",
                        [{"role": "user", "content": "hello", "created_at": "2026-06-18 09:10:00"}],
                    )
                ],
            }
            agent.active_chat_id = "chat-a"
            manager.save_chat_state()

            chat_a = manager.find_chat_by_id("chat-a")
            chat_a["messages"] = []
            chat_a.pop("first_user_message_at", None)
            chat_a["updated_at"] = "2026-06-18 09:30:00"
            manager.save_chat_state()

            record = _read_first_chat_record(workspace)
            self.assertEqual(record.get("messages") or [], [])

    def test_lazy_load_skips_record_files_and_marks_placeholders(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-1"
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-a",
                "workspace_id": "ws-1",
                "chats": [
                    self._make_chat(
                        "chat-a",
                        "A",
                        "2026-06-18 09:10:00",
                        [{"role": "user", "content": "hello", "created_at": "2026-06-18 09:10:00"}],
                    ),
                    self._make_chat(
                        "chat-b",
                        "B",
                        "2026-06-18 09:11:00",
                        [{"role": "user", "content": "world", "created_at": "2026-06-18 09:11:00"}],
                    ),
                ],
            }
            manager.save_chat_state()

            # Wipe the in-memory state so a reload reads from disk.
            agent._chat_state = {"version": CHAT_STATE_VERSION, "active": "", "workspace_id": "ws-1", "chats": []}
            agent.conversation_history = []
            manager.load_chat_state(create_default_chat=False, lazy_records=True)

            chats = manager.chat_entries()
            self.assertEqual(len(chats), 2)
            # Placeholders carry the index summary but no messages.
            for c in chats:
                self.assertTrue(c.get("_lazy_placeholder"), f"chat {c['id']} not placeholder")
                self.assertEqual(c.get("messages") or [], [])
                self.assertEqual(c["id"] in ("chat-a", "chat-b"), True)
                self.assertTrue(str(c.get("_record_file") or "").endswith(".json"))

    def test_lazy_placeholder_save_does_not_truncate_disk_record(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-1"
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-a",
                "workspace_id": "ws-1",
                "chats": [
                    self._make_chat(
                        "chat-a",
                        "A",
                        "2026-06-18 09:10:00",
                        [{"role": "user", "content": "hello", "created_at": "2026-06-18 09:10:00"}],
                    ),
                ],
            }
            manager.save_chat_state()
            disk_record = _read_first_chat_record(workspace)
            self.assertEqual([m.get("content") for m in disk_record["messages"]], ["hello"])

            # Reload lazily, then mark the placeholder dirty (e.g. unread flag)
            # and save: the on-disk record must keep its messages.
            agent._chat_state = {"version": CHAT_STATE_VERSION, "active": "", "workspace_id": "ws-1", "chats": []}
            agent.conversation_history = []
            manager.load_chat_state(create_default_chat=False, lazy_records=True)
            chat_a = manager.find_chat_by_id("chat-a")
            self.assertTrue(chat_a.get("_lazy_placeholder"))
            manager.mark_chat_dirty("chat-a")
            manager.save_chat_state()

            record = _read_first_chat_record(workspace)
            self.assertEqual([m.get("content") for m in (record.get("messages") or [])], ["hello"])

    def test_refresh_hydrates_lazy_placeholder_to_full_record(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _FakeAgent(workspace)
            agent.workspace_id = "ws-1"
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state = {
                "version": CHAT_STATE_VERSION,
                "active": "chat-a",
                "workspace_id": "ws-1",
                "chats": [
                    self._make_chat(
                        "chat-a",
                        "A",
                        "2026-06-18 09:10:00",
                        [{"role": "user", "content": "hello", "created_at": "2026-06-18 09:10:00"}],
                    ),
                ],
            }
            manager.save_chat_state()

            agent._chat_state = {"version": CHAT_STATE_VERSION, "active": "", "workspace_id": "ws-1", "chats": []}
            agent.conversation_history = []
            manager.load_chat_state(create_default_chat=False, lazy_records=True)
            self.assertTrue(manager.find_chat_by_id("chat-a").get("_lazy_placeholder"))

            ok = manager.refresh_chat_record_from_disk("chat-a")
            self.assertTrue(ok)
            chat_a = manager.find_chat_by_id("chat-a")
            self.assertFalse(chat_a.get("_lazy_placeholder", False))
            self.assertEqual([m.get("content") for m in chat_a["messages"]], ["hello"])


if __name__ == "__main__":
    unittest.main()
