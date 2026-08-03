import unittest
from pathlib import Path

from cli.controllers.workspace_command_controller import workspace_switch_command


class _FakeWorkspaceSwitchAgent:
    def __init__(self):
        self.workspace_id = "ws-a"
        self.workspace_name = "Workspace A"
        self.work_directory = Path("D:/ws/a")
        self.refresh_calls = 0
        self.refresh_create_default_chat = []
        self.saved_workspace_ids = []
        # Records (workspace_id, sync_messages) for each persist call so tests
        # can assert the post-apply save is metadata-only.
        self.saved_positions = []
        self._entries = {
            "ws-b": {
                "id": "ws-b",
                "name": "Workspace B",
                "current_dir": "D:/ws/b",
            }
        }

    def _workspace_entry_by_selector(self, selector: str):
        return self._entries.get(str(selector or "").strip())

    def _save_current_workspace_position(self, sync_messages: bool = True):
        self.saved_workspace_ids.append(str(self.workspace_id))
        self.saved_positions.append((str(self.workspace_id), bool(sync_messages)))

    def _apply_workspace_entry(self, entry, _fallback_dir):
        self.workspace_id = str(entry.get("id") or self.workspace_id)
        self.workspace_name = str(entry.get("name") or self.workspace_name)
        self.work_directory = Path(str(entry.get("current_dir") or self.work_directory))

    def _refresh_workspace_runtime(self, create_default_chat: bool = True):
        self.refresh_calls += 1
        self.refresh_create_default_chat.append(bool(create_default_chat))


class WorkspaceCommandControllerTests(unittest.TestCase):
    def test_workspace_switch_persists_before_and_after_switch(self):
        agent = _FakeWorkspaceSwitchAgent()
        msg = workspace_switch_command(agent, "ws-b")
        self.assertIn("✅ Switched to workspace: Workspace B", msg)
        self.assertEqual(agent.refresh_calls, 1)
        self.assertEqual(agent.refresh_create_default_chat, [True])
        self.assertEqual(agent.saved_workspace_ids, ["ws-a", "ws-b"])
        # Pre-apply (source workspace) flushes live messages; post-apply (target
        # workspace) must persist position metadata only, because the session
        # still carries the source chat's id/history and syncing it would
        # duplicate that history into a same-id chat of the target workspace.
        self.assertEqual(
            agent.saved_positions, [("ws-a", True), ("ws-b", False)]
        )

    def test_workspace_switch_same_workspace_does_not_persist_again(self):
        agent = _FakeWorkspaceSwitchAgent()
        agent._entries["ws-a"] = {
            "id": "ws-a",
            "name": "Workspace A",
            "current_dir": "D:/ws/a",
        }
        msg = workspace_switch_command(agent, "ws-a")
        self.assertEqual(msg, "ℹ️ Already in workspace: Workspace A")
        self.assertEqual(agent.saved_workspace_ids, [])
        self.assertEqual(agent.refresh_calls, 0)

    def test_workspace_switch_can_skip_default_chat_creation(self):
        agent = _FakeWorkspaceSwitchAgent()
        workspace_switch_command(agent, "ws-b", create_default_chat=False)
        self.assertEqual(agent.refresh_create_default_chat, [False])


if __name__ == "__main__":
    unittest.main()
