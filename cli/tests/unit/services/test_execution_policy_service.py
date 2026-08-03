import json
import os
import tempfile
import unittest
from pathlib import Path

from cli.services.execution_policy_service import (
    freedom_remove_user_script_review_cache_entry,
    freedom_script_review_cache_path,
)


class _DummyAgent:
    def __init__(self, config_dir: Path) -> None:
        self.workspace_config_dir = config_dir
        self._freedom_script_review_entries = {}

    def _ephemeral_path_key(self, path: Path) -> str:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        s = str(resolved)
        if os.name == "nt":
            s = os.path.normcase(s)
        return s


class FreedomScriptReviewCacheRemovalTests(unittest.TestCase):
    def _make_entry(self, key: str) -> dict:
        return {
            "script_sha256": "abc",
            "command_sha256": "def",
            "skip_confirm": True,
            "reason": "safe",
            "updated_at": "2026-01-01T00:00:00",
        }

    def test_remove_entry_drops_in_memory_and_disk(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root / "tools" / "clean.py"
            script.parent.mkdir()
            script.write_text("print(1)\n", encoding="utf-8")
            agent = _DummyAgent(root)
            key = agent._ephemeral_path_key(script)
            other = agent._ephemeral_path_key(root / "keep.py")
            agent._freedom_script_review_entries = {
                key: self._make_entry(key),
                other: self._make_entry(other),
            }

            ok = freedom_remove_user_script_review_cache_entry(agent, script)

            self.assertTrue(ok)
            self.assertNotIn(key, agent._freedom_script_review_entries)
            self.assertIn(other, agent._freedom_script_review_entries)
            payload = json.loads(
                freedom_script_review_cache_path(agent).read_text(encoding="utf-8")
            )
            self.assertNotIn(key, payload["entries"])
            self.assertIn(other, payload["entries"])

    def test_remove_entry_for_already_deleted_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root / "gone.py"
            script.write_text("print(1)\n", encoding="utf-8")
            agent = _DummyAgent(root)
            key = agent._ephemeral_path_key(script)
            agent._freedom_script_review_entries = {key: self._make_entry(key)}
            script.unlink()

            ok = freedom_remove_user_script_review_cache_entry(agent, script)

            self.assertTrue(ok)
            self.assertNotIn(key, agent._freedom_script_review_entries)
            payload = json.loads(
                freedom_script_review_cache_path(agent).read_text(encoding="utf-8")
            )
            self.assertNotIn(key, payload["entries"])

    def test_remove_entry_unknown_path_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agent = _DummyAgent(root)
            key = agent._ephemeral_path_key(root / "tracked.py")
            agent._freedom_script_review_entries = {key: self._make_entry(key)}

            ok = freedom_remove_user_script_review_cache_entry(
                agent, root / "untracked.py"
            )

            self.assertFalse(ok)
            self.assertIn(key, agent._freedom_script_review_entries)
            self.assertFalse(freedom_script_review_cache_path(agent).exists())


if __name__ == "__main__":
    unittest.main()