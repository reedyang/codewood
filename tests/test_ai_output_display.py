import sys
import types
import unittest
from unittest.mock import patch

from src.core import assistant_output_highlighter as aoh


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
        out = aoh.strip_tool_json_blocks_for_display(text)
        self.assertEqual(out, "我将先读取文件。")

    def test_tool_call_summary_prefers_path_like_fields(self):
        s = self.agent._tool_call_summary(
            "read",
            {"path": "src/main.py", "line_count": 10, "start_line": 101},
        )
        self.assertIn("read", s)
        self.assertIn("path=src/main.py", s)
        self.assertNotIn("line_count=10", s)
        self.assertNotIn("start_line=101", s)

    def test_strip_tool_json_unclosed_fence_keeps_narrative(self):
        text = (
            "Step 2 [in_progress]: 继续读取文件。\n\n"
            "```json\n"
            "{\n"
            '  "tool": "read",\n'
            '  "args": {"path": "src/main.py", "start_line": 101}\n'
            "}\n"
        )
        out = aoh.strip_tool_json_blocks_for_display(text)
        self.assertEqual(out, "Step 2 [in_progress]: 继续读取文件。")

    def test_format_assistant_display_response_highlights_key_tokens(self):
        text = (
            "1. Check https://127.0.0.1:4001 and OPENWEBUI_API_KEY\n"
            "./scripts/start-gateway.ps1 # Windows wrapper"
        )
        with patch("src.core.assistant_output_highlighter._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"), patch(
            "src.core.assistant_output_highlighter._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"
        ), patch("src.core.assistant_output_highlighter._ansi_gray", side_effect=lambda s: f"<G>{s}</G>"):
            out = aoh.format_assistant_display_response(text)

        self.assertIn("<BB>1. </BB>", out)
        self.assertIn("<C>https://127.0.0.1:4001</C>", out)
        self.assertIn("<C>OPENWEBUI_API_KEY</C>", out)
        self.assertIn("<BB>./scripts/start-gateway.ps1</BB>", out)
        self.assertIn("<G> # Windows wrapper</G>", out)

    def test_format_assistant_display_response_highlights_shell_command_lines(self):
        text = (
            "powershell -File .\\scripts\\stop-gateway.ps1\n"
            ".\\.venv\\Scripts\\python -m pip install \"litellm[proxy]==1.83.14\""
        )
        with patch("src.core.assistant_output_highlighter._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"), patch(
            "src.core.assistant_output_highlighter._ansi_yellow", side_effect=lambda s: f"<Y>{s}</Y>"
        ), patch("src.core.assistant_output_highlighter._ansi_green", side_effect=lambda s: f"<G>{s}</G>"), patch(
            "src.core.assistant_output_highlighter._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"
        ), patch("src.core.assistant_output_highlighter._ansi_gray", side_effect=lambda s: f"<GR>{s}</GR>"):
            out = aoh.format_assistant_display_response(text)

        self.assertIn("<BB>powershell</BB>", out)
        self.assertIn("<Y>-File</Y>", out)
        self.assertIn("<C>.\\scripts\\stop-gateway.ps1</C>", out)
        self.assertIn("<BB>.\\.venv\\Scripts\\python</BB>", out)
        self.assertIn("<Y>-m</Y>", out)
        self.assertIn("<BB>pip</BB>", out)
        self.assertIn("<BB>install</BB>", out)
        self.assertIn("<G>\"litellm[proxy]==1.83.14\"</G>", out)

    def test_tool_call_summary_for_powershell_shell_only_shows_command(self):
        cmd = 'powershell -ExecutionPolicy Bypass -Command "Get-ChildItem -Force"'
        s = self.agent._tool_call_summary("shell", {"command": cmd, "force": True, "input": "x"})
        self.assertEqual(s, "Get-ChildItem -Force")

    def test_format_tool_call_feedback_line_uses_ran_and_default_bullet_color(self):
        with patch("src.smart_shell_agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "src.smart_shell_agent._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"
        ):
            line = self.agent._format_tool_call_feedback_line("read", {"path": "a.txt"}, failed=False)
        self.assertTrue(line.startswith("<RGB:19,161,14>•</RGB> Ran "))
        self.assertIn("<BB>read (path=a.txt)</BB>", line)

    def test_format_tool_call_feedback_line_switches_bullet_color_when_failed(self):
        with patch("src.smart_shell_agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "src.smart_shell_agent._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"
        ):
            line = self.agent._format_tool_call_feedback_line("read", {"path": "a.txt"}, failed=True)
        self.assertTrue(line.startswith("<RGB:197,15,31>•</RGB> Ran "))
        self.assertIn("<BB>read (path=a.txt)</BB>", line)


if __name__ == "__main__":
    unittest.main()
