import json
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


if __name__ == "__main__":
    unittest.main()
