import json
import tempfile
import unittest

from src.core.state.history_manager import HistoryManager


class HistoryManagerTests(unittest.TestCase):
    def test_add_entry_persists_immediately(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = HistoryManager(config_dir=tmpdir, max_entries=50)
            manager.add_entry("hello world")

            reloaded = HistoryManager(config_dir=tmpdir, max_entries=50)
            self.assertEqual(reloaded.get_all_history(), ["hello world"])

    def test_add_entry_removes_previous_duplicate_before_append(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = HistoryManager(config_dir=tmpdir, max_entries=50)
            manager.add_entry("build")
            manager.add_entry("test")
            manager.add_entry("build")

            self.assertEqual(manager.get_all_history(), ["test", "build"])

            with open(manager.history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data.get("history"), ["test", "build"])

    def test_slash_entries_persist_to_history_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = HistoryManager(config_dir=tmpdir, max_entries=50)
            manager.add_entry("build")
            manager.add_entry("/chat list")

            self.assertEqual(manager.get_all_history(), ["build", "/chat list"])

            with open(manager.history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data.get("history"), ["build", "/chat list"])

            manager.add_entry("test")
            self.assertEqual(manager.get_all_history(), ["build", "/chat list", "test"])
            with open(manager.history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data.get("history"), ["build", "/chat list", "test"])

            reloaded = HistoryManager(config_dir=tmpdir, max_entries=50)
            self.assertEqual(reloaded.get_all_history(), ["build", "/chat list", "test"])


if __name__ == "__main__":
    unittest.main()
