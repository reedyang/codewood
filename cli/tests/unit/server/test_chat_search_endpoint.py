import json
import tempfile
import threading
import unittest
from pathlib import Path

from cli.server.serve_app import ServeApp
from cli.services.chat_search_index import ChatSearchIndex


class _FakeSearchIndex:
    def __init__(self, results=None) -> None:
        self._results = results or []
        self.last_query = ""
        self.last_limit = 0

    def search(self, query: str, limit: int):
        self.last_query = query
        self.last_limit = limit
        return {"keywords": ["k"], "total": len(self._results), "results": self._results}


class _FakeAgent:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.workspace_id = "workspace-1"


class _AgentWithWorkspaces:
    def __init__(self, root: Path) -> None:
        self.workspace_id = "ws_default"
        self._chats_root_override = root / "chats"
        storage = root / "ws-a" / ".codewood"
        self._workspaces_state = {
            "workspaces": {
                "ws_default": {"id": "ws_default", "name": "Default", "kind": "default"},
                "ws_other": {"id": "ws_other", "name": "Other"},
            }
        }
        self._storages = {"ws_default": storage, "ws_other": storage}

    def _workspace_storage_path(self, entry):
        return self._storages[str(entry.get("id") or "")]


class _Stub:
    pass


def _app(cfg_dir: Path, index) -> _Stub:
    stub = _Stub()
    stub.agent = _FakeAgent(cfg_dir)
    stub._chat_search = index
    stub._chat_search_refresher_started = False
    stub._chat_search_wake = threading.Event()
    stub._chat_search_index = getattr(ServeApp, "_chat_search_index").__get__(
        stub, _Stub
    )
    stub.search_chats = getattr(ServeApp, "search_chats").__get__(stub, _Stub)
    stub._invalidate_chat_search = getattr(ServeApp, "_invalidate_chat_search").__get__(
        stub, _Stub
    )
    return stub  # type: ignore[return-value]


class ServeAppChatSearchTests(unittest.TestCase):
    def test_search_chats_delegates_with_ok_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = _FakeSearchIndex([{"chatId": "c1"}])
            app = _app(Path(tmp), fake)
            payload = app.search_chats("搜索 极速", 8)
            self.assertTrue(payload["ok"])
            self.assertEqual(fake.last_query, "搜索 极速")
            self.assertEqual(fake.last_limit, 8)
            self.assertEqual(payload["total"], 1)

    def test_search_chats_clamps_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = _FakeSearchIndex()
            app = _app(Path(tmp), fake)
            app.search_chats("q", 9999)
            self.assertEqual(fake.last_limit, 50)
            app.search_chats("q", 0)
            self.assertEqual(fake.last_limit, 20)

    def test_invalidate_chat_search_uses_active_workspace_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = _FakeSearchIndex()
            app = _app(Path(tmp), fake)
            calls = []
            fake.invalidate_chat = lambda ws, cid: calls.append((ws, cid))
            app._invalidate_chat_search("c1")
            self.assertEqual(calls, [("workspace-1", "c1")])

    def test_refresh_once_indexes_all_enumerated_workspaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = root / "ws-a" / ".codewood"
            chats_dir = root / "chats"
            chats_dir.mkdir(parents=True)
            index_payload = json.dumps(
                {
                    "chats": [
                        {
                            "id": "c1",
                            "name": "Integration Chat",
                            "record_file": "a.json",
                            "archived": False,
                        }
                    ]
                },
                ensure_ascii=False,
            )
            # One index per workspace (``<workspace id>.json``); both
            # workspaces share the same single chat here.
            (chats_dir / "ws_default.json").write_text(index_payload, encoding="utf-8")
            (chats_dir / "ws_other.json").write_text(index_payload, encoding="utf-8")
            (chats_dir / "a.json").write_text(
                json.dumps(
                    {
                        "id": "c1",
                        "messages": [
                            {
                                "role": "user",
                                "content": "integration probe keyword",
                                "created_at": "2026-01-01 00:00:00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            index = ChatSearchIndex(root / "global")
            stub = _Stub()
            stub.agent = _AgentWithWorkspaces(root)
            stub._chat_search = index
            stub._enumerate_search_workspaces = getattr(
                ServeApp, "_enumerate_search_workspaces"
            ).__get__(stub, _Stub)
            stub._chat_search_refresh_once = getattr(
                ServeApp, "_chat_search_refresh_once"
            ).__get__(stub, _Stub)
            stub._chat_search_refresh_once()
            r = index.search("probe")
            self.assertEqual(r["total"], 2)
            self.assertEqual(r["results"][0]["chatName"], "Integration Chat")


if __name__ == "__main__":
    unittest.main()
