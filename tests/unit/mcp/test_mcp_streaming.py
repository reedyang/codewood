import importlib
import unittest
from pathlib import Path
from src.config.app_info import get_app_slug_snake

def _load_mcp_manager_module():
    return importlib.import_module("src.integrations.mcp.manager")


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



