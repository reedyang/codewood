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
