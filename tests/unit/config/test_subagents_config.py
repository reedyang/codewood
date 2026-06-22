import tempfile
import unittest
from pathlib import Path

from cli.core.config.subagents_loader import (
    DEFAULT_SUBAGENT_MAX_ROUNDS,
    delete_subagent,
    is_valid_subagent_name,
    list_subagents_for_config,
    load_subagents_merged,
    set_subagent_enabled,
    write_subagent,
)


class SubAgentsConfigCrudTests(unittest.TestCase):
    """CRUD over the global subagents root powering the GUI config page."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _names(self):
        return [a["name"] for a in list_subagents_for_config(self.config_dir)]

    def test_create_then_list(self):
        result = write_subagent(
            self.config_dir,
            original_name="",
            name="Reviewer",
            description="Reviews diffs.",
            instructions="You are a careful code reviewer.",
            model="openai:gpt-4o",
            tools=["shell", "apply_patch"],
            tools_specified=True,
            max_rounds=15,
            enabled=True,
        )
        self.assertTrue(result["ok"], result)
        items = list_subagents_for_config(self.config_dir)
        self.assertEqual(len(items), 1)
        rec = items[0]
        self.assertEqual(rec["name"], "Reviewer")
        self.assertEqual(rec["model"], "openai:gpt-4o")
        self.assertEqual(rec["tools"], ["shell", "apply_patch"])
        self.assertTrue(rec["toolsSpecified"])
        self.assertEqual(rec["maxRounds"], 15)
        self.assertTrue(rec["enabled"])

    def test_default_max_rounds_omitted_from_file(self):
        write_subagent(
            self.config_dir,
            original_name="",
            name="Plain",
            description="d",
            instructions="i",
            max_rounds=DEFAULT_SUBAGENT_MAX_ROUNDS,
        )
        items = list_subagents_for_config(self.config_dir)
        self.assertEqual(items[0]["maxRounds"], DEFAULT_SUBAGENT_MAX_ROUNDS)

    def test_rename_moves_file(self):
        write_subagent(
            self.config_dir,
            original_name="",
            name="Old",
            description="d",
            instructions="i",
        )
        result = write_subagent(
            self.config_dir,
            original_name="Old",
            name="New",
            description="d",
            instructions="i",
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(self._names(), ["New"])

    def test_create_duplicate_name_rejected(self):
        write_subagent(
            self.config_dir,
            original_name="",
            name="Dup",
            description="d",
            instructions="i",
        )
        result = write_subagent(
            self.config_dir,
            original_name="",
            name="Dup",
            description="d2",
            instructions="i2",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "name_exists")

    def test_validation_errors(self):
        self.assertFalse(
            write_subagent(
                self.config_dir,
                original_name="",
                name="../escape",
                description="d",
                instructions="i",
            )["ok"]
        )
        self.assertFalse(
            write_subagent(
                self.config_dir,
                original_name="",
                name="Ok",
                description="",
                instructions="i",
            )["ok"]
        )
        self.assertFalse(
            write_subagent(
                self.config_dir,
                original_name="",
                name="Ok",
                description="d",
                instructions="",
            )["ok"]
        )

    def test_disable_then_runtime_filters_it_out(self):
        write_subagent(
            self.config_dir,
            original_name="",
            name="Helper",
            description="d",
            instructions="i",
            enabled=True,
        )
        result = set_subagent_enabled(self.config_dir, "Helper", False)
        self.assertTrue(result["ok"], result)
        # Config UI still lists it (disabled), runtime merge keeps the flag.
        items = list_subagents_for_config(self.config_dir)
        self.assertFalse(items[0]["enabled"])
        merged = load_subagents_merged(self.config_dir)
        self.assertEqual(len(merged), 1)
        self.assertFalse(merged[0].enabled)

    def test_delete(self):
        write_subagent(
            self.config_dir,
            original_name="",
            name="Gone",
            description="d",
            instructions="i",
        )
        result = delete_subagent(self.config_dir, "Gone")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self._names(), [])
        self.assertFalse(delete_subagent(self.config_dir, "Gone")["ok"])

    def test_is_valid_subagent_name(self):
        self.assertTrue(is_valid_subagent_name("code-reviewer"))
        self.assertTrue(is_valid_subagent_name("My Agent_1"))
        self.assertFalse(is_valid_subagent_name(""))
        self.assertFalse(is_valid_subagent_name("../x"))
        self.assertFalse(is_valid_subagent_name("bad/name"))


if __name__ == "__main__":
    unittest.main()
