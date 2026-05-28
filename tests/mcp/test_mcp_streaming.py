import importlib.util
import unittest
from pathlib import Path
from src.config.app_info import get_app_slug_snake

def _load_mcp_manager_module():
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "agent" / "mcp_manager.py"
    spec = importlib.util.spec_from_file_location(f"{get_app_slug_snake()}_mcp_manager_streaming", str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load mcp_manager module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class McpStreamingUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mcp_module = _load_mcp_manager_module()
        cls.extract_chunk = staticmethod(cls.mcp_module._extract_tool_stream_chunk)

    def test_extract_chunk_from_chunk_field(self):
        text = self.extract_chunk("notifications/tools/call/stream", {"chunk": "hello"})
        self.assertEqual(text, "hello")

    def test_extract_chunk_from_delta_text(self):
        text = self.extract_chunk("notifications/tools/call/progress", {"delta": {"text": "world"}})
        self.assertEqual(text, "world")

    def test_extract_chunk_from_text_content(self):
        text = self.extract_chunk(
            "notifications/tools/call/stream",
            {"content": {"type": "text", "text": "abc"}},
        )
        self.assertEqual(text, "abc")

    def test_extract_chunk_non_stream_method_returns_empty(self):
        text = self.extract_chunk("notifications/something_else", {"chunk": "x"})
        self.assertEqual(text, "")


if __name__ == "__main__":
    unittest.main()
