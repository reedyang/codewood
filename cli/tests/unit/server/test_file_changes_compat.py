import json
import tempfile
import unittest
from pathlib import Path

from cli.server.serve_app import _cap_patch_rows, _truncate_file_changes
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

    def chat_file_changes_path(self, chat_id: str):
        data_dir = self.chat_data_dir_for_chat(chat_id)
        if data_dir is None:
            return None
        return data_dir / "file_changes.json"

    def load_file_changes(self, chat_id: str):
        path = self.chat_file_changes_path(chat_id)
        if path is None or not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


class _FakeAgent:
    def __init__(self, cfg_dir: Path) -> None:
        self._chat_state_manager = _FakeChatStateManager(cfg_dir)
        self._file_changes_by_chat = {}


def _app(cfg_dir: Path) -> ServeApp:
    class _Stub:
        pass

    stub = _Stub()
    stub.agent = _FakeAgent(cfg_dir)
    stub._lookup_file_change = getattr(ServeApp, "_lookup_file_change").__get__(stub, _Stub)
    stub._chat_data_dir_for = getattr(ServeApp, "_chat_data_dir_for").__get__(stub, _Stub)
    return stub  # type: ignore[return-value]


class FileChangesCompatTests(unittest.TestCase):
    def test_lookup_file_change_reads_new_ref_keyed_disk_format(self):
        with tempfile.TemporaryDirectory() as td:
            app = _app(Path(td))
            path = app.agent._chat_state_manager.chat_file_changes_path("chat-1")
            assert path is not None
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "ref-123": {
                    "ref": "ref-123",
                    "totalFiles": 1,
                    "files": [
                        {
                            "filePath": "D:/workspace/demo.txt",
                            "changeType": "modify",
                            "patch": [{"type": "add", "oldNo": None, "newNo": 1, "oldText": "", "newText": "hello"}],
                        }
                    ],
                }
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

            record = app._lookup_file_change("chat-1", "ref-123", "D:\\workspace\\demo.txt")

            self.assertIsNotNone(record)
            self.assertEqual(record["changeType"], "modify")
            self.assertEqual(record["filePath"], "D:/workspace/demo.txt")


class FileChangesTruncationTests(unittest.TestCase):
    def _full_file_diff(self, total_lines: int, add_at: int, add_count: int) -> list:
        """Build a full-file DiffRow[] like FileChangeTracker.get_summary
        produces: context rows for every line plus add rows at *add_at*."""
        rows = []
        for i in range(1, total_lines + 1):
            if add_at <= i < add_at + add_count:
                rows.append({
                    "type": "add",
                    "oldNo": None,
                    "newNo": i,
                    "oldText": "",
                    "newText": f"new line {i}",
                })
            else:
                old_no = i if i < add_at else i - add_count
                rows.append({
                    "type": "context",
                    "oldNo": old_no,
                    "newNo": i,
                    "oldText": f"line {old_no}",
                    "newText": f"line {i}",
                })
        return rows

    def test_truncate_keeps_change_rows_beyond_head_cap(self):
        # A 4208-row full-file diff whose only edits sit at line 1074+.
        # The old head-slice (first 400 rows) dropped every add row, so the
        # expanded diff showed no modifications at all.
        patch = self._full_file_diff(total_lines=4208, add_at=1074, add_count=8)
        summary = {
            "totalFiles": 1,
            "totalAdded": 8,
            "totalDeleted": 0,
            "files": [
                {
                    "filePath": "D:/workspace/shell.py",
                    "changeType": "modify",
                    "addedLines": 8,
                    "deletedLines": 0,
                    "patch": patch,
                }
            ],
        }

        out = _truncate_file_changes(dict(summary))
        kept = out["files"][0]["patch"]

        self.assertLessEqual(len(kept), 400)
        self.assertTrue(out.get("truncated"))
        adds = [r for r in kept if r.get("type") == "add"]
        self.assertEqual(len(adds), 8)
        self.assertEqual([r.get("newNo") for r in adds], list(range(1074, 1082)))

    def test_cap_patch_rows_short_patch_untouched(self):
        rows = [{"type": "context", "oldNo": 1, "newNo": 1, "oldText": "a", "newText": "a"}]
        self.assertEqual(_cap_patch_rows(rows, 400), rows)

    def test_cap_patch_rows_many_changes_keeps_first_budget(self):
        rows = [
            {"type": "add", "oldNo": None, "newNo": i, "oldText": "", "newText": f"x{i}"}
            for i in range(1, 501)
        ]
        capped = _cap_patch_rows(rows, 400)
        self.assertEqual(len(capped), 400)
        self.assertTrue(all(r.get("type") == "add" for r in capped))


if __name__ == "__main__":
    unittest.main()
