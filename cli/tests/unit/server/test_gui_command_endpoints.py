"""ServeApp dedicated endpoints replacing GUI slash-command routing.

Covers ``chat_fork`` / ``chat_edit`` / ``set_execution_policy`` /
``workspace_create`` / ``workspace_rename`` — the GUI operations that used to
be implemented by queuing TUI slash commands through ``submit_input``. Each
now runs directly on the HTTP thread, workspace-scoped, without touching the
slash-command machinery.
"""

import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

from cli.agent import Agent
from cli.server.serve_app import ServeApp
from cli.services.session_memory_service import CONTEXT_COMPACTION_SUMMARY_PREFIX


class _FakeBroadcaster:
    def __init__(self) -> None:
        self.published = []

    def publish(self, event, data) -> None:
        self.published.append((event, data))


def _agent(active_chat: str = "chat-1") -> Agent:
    agent = Agent.__new__(Agent)
    agent.workspace_id = "ws-1"
    agent.workspace_name = "Workspace 1"
    agent.execution_policy = "confirmation"
    agent._save_execution_policy_to_config = None
    agent._refresh_chat_record_from_disk = None
    agent._chat_state_lock = threading.RLock()
    agent._chat_state = {
        "active": active_chat,
        "chats": [
            {"id": "chat-1", "name": "Chat 1", "messages": []},
            {"id": "chat-2", "name": "Chat 2", "messages": []},
        ],
    }
    agent._find_chat_by_id = lambda cid: next(
        (c for c in agent._chat_state["chats"] if c.get("id") == cid), None
    )
    agent._chat_entries = lambda: agent._chat_state["chats"]
    agent._workspaces_state = {
        "workspaces": {
            "ws-1": {"id": "ws-1", "name": "Workspace 1", "kind": "custom", "root": "D:/ws1"},
            "ws-2": {"id": "ws-2", "name": "Workspace 2", "kind": "custom", "root": "D:/ws2"},
        },
    }
    agent._save_workspace_state = lambda: None
    agent._refresh_input_handler_skill_completions = lambda: None
    agent._workspace_entry_by_selector = lambda selector: next(
        (
            e
            for e in agent._workspaces_state.get("workspaces", {}).values()
            if isinstance(e, dict)
            and (e.get("id") == selector or e.get("name") == selector)
        ),
        None,
    )
    # Lazily installs the per-chat session registry (real Agent machinery).
    agent._session_for_key("")
    return agent


def _app(agent):
    class _Stub:
        pass

    stub = _Stub()
    stub.agent = agent
    stub.broadcaster = _FakeBroadcaster()
    stub._token = "test"
    stub._route = lambda **payload: payload
    stub._resolve_chat_scope = lambda chat_id="", workspace_id="": (
        str(chat_id or "") or "chat-1",
        str(workspace_id or "") or "ws-1",
    )
    stub._session_scope_for_chat = getattr(ServeApp, "_session_scope_for_chat").__get__(
        stub, _Stub
    )
    stub._switch_to_workspace_safe = getattr(
        ServeApp, "_switch_to_workspace_safe"
    ).__get__(stub, _Stub)
    stub._restore_workspace = getattr(ServeApp, "_restore_workspace").__get__(
        stub, _Stub
    )
    stub.chat_new_from_compact = getattr(
        ServeApp, "chat_new_from_compact"
    ).__get__(stub, _Stub)
    stub.chat_fork = getattr(ServeApp, "chat_fork").__get__(stub, _Stub)
    stub.chat_edit = getattr(ServeApp, "chat_edit").__get__(stub, _Stub)
    stub.set_execution_policy = getattr(
        ServeApp, "set_execution_policy"
    ).__get__(stub, _Stub)
    stub.workspace_create = getattr(ServeApp, "workspace_create").__get__(stub, _Stub)
    stub.workspace_rename = getattr(ServeApp, "workspace_rename").__get__(stub, _Stub)
    stub.delete_workspace = getattr(ServeApp, "delete_workspace").__get__(stub, _Stub)
    return stub


class ServeAppGuiCommandEndpointTests(unittest.TestCase):
    def test_chat_fork_runs_controller_and_returns_new_chat_id(self):
        agent = _agent()
        app = _app(agent)

        def fake_fork(agent_obj, raw_index: str):
            self.assertEqual(raw_index, "-2")
            agent_obj._chat_state["active"] = "chat-9"

        with patch(
            "cli.controllers.chat_command_controller.handle_chat_fork_command",
            side_effect=fake_fork,
        ):
            result = app.chat_fork("chat-1", "ws-1", -2)

        self.assertEqual(result, {"ok": True, "chatId": "chat-9"})
        # The fork switch must be reflected in the broadcast state event.
        self.assertTrue(any(e == "state" for e, _ in app.broadcaster.published))

    def test_chat_fork_returns_not_ok_when_controller_makes_no_new_chat(self):
        agent = _agent()
        app = _app(agent)

        with patch(
            "cli.controllers.chat_command_controller.handle_chat_fork_command",
            return_value=None,
        ):
            result = app.chat_fork("chat-1", "ws-1", -1)

        self.assertEqual(result["ok"], False)

    def test_chat_edit_interrupts_target_chat_then_runs_controller(self):
        agent = _agent()
        app = _app(agent)
        app.interrupt = Mock()

        with patch(
            "cli.controllers.chat_command_controller.handle_chat_edit_command"
        ) as edit:
            ok = app.chat_edit("chat-2", "ws-1", -1)

        self.assertTrue(ok)
        app.interrupt.assert_called_once_with(chat_id="chat-2", workspace_id="ws-1")
        self.assertEqual(edit.call_args.args[1], "-1")
        # An edit leaves the chat id unchanged; the frontend reloads history on
        # the next idle event (not via the active-chat change effect).
        self.assertTrue(any(e == "idle" for e, _ in app.broadcaster.published))

    def test_chat_new_from_compact_creates_seeded_chat_with_unique_name(self):
        agent = _agent()
        agent._chat_state["chats"][0].update(
            {
                "model_provider": "openai",
                "model_name": "gpt",
                "reasoning_level": "high",
            }
        )
        # A prior compaction summary in the source chat whose mode the new
        # chat's seeded summary should inherit for its banner title.
        agent._chat_state["chats"][0]["messages"] = [
            {
                "role": "assistant",
                "content": CONTEXT_COMPACTION_SUMMARY_PREFIX
                + json.dumps(
                    {
                        "kind": "context_compaction_summary",
                        "summary": "old summary",
                        "mode": "auto",
                        "created_at": "2026-08-01 10:00:00",
                    },
                    ensure_ascii=False,
                ),
            }
        ]

        class _FakeSms:
            def parse_context_compaction_summary_content(self, content):
                text = str(content or "")
                if not text.startswith(CONTEXT_COMPACTION_SUMMARY_PREFIX):
                    return None
                try:
                    payload = json.loads(
                        text[len(CONTEXT_COMPACTION_SUMMARY_PREFIX):]
                    )
                except Exception:
                    return None
                return payload if isinstance(payload, dict) else None

        agent.session_memory_service = _FakeSms()
        agent._next_chat_id = lambda: "chat-new"

        def _new_chat_entry(cid, name=None):
            return {
                "id": cid,
                "name": name or "",
                "messages": [],
                "name_source": "auto",
                "model_provider": "",
                "model_name": "",
                "reasoning_level": "",
            }

        agent._new_chat_entry = _new_chat_entry
        agent._save_chat_state = lambda: None
        app = _app(agent)

        result = app.chat_new_from_compact(
            "chat-1", "ws-1", "summary body"
        )

        self.assertEqual(result, {"ok": True, "chatId": "chat-new"})
        new_chat = agent._find_chat_by_id("chat-new")
        self.assertEqual(new_chat["name"], "Chat 1 (2)")
        self.assertEqual(new_chat["name_source"], "manual")
        self.assertEqual(len(new_chat["messages"]), 1)
        message = new_chat["messages"][0]
        # The summary is carried over as an ASSISTANT compaction-summary
        # message (same wire format the runtime persists), not a user prompt.
        self.assertEqual(message["role"], "assistant")
        self.assertTrue(
            message["content"].startswith(CONTEXT_COMPACTION_SUMMARY_PREFIX)
        )
        payload = json.loads(
            message["content"][len(CONTEXT_COMPACTION_SUMMARY_PREFIX):]
        )
        self.assertEqual(payload["kind"], "context_compaction_summary")
        self.assertEqual(payload["summary"], "summary body")
        self.assertEqual(payload["mode"], "auto")
        self.assertEqual(new_chat["model_provider"], "openai")
        self.assertEqual(new_chat["model_name"], "gpt")
        self.assertEqual(new_chat["reasoning_level"], "high")
        # The new chat becomes the active chat and a state event is broadcast.
        self.assertEqual(agent._chat_state["active"], "chat-new")
        self.assertTrue(any(e == "state" for e, _ in app.broadcaster.published))

    def test_chat_new_from_compact_increments_existing_numeric_suffix(self):
        agent = _agent()
        agent._chat_state["chats"].append(
            {"id": "chat-3", "name": "Chat 1 (2)", "messages": []}
        )
        agent._next_chat_id = lambda: "chat-new"
        agent._new_chat_entry = lambda cid, name=None: {
            "id": cid,
            "name": name or "",
            "messages": [],
            "name_source": "auto",
        }
        agent._save_chat_state = lambda: None
        app = _app(agent)

        result = app.chat_new_from_compact("chat-1", "ws-1", "summary")

        self.assertEqual(result["ok"], True)
        self.assertEqual(agent._find_chat_by_id("chat-new")["name"], "Chat 1 (3)")
        self.assertEqual(
            agent._find_chat_by_id("chat-new")["messages"][0]["role"],
            "assistant",
        )

    def test_set_execution_policy_applies_and_persists(self):
        agent = _agent()
        saved = {}
        agent._save_execution_policy_to_config = lambda: saved.update(
            policy=agent.execution_policy
        )
        app = _app(agent)

        self.assertTrue(app.set_execution_policy("unlimited"))
        self.assertEqual(agent.execution_policy, "unlimited")
        self.assertEqual(saved.get("policy"), "unlimited")
        self.assertTrue(any(e == "state" for e, _ in app.broadcaster.published))

    def test_set_execution_policy_rejects_unknown_value(self):
        app = _app(_agent())
        self.assertFalse(app.set_execution_policy("bogus"))
        self.assertEqual(app.broadcaster.published, [])

    def test_workspace_create_invokes_controller_and_returns_new_id(self):
        agent = _agent()
        app = _app(agent)

        def fake_create(agent_obj, arg_text: str):
            agent_obj._workspaces_state.setdefault("workspaces", {})["ws-9"] = {
                "id": "ws-9",
                "name": "New WS",
                "kind": "custom",
                "root": "D:/new",
            }
            return "workspace.create.success"

        with patch(
            "cli.controllers.workspace_command_controller.workspace_create_command",
            side_effect=fake_create,
        ) as create:
            result = app.workspace_create("D:/new")

        self.assertEqual(result["ok"], True)
        self.assertEqual(result["id"], "ws-9")
        create.assert_called_once()
        self.assertIn("D:/new", create.call_args.args[1])
        # Mirrors open_folder: an idle event (not just state) lets the
        # frontend enter draft mode and refresh the workspace list once the
        # pending focus is set.
        self.assertTrue(any(e == "idle" for e, _ in app.broadcaster.published))

    def test_workspace_create_fails_when_no_workspace_added(self):
        agent = _agent()
        app = _app(agent)

        with patch(
            "cli.controllers.workspace_command_controller.workspace_create_command",
            return_value="workspace.name_exists_error",
        ):
            result = app.workspace_create("D:/dup")

        self.assertEqual(result["ok"], False)

    def test_workspace_rename_invokes_controller_and_confirms_name(self):
        agent = _agent()
        app = _app(agent)

        def fake_update(agent_obj, arg_text: str):
            entry = agent_obj._workspace_entry_by_selector("ws-1")
            entry["name"] = "Renamed"
            return "workspace.update.success"

        with patch(
            "cli.controllers.workspace_command_controller.workspace_update_command",
            side_effect=fake_update,
        ) as update:
            result = app.workspace_rename("ws-1", "Renamed")

        self.assertEqual(result["ok"], True)
        update.assert_called_once()
        self.assertIn('"ws-1"', update.call_args.args[1])
        self.assertIn("Renamed", update.call_args.args[1])
        self.assertTrue(any(e == "state" for e, _ in app.broadcaster.published))

    def test_workspace_rename_fails_when_name_not_applied(self):
        agent = _agent()
        app = _app(agent)

        with patch(
            "cli.controllers.workspace_command_controller.workspace_update_command",
            return_value="workspace.name_exists_error",
        ):
            result = app.workspace_rename("ws-1", "Taken")

        self.assertEqual(result["ok"], False)

    def test_delete_workspace_removes_entry_synchronously_and_cleans_acl_in_background(self):
        agent = _agent()
        app = _app(agent)

        with patch(
            "cli.core.sandbox.cleanup_workspace_acls"
        ) as cleanup:
            result = app.delete_workspace("ws-2")

        # The registry entry is gone BEFORE the request returns.
        self.assertEqual(result, {"id": "ws-2", "wasActive": False, "fallbackId": ""})
        self.assertNotIn(
            "ws-2", agent._workspaces_state.get("workspaces", {})
        )
        # The sandbox ACL cleanup runs on a background thread (does not block
        # the HTTP response); wait for it to fire.
        deadline = time.time() + 3
        while cleanup.call_count == 0 and time.time() < deadline:
            time.sleep(0.005)
        cleanup.assert_called_once_with(agent, "D:/ws2")
        self.assertTrue(any(e == "idle" for e, _ in app.broadcaster.published))

    def test_delete_workspace_returns_none_for_missing_or_default(self):
        app = _app(_agent())
        self.assertIsNone(app.delete_workspace("nope"))
        self.assertIsNone(app.delete_workspace("default"))


if __name__ == "__main__":
    unittest.main()
