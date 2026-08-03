"""Undo/reapply of rename file changes via ServeApp."""

import tempfile
import unittest
from pathlib import Path

from cli.server.serve_app import ServeApp


class _FakeChatStateManager:
    def __init__(self, cfg_dir: Path) -> None:
        self._cfg = Path(cfg_dir)

    def chat_records_dir(self) -> Path:
        return self._cfg / "chats"

    def chat_data_dir_for_chat(self, chat_id: str):
        cid = str(chat_id or "").strip()
        if not cid:
            return None
        return self.chat_records_dir() / "data" / f"record-{cid}"


class _FakeAgent:
    def __init__(self, cfg_dir: Path) -> None:
        self._chat_state_manager = _FakeChatStateManager(cfg_dir)
        self._file_changes_by_chat = {}


class _Stub:
    pass


def _app(cfg_dir: Path) -> _Stub:
    stub = _Stub()
    stub.agent = _FakeAgent(cfg_dir)
    for name in (
        "_lookup_file_change",
        "_chat_data_dir_for",
        "undo_file_changes",
        "reapply_file_changes",
        "_update_undone_files_state",
        "_resolve_backup_full_path",
    ):
        setattr(stub, name, getattr(ServeApp, name).__get__(stub, _Stub))
    # Static helpers referenced via ``self`` inside the bound methods.
    stub._reconstruct_expected_from_diffrows = ServeApp._reconstruct_expected_from_diffrows
    stub._read_file_for_patch = ServeApp._read_file_for_patch
    stub._normalized_undo_lines = ServeApp._normalized_undo_lines
    stub._file_changes_scope_key = ServeApp._file_changes_scope_key
    return stub


def _store_rename_summary(app, chat_id, ref, workspace_id, old_path, new_path, content):
    scope_key = ServeApp._file_changes_scope_key(chat_id, workspace_id)
    summary = {
        "ref": ref,
        "totalFiles": 1,
        "totalAdded": len(content.splitlines()),
        "totalDeleted": 0,
        "files": [
            {
                "filePath": new_path,
                "changeType": "rename",
                "oldPath": old_path,
                "addedLines": len(content.splitlines()),
                "deletedLines": 0,
                "patch": [
                    {"type": "add", "oldNo": None, "newNo": i + 1, "oldText": "", "newText": line}
                    for i, line in enumerate(content.splitlines())
                ],
            }
        ],
    }
    app.agent._file_changes_by_chat[scope_key] = {ref: summary}


class RenameUndoReapplyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.app = _app(Path(self._tmp.name))
        self.work = Path(self._tmp.name) / "work"
        self.work.mkdir()
        self.old_path = str(self.work / "helloworld.py")
        self.new_path = str(self.work / "alice_wonderland.py")
        self.content = "line1\nline2\nline3\n"
        Path(self.new_path).write_text(self.content, encoding="utf-8")
        _store_rename_summary(
            self.app, "chat-1", "ref-1", "ws-1",
            self.old_path, self.new_path, self.content,
        )

    def test_undo_moves_renamed_file_back(self):
        result = self.app.undo_file_changes("chat-1", "ref-1", [self.new_path], "ws-1")

        self.assertTrue(result["results"][self.new_path]["success"])
        self.assertFalse(Path(self.new_path).exists())
        self.assertEqual(Path(self.old_path).read_text(encoding="utf-8"), self.content)

    def test_undo_fails_when_old_path_already_exists(self):
        Path(self.old_path).write_text("occupied\n", encoding="utf-8")

        result = self.app.undo_file_changes("chat-1", "ref-1", [self.new_path], "ws-1")

        self.assertFalse(result["results"][self.new_path]["success"])
        self.assertEqual(
            result["results"][self.new_path]["error"],
            "target file already exists",
        )

    def test_undo_fails_when_renamed_file_was_modified(self):
        Path(self.new_path).write_text("changed content\n", encoding="utf-8")

        result = self.app.undo_file_changes("chat-1", "ref-1", [self.new_path], "ws-1")

        self.assertFalse(result["results"][self.new_path]["success"])
        self.assertEqual(
            result["results"][self.new_path]["error"],
            "file modified since creation",
        )

    def test_undo_fails_when_renamed_file_missing(self):
        Path(self.new_path).unlink()

        result = self.app.undo_file_changes("chat-1", "ref-1", [self.new_path], "ws-1")

        self.assertFalse(result["results"][self.new_path]["success"])
        self.assertEqual(result["results"][self.new_path]["error"], "renamed file missing")

    def test_undo_accepts_bom_difference(self):
        # Recorded content carried a BOM character on the first line; the
        # on-disk file stores the BOM as bytes (stripped by the reader).
        _store_rename_summary(
            self.app, "chat-1", "ref-1", "ws-1",
            self.old_path, self.new_path, "\ufeff" + self.content,
        )

        result = self.app.undo_file_changes("chat-1", "ref-1", [self.new_path], "ws-1")

        self.assertTrue(result["results"][self.new_path]["success"])
        self.assertFalse(Path(self.new_path).exists())
        self.assertEqual(Path(self.old_path).read_text(encoding="utf-8"), self.content)

    def test_reapply_moves_file_back_to_new_name(self):
        result = self.app.undo_file_changes("chat-1", "ref-1", [self.new_path], "ws-1")
        self.assertTrue(result["results"][self.new_path]["success"])

        result = self.app.reapply_file_changes("chat-1", "ref-1", [self.new_path], "ws-1")

        self.assertTrue(result["results"][self.new_path]["success"])
        self.assertFalse(Path(self.old_path).exists())
        self.assertEqual(Path(self.new_path).read_text(encoding="utf-8"), self.content)


if __name__ == "__main__":
    unittest.main()
