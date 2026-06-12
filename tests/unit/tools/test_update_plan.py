import json
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime
from pathlib import Path


if "ollama" not in sys.modules:
    sys.modules["ollama"] = types.SimpleNamespace(list=lambda: {"models": []})


from src.managers.chat_state_manager import ChatStateManager
from src.tools.plan import (
    PlanValidationError,
    UpdatePlanTool,
)


class _FakeAgent:
    def __init__(self, workspace: Path) -> None:
        self.workspace_config_dir = workspace
        self._chat_state = {}
        self._chat_state_lock = threading.RLock()
        self.provider = "openai"
        self.model_name = "gpt-4.1"
        self.active_chat_id = "chat-1"
        self.active_chat_name = "Demo"
        self.conversation_history = []
        self.operation_results = []
        self._session_summary_llm = ""
        self._session_summary_rolling = ""
        self._last_llm_summary_pair_count = 0
        self._last_context_usage_percent = 0
        self._last_context_input_tokens = 0
        self._last_context_window = 0
        self.session_memory_service = None
        self._chat_state_manager = None
        self._active_chat_plan = None
        self._active_chat_plan_pending = False

    def _apply_chat_model_from_entry(self, chat, persist_if_missing=False):
        if persist_if_missing:
            chat.setdefault("model_provider", self.provider)
            chat.setdefault("model_name", self.model_name)

    def _append_chat_message(self, role: str, content: str) -> None:
        r = str(role or "").strip().lower()
        if r not in ("user", "assistant"):
            return
        message = {
            "role": r,
            "content": str(content or ""),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if r == "assistant" and self._chat_state_manager is not None:
            self._chat_state_manager.attach_pending_plan_to_message(message)
        self.conversation_history.append(message)
        self._sync_active_chat_messages()

    def _sync_active_chat_messages(self) -> None:
        if self._chat_state_manager is not None:
            self._chat_state_manager.sync_active_chat_messages()

    def _print_chat_history(self):
        return None

    def _refresh_status_context_usage_snapshot(self):
        return None

    def _remember_active_chat_history_first_visible_index(self, _index: int) -> None:
        return None


def _build_agent(workspace: Path) -> _FakeAgent:
    agent = _FakeAgent(workspace)
    manager = ChatStateManager(agent, "chats.json")
    agent._chat_state_manager = manager
    agent._chat_state = manager.default_chat_state()
    manager.activate_chat(
        agent._chat_state["active"],
        announce=False,
        clear_screen=False,
        print_history=False,
        persist=True,
    )
    return agent


class UpdatePlanValidationTests(unittest.TestCase):
    def test_validate_plan_items_returns_normalized_steps(self):
        items = UpdatePlanTool.validate_plan_items(
            [
                {"step": "  Read   target file  ", "status": "Completed"},
                {"step": "Edit helper", "status": "in_progress"},
                {"step": "Run tests", "status": "pending"},
            ]
        )
        self.assertEqual(
            items,
            [
                {"step": "Read target file", "status": "completed"},
                {"step": "Edit helper", "status": "in_progress"},
                {"step": "Run tests", "status": "pending"},
            ],
        )

    def test_validate_plan_rejects_unknown_status(self):
        with self.assertRaises(PlanValidationError):
            UpdatePlanTool.validate_plan_items([{"step": "do x", "status": "doing"}])

    def test_validate_plan_rejects_empty_step(self):
        with self.assertRaises(PlanValidationError):
            UpdatePlanTool.validate_plan_items([{"step": "  ", "status": "pending"}])

    def test_validate_plan_rejects_empty_list(self):
        with self.assertRaises(PlanValidationError):
            UpdatePlanTool.validate_plan_items([])

    def test_validate_plan_rejects_non_list(self):
        with self.assertRaises(PlanValidationError):
            UpdatePlanTool.validate_plan_items({"step": "x", "status": "pending"})

    def test_validate_plan_rejects_multiple_in_progress(self):
        with self.assertRaises(PlanValidationError):
            UpdatePlanTool.validate_plan_items(
                [
                    {"step": "a", "status": "in_progress"},
                    {"step": "b", "status": "in_progress"},
                ]
            )

    def test_parse_args_extracts_explanation(self):
        parsed = UpdatePlanTool.parse_args(
            {
                "explanation": "  step 1 finished  ",
                "plan": [{"step": "next thing", "status": "in_progress"}],
            }
        )
        self.assertEqual(parsed["explanation"], "step 1 finished")
        self.assertEqual(parsed["plan"], [{"step": "next thing", "status": "in_progress"}])


class UpdatePlanIntegrationTests(unittest.TestCase):
    def test_apply_records_plan_on_next_assistant_message(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _build_agent(workspace)
            result = UpdatePlanTool.apply(
                agent,
                {
                    "explanation": "kicking off",
                    "plan": [
                        {"step": "Survey code", "status": "in_progress"},
                        {"step": "Edit helper", "status": "pending"},
                    ],
                },
            )
            self.assertTrue(result["success"], result)
            self.assertEqual(result["in_progress_step"], "Survey code")

            # The plan stays off the chat root and is only attached to the next
            # recorded assistant message.
            chat = agent._chat_state_manager.find_chat_by_id("chat-1")
            self.assertNotIn("plan", chat)
            self.assertEqual(chat["messages"], [])
            self.assertTrue(agent._active_chat_plan_pending)

            agent._append_chat_message("assistant", "working on it")

            chat = agent._chat_state_manager.find_chat_by_id("chat-1")
            self.assertEqual(len(chat["messages"]), 1)
            msg = chat["messages"][0]
            self.assertEqual(
                msg["plan"],
                [
                    {"step": "Survey code", "status": "in_progress"},
                    {"step": "Edit helper", "status": "pending"},
                ],
            )
            self.assertEqual(msg["plan_explanation"], "kicking off")
            self.assertNotEqual(msg["plan_updated_at"], "")
            self.assertFalse(agent._active_chat_plan_pending)

            index_path = workspace / "chats" / "chats.json"
            self.assertTrue(index_path.exists())
            index = json.loads(index_path.read_text(encoding="utf-8"))
            record_file = index["chats"][0]["record_file"]
            saved_chat = json.loads(
                (workspace / "chats" / record_file).read_text(encoding="utf-8")
            )
            self.assertEqual(saved_chat["messages"][0]["plan"], msg["plan"])
            self.assertEqual(saved_chat["messages"][0]["plan_explanation"], "kicking off")
            self.assertNotIn("plan", saved_chat)

    def test_plan_only_recorded_once_until_changed(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _build_agent(Path(td))
            UpdatePlanTool.apply(
                agent,
                {"plan": [{"step": "Only step", "status": "in_progress"}]},
            )
            agent._append_chat_message("assistant", "first")
            agent._append_chat_message("assistant", "second")

            chat = agent._chat_state_manager.find_chat_by_id("chat-1")
            self.assertIn("plan", chat["messages"][0])
            self.assertNotIn("plan", chat["messages"][1])

    def test_apply_returns_failure_when_payload_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _build_agent(Path(td))
            result = UpdatePlanTool.apply(
                agent,
                {"plan": [{"step": "do x", "status": "doing"}]},
            )
            self.assertFalse(result.get("success", True))
            self.assertIn("status", str(result.get("error", "")))

            self.assertIsNone(UpdatePlanTool.current_plan(agent))
            self.assertFalse(agent._active_chat_plan_pending)

    def test_apply_replaces_previous_plan(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _build_agent(Path(td))
            UpdatePlanTool.apply(
                agent,
                {
                    "plan": [
                        {"step": "First step", "status": "in_progress"},
                        {"step": "Second step", "status": "pending"},
                    ]
                },
            )
            agent._append_chat_message("assistant", "round one")
            UpdatePlanTool.apply(
                agent,
                {
                    "plan": [
                        {"step": "First step", "status": "completed"},
                        {"step": "Second step", "status": "in_progress"},
                    ]
                },
            )
            agent._append_chat_message("assistant", "round two")

            chat = agent._chat_state_manager.find_chat_by_id("chat-1")
            self.assertEqual(
                [(it["step"], it["status"]) for it in chat["messages"][0]["plan"]],
                [("First step", "in_progress"), ("Second step", "pending")],
            )
            self.assertEqual(
                [(it["step"], it["status"]) for it in chat["messages"][1]["plan"]],
                [("First step", "completed"), ("Second step", "in_progress")],
            )
            # The latest plan reflects the most recent update.
            snapshot = UpdatePlanTool.current_plan(agent)
            self.assertEqual(
                [(it["step"], it["status"]) for it in snapshot["plan"]],
                [("First step", "completed"), ("Second step", "in_progress")],
            )

    def test_active_chat_plan_returns_copy(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _build_agent(Path(td))
            UpdatePlanTool.apply(
                agent,
                {
                    "plan": [
                        {"step": "Only step", "status": "in_progress"},
                    ]
                },
            )
            snapshot = UpdatePlanTool.current_plan(agent)
            self.assertIsNotNone(snapshot)
            self.assertEqual(
                snapshot["plan"],
                [{"step": "Only step", "status": "in_progress"}],
            )
            snapshot["plan"].append({"step": "mutated", "status": "pending"})
            snapshot2 = UpdatePlanTool.current_plan(agent)
            self.assertEqual(len(snapshot2["plan"]), 1)

    def test_clear_chat_context_resets_plan(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _build_agent(Path(td))
            UpdatePlanTool.apply(
                agent,
                {
                    "plan": [{"step": "Only step", "status": "in_progress"}],
                    "explanation": "keep going",
                },
            )
            agent._append_chat_message("assistant", "noted")
            agent._chat_state_manager.clear_chat_context("chat-1")
            chat = agent._chat_state_manager.find_chat_by_id("chat-1")
            self.assertEqual(chat["messages"], [])
            self.assertIsNone(agent._active_chat_plan)
            self.assertIsNone(UpdatePlanTool.current_plan(agent))

    def test_load_chat_state_round_trip_preserves_latest_plan(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            agent = _build_agent(workspace)
            UpdatePlanTool.apply(
                agent,
                {
                    "plan": [
                        {"step": "Step A", "status": "completed"},
                        {"step": "Step B", "status": "in_progress"},
                    ]
                },
            )
            agent._append_chat_message("assistant", "progress")

            agent2 = _FakeAgent(workspace)
            manager2 = ChatStateManager(agent2, "chats.json")
            agent2._chat_state_manager = manager2
            manager2.load_chat_state()
            chat = manager2.find_chat_by_id("chat-1")
            self.assertEqual(
                chat["messages"][0]["plan"],
                [
                    {"step": "Step A", "status": "completed"},
                    {"step": "Step B", "status": "in_progress"},
                ],
            )
            snapshot = UpdatePlanTool.current_plan(agent2)
            self.assertEqual(
                snapshot["plan"],
                [
                    {"step": "Step A", "status": "completed"},
                    {"step": "Step B", "status": "in_progress"},
                ],
            )

    def test_load_chat_state_drops_invalid_plan_entries(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            chats_dir = workspace / "chats"
            chats_dir.mkdir(parents=True, exist_ok=True)
            record_file = "0123456789abcdef0123456789abcdef.json"
            (chats_dir / record_file).write_text(
                json.dumps(
                    {
                        "id": "chat-1",
                        "name": "Demo",
                        "name_source": "manual",
                        "created_at": "",
                        "updated_at": "",
                        "messages": [
                            {
                                "role": "assistant",
                                "content": "with plan",
                                "created_at": "2025-01-01 00:00:00",
                                "plan": [
                                    {"step": "valid", "status": "pending"},
                                    {"step": "missing-status"},
                                    {"status": "pending"},
                                    "not an object",
                                    {"step": "bad status", "status": "frobnicate"},
                                ],
                                "plan_explanation": "loaded",
                                "plan_updated_at": "2025-01-01 00:00:00",
                            }
                        ],
                        "context_usage_percent": 0,
                        "context_input_tokens": 0,
                        "context_window": 0,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (chats_dir / "chats.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "active": "chat-1",
                        "chats": [
                            {
                                "id": "chat-1",
                                "name": "Demo",
                                "name_source": "manual",
                                "created_at": "",
                                "updated_at": "",
                                "model_provider": "",
                                "model_name": "",
                                "record_file": record_file,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            agent = _FakeAgent(workspace)
            manager = ChatStateManager(agent, "chats.json")
            agent._chat_state_manager = manager
            manager.load_chat_state()
            chat = manager.find_chat_by_id("chat-1")
            self.assertEqual(
                chat["messages"][0]["plan"],
                [{"step": "valid", "status": "pending"}],
            )
            self.assertEqual(chat["messages"][0]["plan_explanation"], "loaded")


if __name__ == "__main__":
    unittest.main()
