import unittest

from cli.core.file_change_tracker import FileChangeTracker, _compute_diff_rows


class FileChangeTrackerTests(unittest.TestCase):
    def test_compute_diff_rows_splits_replace_with_extra_new_lines(self):
        before = "const isPendingSwitch = eventWsId === pendingWs;\nif (chatId && eventKey && eventKey !== focusedKey && !isPendingSwitch) {\nsetState((prev) => {\n"
        after = (
            "const isPendingSwitch = eventWsId === pendingWs;\n"
            "// Detect whether the focused chat was deleted: the old focused\n"
            "// chat id is absent from the incoming chat list. In that case\n"
            "// we must do a full state replacement so the new activeChatId\n"
            "// takes effect and the message area updates accordingly.\n"
            "const focusedDeleted = focusedKey && !isPendingSwitch;\n"
            "if (chatId && eventKey && eventKey !== focusedKey && !isPendingSwitch && !focusedDeleted) {\n"
            "setState((prev) => {\n"
        )

        rows = _compute_diff_rows(before, after)
        changed = [row for row in rows if row["type"] == "change"]
        added = [row for row in rows if row["type"] == "add"]
        deleted = [row for row in rows if row["type"] == "del"]

        self.assertEqual(len(changed), 1)
        self.assertEqual(len(added), 5)
        self.assertEqual(len(deleted), 0)
        self.assertEqual(changed[0]["oldNo"], 2)
        self.assertEqual(changed[0]["newNo"], 2)
        self.assertIn("if (chatId", changed[0]["oldText"])
        self.assertIn("Detect whether the focused chat was deleted", changed[0]["newText"])
        self.assertTrue(all(not row["oldText"] for row in added))
        self.assertEqual([row["newNo"] for row in added], [3, 4, 5, 6, 7])

    def test_get_summary_merges_multiple_modifications_of_same_file_with_final_diff(self):
        tracker = FileChangeTracker()
        file_path = "D:/workspace/demo.ts"

        first_before = "alpha\nbeta\ngamma\n"
        first_after = "alpha\nbeta-1\ngamma\n"
        second_after = "alpha\nbeta-2\ngamma\ndelta\n"

        tracker.record_change(
            file_path=file_path,
            change_type="modify",
            source="apply_patch",
            content_before=first_before,
            content_after=first_after,
            patch=[
                {"type": "context", "oldNo": 1, "newNo": 1, "oldText": "alpha", "newText": "alpha"},
                {"type": "change", "oldNo": 2, "newNo": 2, "oldText": "beta", "newText": "beta-1"},
                {"type": "context", "oldNo": 3, "newNo": 3, "oldText": "gamma", "newText": "gamma"},
            ],
        )
        tracker.record_change(
            file_path=file_path,
            change_type="modify",
            source="apply_patch",
            content_before=first_after,
            content_after=second_after,
            patch=[
                {"type": "context", "oldNo": 1, "newNo": 1, "oldText": "alpha", "newText": "alpha"},
                {"type": "change", "oldNo": 2, "newNo": 2, "oldText": "beta-1", "newText": "beta-2"},
                {"type": "context", "oldNo": 3, "newNo": 3, "oldText": "gamma", "newText": "gamma"},
                {"type": "add", "oldNo": None, "newNo": 4, "oldText": "", "newText": "delta"},
            ],
        )

        summary = tracker.get_summary()

        self.assertEqual(summary["totalFiles"], 1)
        self.assertEqual(summary["totalAdded"], 2)
        self.assertEqual(summary["totalDeleted"], 1)
        self.assertEqual(len(summary["files"]), 1)

        file_summary = summary["files"][0]
        self.assertEqual(file_summary["filePath"], file_path)
        self.assertEqual(file_summary["changeType"], "modify")
        self.assertEqual(file_summary["addedLines"], 2)
        self.assertEqual(file_summary["deletedLines"], 1)

        patch = file_summary["patch"]
        self.assertEqual(
            patch,
            [
                {"type": "context", "oldNo": 1, "newNo": 1, "oldText": "alpha", "newText": "alpha"},
                {"type": "change", "oldNo": 2, "newNo": 2, "oldText": "beta", "newText": "beta-2"},
                {"type": "context", "oldNo": 3, "newNo": 3, "oldText": "gamma", "newText": "gamma"},
                {"type": "add", "oldNo": None, "newNo": 4, "oldText": "", "newText": "delta"},
            ],
        )

    def test_merge_overlapping_modifications_uses_first_before_last_after(self):
        """Two modifications touching overlapping lines — merge computes diff
        from very first before to very last after."""
        tracker = FileChangeTracker()
        path = "/ws/f.py"

        first_before = "a\nb\nc\nd\ne\n"
        first_after = "a\nB\nC\nd\ne\n"
        second_after = "a\nX\nC\nDDD\ne\n"

        tracker.record_change(
            file_path=path, change_type="modify", source="apply_patch",
            content_before=first_before, content_after=first_after,
            patch=[],
        )
        tracker.record_change(
            file_path=path, change_type="modify", source="apply_patch",
            content_before=first_after, content_after=second_after,
            patch=[],
        )

        summary = tracker.get_summary()
        self.assertEqual(summary["totalFiles"], 1)
        f = summary["files"][0]
        self.assertEqual(f["changeType"], "modify")
        # b→X, c→C, d→DDD = 3 changes → 3 added, 3 deleted
        self.assertEqual(f["addedLines"], 3)
        self.assertEqual(f["deletedLines"], 3)

    def test_merge_create_then_modify_yields_create_with_final_content(self):
        """Create followed by modify: merged as create from empty to last after."""
        tracker = FileChangeTracker()
        path = "/ws/new.py"

        create_after = "alpha\nbeta\n"
        modify_after = "alpha\nbeta-2\ngamma\n"

        tracker.record_change(
            file_path=path, change_type="create", source="shell",
            content_before="", content_after=create_after,
            patch=[],
        )
        tracker.record_change(
            file_path=path, change_type="modify", source="apply_patch",
            content_before=create_after, content_after=modify_after,
            patch=[],
        )

        summary = tracker.get_summary()
        self.assertEqual(summary["totalFiles"], 1)
        f = summary["files"][0]
        self.assertEqual(f["changeType"], "create")
        self.assertEqual(f["addedLines"], 3)
        self.assertEqual(f["deletedLines"], 0)
        patch = f["patch"]
        add_rows = [r for r in patch if r["type"] == "add"]
        self.assertEqual(len(add_rows), 3)

    def test_merge_modify_then_delete_yields_delete_with_first_before(self):
        """Modify then delete: merged as delete, deletedLines = original count."""
        tracker = FileChangeTracker()
        path = "/ws/bye.py"

        before = "line1\nline2\nline3\n"
        after = "line1\nline2-modified\nline3\n"

        tracker.record_change(
            file_path=path, change_type="modify", source="shell",
            content_before=before, content_after=after,
            patch=[],
        )
        tracker.record_delete(
            file_path=path, source="shell",
            content_before=after,
        )

        summary = tracker.get_summary()
        self.assertEqual(summary["totalFiles"], 1)
        f = summary["files"][0]
        self.assertEqual(f["changeType"], "delete")
        self.assertEqual(f["addedLines"], 0)
        self.assertEqual(f["deletedLines"], 3)  # line1, line2, line3
        self.assertEqual(f["patch"], [])

    def test_merge_create_then_delete_is_net_zero(self):
        """Create then delete: cancel_create_for_deleted_file removes records."""
        tracker = FileChangeTracker()
        path = "/ws/tmp.py"

        tracker.record_change(
            file_path=path, change_type="create", source="shell",
            content_before="", content_after="temp\ncontent\n",
            patch=[],
        )
        tracker.record_delete(
            file_path=path, source="shell",
            content_before="temp\ncontent\n",
        )
        tracker.cancel_create_for_deleted_file(path)

        summary = tracker.get_summary()
        self.assertEqual(summary["totalFiles"], 0)
        self.assertEqual(summary["totalAdded"], 0)
        self.assertEqual(summary["totalDeleted"], 0)

    def test_merge_modify_same_line_twice_returns_correct_unified_diff(self):
        """dog→cat then cat→bird on same line → unified diff dog→bird."""
        tracker = FileChangeTracker()
        path = "/ws/animals.txt"

        first_before = "the dog\nis happy\n"
        first_after = "the cat\nis happy\n"
        second_after = "the bird\nis happy\n"

        tracker.record_change(
            file_path=path, change_type="modify", source="apply_patch",
            content_before=first_before, content_after=first_after,
            patch=[],
        )
        tracker.record_change(
            file_path=path, change_type="modify", source="apply_patch",
            content_before=first_after, content_after=second_after,
            patch=[],
        )

        summary = tracker.get_summary()
        f = summary["files"][0]
        self.assertEqual(f["addedLines"], 1)
        self.assertEqual(f["deletedLines"], 1)
        patch = f["patch"]
        change_rows = [r for r in patch if r["type"] == "change"]
        self.assertEqual(len(change_rows), 1)
        self.assertEqual(change_rows[0]["oldText"], "the dog")
        self.assertEqual(change_rows[0]["newText"], "the bird")

    def test_merge_with_intermediate_null_before_uses_first_valid_before(self):
        """First record has content_before, second has null — still uses
        first_before and last_after for unified diff."""
        tracker = FileChangeTracker()
        path = "/ws/f.py"

        first_before = "hello\nworld\n"
        first_after = "hello\nworld!\n"

        tracker.record_change(
            file_path=path, change_type="modify", source="shell",
            content_before=first_before, content_after=first_after,
            patch=[],
        )
        tracker.record_change(
            file_path=path, change_type="modify", source="shell",
            content_before=None, content_after="hello\nworld!\nbonus\n",
            patch=[],
        )

        summary = tracker.get_summary()
        f = summary["files"][0]
        self.assertEqual(f["changeType"], "modify")
        self.assertEqual(f["addedLines"], 2)
        self.assertEqual(f["deletedLines"], 1)


if __name__ == "__main__":
    unittest.main()
