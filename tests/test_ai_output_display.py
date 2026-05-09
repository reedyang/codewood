import sys
import types
import unittest


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.smart_shell_agent import SmartShellAgent


class AiOutputDisplayTests(unittest.TestCase):
    def setUp(self):
        self.agent = SmartShellAgent.__new__(SmartShellAgent)

    def test_strip_tool_json_fence_keeps_narrative(self):
        text = (
            "我将先读取文件。\n\n"
            "```json\n"
            "{\"tool\":\"read\",\"args\":{\"path\":\"a.py\"}}\n"
            "```\n"
        )
        out = self.agent._strip_tool_json_blocks_for_display(text)
        self.assertEqual(out, "我将先读取文件。")

    def test_tool_call_summary_prefers_path_like_fields(self):
        s = self.agent._tool_call_summary("read", {"path": "src/main.py", "line_count": 10})
        self.assertIn("read", s)
        self.assertIn("path=src/main.py", s)


if __name__ == "__main__":
    unittest.main()
