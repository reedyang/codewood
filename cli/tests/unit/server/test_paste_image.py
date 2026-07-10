import base64
import tempfile
import unittest
from pathlib import Path

from cli.server.serve_app import ServeApp


# A 1x1 transparent PNG.
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class _FakeChatStateManager:
    """Minimal manager exposing the chat side-data directory under
    ``<cfg>/chats/data/<record-stem>/`` used by the paste-image upload."""

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
        self.workspace_id = "ws-1"


def _app(cfg_dir: Path) -> ServeApp:
    """Bind only the methods under test onto a lightweight stub so we avoid the
    heavyweight ``ServeApp.__init__`` (it spins up a server / agent loop)."""

    class _Stub:
        pass

    stub = _Stub()
    stub.agent = _FakeAgent(cfg_dir)
    stub._persist_ctx_for_workspace = lambda workspace_id: None
    for name in ("_chat_data_dir_for", "save_pasted_image", "read_chat_image"):
        setattr(stub, name, getattr(ServeApp, name).__get__(stub, _Stub))
    # Class attributes consulted by the methods.
    stub._PASTE_IMAGE_EXT = ServeApp._PASTE_IMAGE_EXT
    stub._PASTE_IMAGE_MAX_BYTES = ServeApp._PASTE_IMAGE_MAX_BYTES
    return stub  # type: ignore[return-value]


def _data_url(mime: str, raw: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


class PasteImageTests(unittest.TestCase):
    def test_saves_valid_png_under_chat_data_dir(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            app = _app(cfg)
            res = app.save_pasted_image("c1", _data_url("image/png", _PNG_1x1))
            self.assertTrue(res.get("ok"), res)
            saved = Path(res["path"])
            self.assertTrue(saved.exists())
            # Landed under chats/data/<record>/ and kept the png extension.
            self.assertEqual(
                saved.parent, (cfg / "chats" / "data" / "record-c1").resolve()
            )
            self.assertTrue(saved.name.endswith(".png"))
            self.assertEqual(saved.read_bytes(), _PNG_1x1)

    def test_rejects_missing_chat_id(self):
        with tempfile.TemporaryDirectory() as d:
            app = _app(Path(d))
            res = app.save_pasted_image("", _data_url("image/png", _PNG_1x1))
            self.assertFalse(res.get("ok"))

    def test_saves_under_explicit_workspace_when_chat_ids_repeat(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            app = _app(cfg)
            ws2_cfg = cfg / "workspace-b-config"
            app._persist_ctx_for_workspace = lambda workspace_id: {
                "config_dir": ws2_cfg,
                "chat_state": {
                    "chats": [
                        {"id": "c1", "_record_file": "other-record.json"},
                    ]
                },
            } if workspace_id == "ws-2" else None
            res = app.save_pasted_image("c1", _data_url("image/png", _PNG_1x1), "ws-2")
            self.assertTrue(res.get("ok"), res)
            saved = Path(res["path"])
            self.assertEqual(
                saved.parent,
                (ws2_cfg / "chats" / "data" / "other-record").resolve(),
            )
            self.assertEqual(saved.read_bytes(), _PNG_1x1)

    def test_rejects_non_image_mime(self):
        with tempfile.TemporaryDirectory() as d:
            app = _app(Path(d))
            res = app.save_pasted_image(
                "c1", "data:text/plain;base64," + base64.b64encode(b"hi").decode()
            )
            self.assertFalse(res.get("ok"))

    def test_rejects_unsupported_image_type(self):
        with tempfile.TemporaryDirectory() as d:
            app = _app(Path(d))
            res = app.save_pasted_image(
                "c1", _data_url("image/svg+xml", b"<svg/>")
            )
            self.assertFalse(res.get("ok"))

    def test_rejects_oversize(self):
        with tempfile.TemporaryDirectory() as d:
            app = _app(Path(d))
            big = b"\x00" * (ServeApp._PASTE_IMAGE_MAX_BYTES + 1)
            res = app.save_pasted_image("c1", _data_url("image/png", big))
            self.assertFalse(res.get("ok"))

    def test_rejects_malformed_data_url(self):
        with tempfile.TemporaryDirectory() as d:
            app = _app(Path(d))
            res = app.save_pasted_image("c1", "not-a-data-url")
            self.assertFalse(res.get("ok"))

    def test_read_chat_image_serves_saved_file(self):
        with tempfile.TemporaryDirectory() as d:
            app = _app(Path(d))
            saved = app.save_pasted_image("c1", _data_url("image/png", _PNG_1x1))
            out = app.read_chat_image(saved["path"])
            self.assertIsNotNone(out)
            data, ct = out
            self.assertEqual(data, _PNG_1x1)
            self.assertEqual(ct, "image/png")

    def test_read_chat_image_rejects_path_outside_data_dir(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            app = _app(cfg)
            # A file that exists but lives outside chats/data must be refused.
            outside = cfg / "secret.png"
            outside.write_bytes(_PNG_1x1)
            self.assertIsNone(app.read_chat_image(str(outside)))

    def test_read_chat_image_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            app = _app(cfg)
            saved = app.save_pasted_image("c1", _data_url("image/png", _PNG_1x1))
            traversal = str(Path(saved["path"]).parent / ".." / ".." / ".." / "etc")
            self.assertIsNone(app.read_chat_image(traversal))


if __name__ == "__main__":
    unittest.main()
