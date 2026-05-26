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
            ".\\.venv\\Scripts\\python -m pip install \"litellm[proxy]==1.83.14\"\n"
            "Get-Content -Path smart_shell_agent.py -Raw"
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
        self.assertIn("<BB>Get-Content</BB>", out)
        self.assertIn("<Y>-Path</Y>", out)
        self.assertIn("<C>smart_shell_agent.py</C>", out)
        self.assertIn("<Y>-Raw</Y>", out)

    def test_chinese_narrative_with_inline_flags_is_not_treated_as_shell_command_line(self):
        text = (
            "- 已使用 PowerShell `Get-Content -Path smart_shell_agent.py -Raw` 读取文件。\n"
            "- 命令在 Windows 环境下通过 `powershell -ExecutionPolicy Bypass` 执行。"
        )
        with patch("src.core.assistant_output_highlighter._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"), patch(
            "src.core.assistant_output_highlighter._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"
        ):
            out = aoh.format_assistant_display_response(text)

        self.assertIn("<BB>- </BB>", out)
        self.assertNotIn("<BB>已使用</BB>", out)
        self.assertNotIn("<BB>命令在</BB>", out)
        self.assertIn("<C>`Get-Content -Path smart_shell_agent.py -Raw`</C>", out)
        self.assertIn("<C>`powershell -ExecutionPolicy Bypass`</C>", out)

    def test_bang_prefixed_powershell_command_with_inner_command_string_is_highlighted(self):
        text = '!powershell -ExecutionPolicy Bypass -Command "Get-Content -Path smart_shell_agent.py -Raw"'
        with patch("src.core.assistant_output_highlighter._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"), patch(
            "src.core.assistant_output_highlighter._ansi_yellow", side_effect=lambda s: f"<Y>{s}</Y>"
        ), patch("src.core.assistant_output_highlighter._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"), patch(
            "src.core.assistant_output_highlighter._ansi_green", side_effect=lambda s: f"<G>{s}</G>"
        ):
            out = aoh.highlight_assistant_display_line(text)

        self.assertIn("!<BB>powershell</BB>", out)
        self.assertIn("<Y>-ExecutionPolicy</Y>", out)
        self.assertIn("<Y>-Command</Y>", out)
        self.assertIn('"<BB>Get-Content</BB>', out)
        self.assertIn("<Y>-Path</Y>", out)
        self.assertIn("<C>smart_shell_agent.py</C>", out)
        self.assertIn("<Y>-Raw</Y>", out)

    def test_tool_call_summary_for_powershell_shell_only_shows_command(self):
        cmd = 'powershell -ExecutionPolicy Bypass -Command "Get-ChildItem -Force"'
        s = self.agent._tool_call_summary("shell", {"command": cmd, "force": True, "input": "x"})
        self.assertEqual(s, "Get-ChildItem -Force")

    def test_format_tool_call_feedback_line_uses_ran_and_default_bullet_color(self):
        with patch("src.smart_shell_agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "src.smart_shell_agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ):
            line = self.agent._format_tool_call_feedback_line("read", {"path": "a.txt"}, failed=False)
        self.assertTrue(line.startswith("<RGB:19,161,14>•</RGB> Ran "))
        self.assertIn("<H>read (path=a.txt)</H>", line)

    def test_format_tool_call_feedback_line_switches_bullet_color_when_failed(self):
        with patch("src.smart_shell_agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "src.smart_shell_agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ):
            line = self.agent._format_tool_call_feedback_line("read", {"path": "a.txt"}, failed=True)
        self.assertTrue(line.startswith("<RGB:197,15,31>•</RGB> Ran "))
        self.assertIn("<H>read (path=a.txt)</H>", line)

    def test_format_direct_shell_command_feedback_line_uses_shared_highlighter(self):
        with patch("src.smart_shell_agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "src.smart_shell_agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ):
            line = self.agent._format_direct_shell_command_feedback_line("git status", failed=False)
        self.assertTrue(line.startswith("<RGB:19,161,14>•</RGB> You ran "))
        self.assertIn("<H>git status</H>", line)

    def test_repaint_tool_call_feedback_if_failed_uses_configured_up_lines(self):
        class _FakeStdout:
            def __init__(self):
                self.writes = []

            def isatty(self):
                return True

            def write(self, text):
                self.writes.append(str(text))
                return len(str(text))

            def flush(self):
                return None

        fake_stdout = _FakeStdout()
        with (
            patch("src.smart_shell_agent.sys.stdout", fake_stdout),
            patch.object(self.agent, "_format_tool_call_feedback_line", return_value="FAILED-LINE"),
        ):
            self.agent._repaint_tool_call_feedback_if_failed(
                "shell",
                {"command": "test"},
                failed=True,
                up_lines=3,
            )
        out = "".join(fake_stdout.writes)
        self.assertIn("\x1b[3A\r\x1b[2KFAILED-LINE", out)


if __name__ == "__main__":
    unittest.main()
