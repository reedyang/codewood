import unittest
from pathlib import Path

from src.controllers.workspace_command_controller import workspace_switch_command


class _FakeWorkspaceSwitchAgent:
    def __init__(self):
        self.workspace_id = "ws-a"
        self.workspace_name = "Workspace A"
        self.work_directory = Path("D:/ws/a")
        self.refresh_calls = 0
        self.saved_workspace_ids = []
        self._entries = {
            "ws-b": {
                "id": "ws-b",
                "name": "Workspace B",
                "current_dir": "D:/ws/b",
            }
        }

    def _workspace_entry_by_selector(self, selector: str):
        return self._entries.get(str(selector or "").strip())

    def _save_current_workspace_position(self):
        self.saved_workspace_ids.append(str(self.workspace_id))

    def _apply_workspace_entry(self, entry, _fallback_dir):
        self.workspace_id = str(entry.get("id") or self.workspace_id)
        self.workspace_name = str(entry.get("name") or self.workspace_name)
        self.work_directory = Path(str(entry.get("current_dir") or self.work_directory))

    def _refresh_workspace_runtime(self):
        self.refresh_calls += 1


class WorkspaceCommandControllerTests(unittest.TestCase):
    def test_workspace_switch_persists_before_and_after_switch(self):
        agent = _FakeWorkspaceSwitchAgent()
        msg = workspace_switch_command(agent, "ws-b")
        self.assertIn("✅ Switched to workspace: Workspace B", msg)
        self.assertEqual(agent.refresh_calls, 1)
        self.assertEqual(agent.saved_workspace_ids, ["ws-a", "ws-b"])

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


if __name__ == "__main__":
    unittest.main()
