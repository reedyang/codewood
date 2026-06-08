import sys
import types
import unittest
from unittest.mock import patch

from src.config.app_info import get_app_name
from src.core import assistant_output_highlighter as aoh


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.agent import Agent


class AiOutputDisplayTests(unittest.TestCase):
    def setUp(self):
        self.agent = Agent.__new__(Agent)

    def test_strip_tool_json_fence_keeps_text(self):
        text = (
            "I will read the file first.\n\n"
            "```json\n"
            "{\"tool\":\"shell\",\"args\":{\"command\":\"Get-Content a.py\"}}\n"
            "```\n"
        )
        out = aoh.strip_tool_json_blocks_for_display(text)
        self.assertEqual(out, text.strip())

    def test_tool_call_summary_prefers_path_like_fields(self):
        s = self.agent._tool_call_summary(
            "read",
            {"path": "src/main.py", "line_count": 10, "start_line": 101},
        )
        self.assertIn("read", s)
        self.assertIn("path=src/main.py", s)
        self.assertNotIn("line_count=10", s)
        self.assertNotIn("start_line=101", s)

    def test_strip_tool_json_unclosed_fence_keeps_text(self):
        text = (
            "Step 2 [in_progress]: Continue reading the file.\n\n"
            "```json\n"
            "{\n"
            '  "tool": "shell",\n'
            '  "args": {"command": "Get-Content src/main.py"}\n'
            "}\n"
        )
        out = aoh.strip_tool_json_blocks_for_display(text)
        self.assertEqual(out, text.strip())

    def test_strip_tool_json_fence_with_patch_text_containing_fence_markers_keeps_text(self):
        text = (
            "Step 1 [in_progress]: Apply the patch.\n\n"
            "```json\n"
            "{\n"
            '  "tool": "apply_patch",\n'
            '  "args": {\n'
            '    "path": "prompts.md",\n'
            '    "patch": "--- a/prompts.md\\\\n+++ b/prompts.md\\\\n@@\\\\n- old\\\\n+ ```json\\\\n{\\\\\\"x\\\\\\":1}\\\\n```\\\\n"\n'
            "  }\n"
            "}\n"
            "```\n"
        )
        out = aoh.strip_tool_json_blocks_for_display(text)
        self.assertEqual(out, text.strip())

    def test_strip_tool_json_array_fence_keeps_text(self):
        text = (
            "Let's handle this in two steps first.\n\n"
            "```json\n"
            "[\n"
            "  {\"tool\":\"shell\",\"args\":{\"command\":\"Get-Content a.py\"}},\n"
            "  {\"tool\":\"project_context_search\",\"args\":{\"query\":\"foo\"}}\n"
            "]\n"
            "```\n"
        )
        out = aoh.strip_tool_json_blocks_for_display(text)
        self.assertEqual(out, text.strip())

    def test_strip_assistant_tool_call_marker_block_keeps_text(self):
        text = (
            "Hello! What task can I help you with?\n"
            "<|assistant tool_calls|>{\"tool\":\"done\",\"args\":{}}<|assistant tool_calls|>"
        )
        out = aoh.strip_tool_json_blocks_for_display(text)
        self.assertEqual(out, text.strip())

    def test_strip_pseudo_tool_calls_block_keeps_text(self):
        text = (
            "I will run a ping check.\n\n"
            "<tool_calls>\n"
            "{\"tool\":\"shell\",\"name\":\"shell\",\"arguments\":{\"command\":\"ping www.baidu.com -n 4\"}}\n"
            "</tool_calls>"
        )
        out = aoh.strip_tool_json_blocks_for_display(text)
        self.assertEqual(out, text.strip())

    def test_format_assistant_display_response_highlights_key_tokens(self):
        text = (
            "1. Check https://127.0.0.1:4001 and OPENAI_API_KEY\n"
            "./scripts/start-gateway.ps1 # Windows wrapper"
        )
        with patch("src.core.assistant_output_highlighter._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"), patch(
            "src.core.assistant_output_highlighter._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"
        ), patch("src.core.assistant_output_highlighter._ansi_gray", side_effect=lambda s: f"<G>{s}</G>"):
            out = aoh.format_assistant_display_response(text)

        self.assertIn("<BB>1. </BB>", out)
        self.assertIn("<C>https://127.0.0.1:4001</C>", out)
        self.assertIn("<C>OPENAI_API_KEY</C>", out)
        self.assertIn("<BB>./scripts/start-gateway.ps1</BB>", out)
        self.assertIn("<G> # Windows wrapper</G>", out)

    def test_format_assistant_display_response_highlights_shell_command_lines(self):
        text = (
            "powershell -File .\\scripts\\stop-gateway.ps1\n"
            ".\\.venv\\Scripts\\python -m pip install \"litellm[proxy]==1.83.14\"\n"
            "Get-Content -Path agent.py -Raw"
        )
        with patch("src.core.assistant_output_highlighter._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"), patch(
            "src.core.assistant_output_highlighter._ansi_yellow", side_effect=lambda s: f"<Y>{s}</Y>"
        ), patch("src.core.assistant_output_highlighter._ansi_green", side_effect=lambda s: f"<G>{s}</G>"), patch(
            "src.core.assistant_output_highlighter._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"
        ), patch(
            "src.core.assistant_output_highlighter._ansi_ps_command", side_effect=lambda s: f"<PSC>{s}</PSC>"
        ), patch(
            "src.core.assistant_output_highlighter._ansi_ps_parameter", side_effect=lambda s: f"<PSP>{s}</PSP>"
        ), patch("src.core.assistant_output_highlighter._ansi_gray", side_effect=lambda s: f"<GR>{s}</GR>"):
            out = aoh.format_assistant_display_response(text)

        self.assertIn("<PSC>powershell</PSC>", out)
        self.assertIn("<PSP>-File</PSP>", out)
        self.assertIn("<C>.\\scripts\\stop-gateway.ps1</C>", out)
        self.assertIn("<BB>.\\.venv\\Scripts\\python</BB>", out)
        self.assertIn("<Y>-m</Y>", out)
        self.assertIn("<BB>pip</BB>", out)
        self.assertIn("<BB>install</BB>", out)
        self.assertIn("\"<G>litellm[proxy]==1.83.14</G>\"", out)
        self.assertIn("<PSC>Get-Content</PSC>", out)
        self.assertIn("<PSP>-Path</PSP>", out)
        self.assertIn("<C>agent.py</C>", out)
        self.assertIn("<PSP>-Raw</PSP>", out)

    def test_highlight_parenthesized_powershell_cmdlet_line(self):
        text = '(Get-Content -Path helloworld.py) -replace "print(\\"Hello\\")", "print(\\"Hi\\")"'
        with patch("src.core.assistant_output_highlighter._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"), patch(
            "src.core.assistant_output_highlighter._ansi_yellow", side_effect=lambda s: f"<Y>{s}</Y>"
        ), patch("src.core.assistant_output_highlighter._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"), patch(
            "src.core.assistant_output_highlighter._ansi_green", side_effect=lambda s: f"<G>{s}</G>"
        ), patch(
            "src.core.assistant_output_highlighter._ansi_ps_command", side_effect=lambda s: f"<PSC>{s}</PSC>"
        ), patch(
            "src.core.assistant_output_highlighter._ansi_ps_parameter", side_effect=lambda s: f"<PSP>{s}</PSP>"
        ), patch(
            "src.core.assistant_output_highlighter._ansi_ps_operator", side_effect=lambda s: f"<PSO>{s}</PSO>"
        ):
            out = aoh.highlight_assistant_display_line(text)

        self.assertIn("<PSC>(Get-Content</PSC>", out)
        self.assertIn("<PSP>-Path</PSP>", out)
        self.assertIn("<C>helloworld.py)</C>", out)
        self.assertIn("<PSO>-replace</PSO>", out)

    def test_highlight_quoted_path_with_trailing_parenthesis_keeps_replace_as_operator(self):
        text = "(Get-Content -Path 'helloworld.py') -replace 'a', 'b'"
        with patch("src.core.assistant_output_highlighter._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"), patch(
            "src.core.assistant_output_highlighter._ansi_green", side_effect=lambda s: f"<G>{s}</G>"
        ), patch(
            "src.core.assistant_output_highlighter._ansi_ps_command", side_effect=lambda s: f"<PSC>{s}</PSC>"
        ), patch(
            "src.core.assistant_output_highlighter._ansi_ps_parameter", side_effect=lambda s: f"<PSP>{s}</PSP>"
        ), patch(
            "src.core.assistant_output_highlighter._ansi_ps_operator", side_effect=lambda s: f"<PSO>{s}</PSO>"
        ):
            out = aoh.highlight_assistant_display_line(text)

        self.assertIn("<PSO>-replace</PSO>", out)
        self.assertNotIn("<G>-replace</G>", out)

    def test_highlight_shell_pipeline_colors_pipe_and_following_cmdlet(self):
        text = "Get-Content -Path a.txt | Set-Content -Path b.txt"
        with patch("src.core.assistant_output_highlighter._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"), patch(
            "src.core.assistant_output_highlighter._ansi_yellow", side_effect=lambda s: f"<Y>{s}</Y>"
        ), patch(
            "src.core.assistant_output_highlighter._ansi_ps_command", side_effect=lambda s: f"<PSC>{s}</PSC>"
        ), patch(
            "src.core.assistant_output_highlighter._ansi_ps_parameter", side_effect=lambda s: f"<PSP>{s}</PSP>"
        ), patch(
            "src.core.assistant_output_highlighter._ansi_ps_pipe", side_effect=lambda s: f"<PSPIPE>{s}</PSPIPE>"
        ), patch("src.core.assistant_output_highlighter._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"):
            out = aoh.highlight_assistant_display_line(text)
        self.assertIn("<PSC>Get-Content</PSC>", out)
        self.assertIn("<PSPIPE>|</PSPIPE>", out)
        self.assertIn("<PSC>Set-Content</PSC>", out)

    def test_chinese_narrative_with_inline_flags_is_not_treated_as_shell_command_line(self):
        text = (
            "- Used PowerShell `Get-Content -Path agent.py -Raw` to read the file.\n"
            "- The command ran on Windows via `powershell -ExecutionPolicy Bypass`."
        )
        with patch("src.core.assistant_output_highlighter._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"), patch(
            "src.core.assistant_output_highlighter._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"
        ):
            out = aoh.format_assistant_display_response(text)

        self.assertIn("<BB>- </BB>", out)
        self.assertNotIn("<BB>Used</BB>", out)
        self.assertNotIn("<BB>The command</BB>", out)
        self.assertIn("<C>`Get-Content -Path agent.py -Raw`</C>", out)
        self.assertIn("<C>`powershell -ExecutionPolicy Bypass`</C>", out)

    def test_bang_prefixed_powershell_command_with_inner_command_string_is_highlighted(self):
        text = '!powershell -ExecutionPolicy Bypass -Command "Get-Content -Path agent.py -Raw"'
        with patch("src.core.assistant_output_highlighter._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"), patch(
            "src.core.assistant_output_highlighter._ansi_yellow", side_effect=lambda s: f"<Y>{s}</Y>"
        ), patch("src.core.assistant_output_highlighter._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"), patch(
            "src.core.assistant_output_highlighter._ansi_green", side_effect=lambda s: f"<G>{s}</G>"
        ), patch(
            "src.core.assistant_output_highlighter._ansi_ps_command", side_effect=lambda s: f"<PSC>{s}</PSC>"
        ), patch(
            "src.core.assistant_output_highlighter._ansi_ps_parameter", side_effect=lambda s: f"<PSP>{s}</PSP>"
        ):
            out = aoh.highlight_assistant_display_line(text)

        self.assertIn("!<PSC>powershell</PSC>", out)
        self.assertIn("<PSP>-ExecutionPolicy</PSP>", out)
        self.assertIn("<PSP>-Command</PSP>", out)
        self.assertIn('"<PSC>Get-Content</PSC>', out)
        self.assertIn("<PSP>-Path</PSP>", out)
        self.assertIn("<C>agent.py</C>", out)
        self.assertIn("<PSP>-Raw</PSP>", out)

    def test_highlight_rg_pipeline_command_line_is_treated_as_shell(self):
        text = "rg -i 'token' -n . | select-String -Pattern 'usage|rate'"
        with patch("src.core.assistant_output_highlighter._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"), patch(
            "src.core.assistant_output_highlighter._ansi_yellow", side_effect=lambda s: f"<Y>{s}</Y>"
        ), patch("src.core.assistant_output_highlighter._ansi_green", side_effect=lambda s: f"<G>{s}</G>"), patch(
            "src.core.assistant_output_highlighter._ansi_ps_command", side_effect=lambda s: f"<PSC>{s}</PSC>"
        ), patch(
            "src.core.assistant_output_highlighter._ansi_ps_parameter", side_effect=lambda s: f"<PSP>{s}</PSP>"
        ), patch(
            "src.core.assistant_output_highlighter._ansi_ps_pipe", side_effect=lambda s: f"<PSPIPE>{s}</PSPIPE>"
        ):
            out = aoh.highlight_assistant_display_line(text)

        self.assertIn("<PSC>rg</PSC>", out)
        self.assertIn("<PSP>-i</PSP>", out)
        self.assertIn("<PSP>-n</PSP>", out)
        self.assertIn("<PSPIPE>|</PSPIPE>", out)
        self.assertIn("<PSC>select-String</PSC>", out)
        self.assertIn("<PSP>-Pattern</PSP>", out)

    def test_tool_call_summary_for_powershell_shell_only_shows_command(self):
        cmd = 'powershell -ExecutionPolicy Bypass -Command "Get-ChildItem -Force"'
        s = self.agent._tool_call_summary("shell", {"command": cmd, "force": True, "input": "x"})
        self.assertEqual(s, "Get-ChildItem -Force")

    def test_format_tool_call_feedback_line_uses_ran_and_default_bullet_color(self):
        with patch("src.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "src.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ):
            line = self.agent._format_tool_call_feedback_line("read", {"path": "a.txt"}, failed=False)
        self.assertTrue(line.startswith("<RGB:19,161,14>•</RGB> Ran "))
        self.assertIn("<H>read (path=a.txt)</H>", line)

    def test_format_tool_call_feedback_line_uses_language_specific_prefix(self):
        self.agent.display_language = "zh-CN"
        with patch("src.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "src.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ):
            line = self.agent._format_tool_call_feedback_line("read", {"path": "a.txt"}, failed=False)
        self.assertTrue(line.startswith("<RGB:19,161,14>•</RGB> 已执行 "))
        self.assertIn("<H>read (path=a.txt)</H>", line)

    def test_format_tool_call_feedback_line_switches_bullet_color_when_failed(self):
        with patch("src.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "src.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ):
            line = self.agent._format_tool_call_feedback_line("read", {"path": "a.txt"}, failed=True)
        self.assertTrue(line.startswith("<RGB:197,15,31>•</RGB> Ran "))
        self.assertIn("<H>read (path=a.txt)</H>", line)

    def test_format_direct_shell_command_feedback_line_uses_shared_highlighter(self):
        with patch("src.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "src.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ):
            line = self.agent._format_direct_shell_command_feedback_line("git status", failed=False)
        self.assertTrue(line.startswith("<RGB:19,161,14>•</RGB> You ran "))
        self.assertIn("<H>git status</H>", line)

    def test_format_direct_shell_command_feedback_line_uses_language_specific_prefix(self):
        self.agent.display_language = "zh-CN"
        with patch("src.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "src.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ):
            line = self.agent._format_direct_shell_command_feedback_line("git status", failed=False)
        self.assertTrue(line.startswith("<RGB:19,161,14>•</RGB> 已运行 "))
        self.assertIn("<H>git status</H>", line)

    def test_format_tool_call_feedback_line_wraps_long_command_with_gray_pipe_prefix(self):
        with (
            patch.object(self.agent, "_tool_call_summary", return_value="abcdef ghijkl mnopqrstuvwxyz"),
            patch.object(self.agent, "_terminal_columns_for_command_feedback", return_value=16),
            patch("src.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"),
            patch("src.agent._ansi_gray", side_effect=lambda s: f"<G>{s}</G>"),
            patch("src.agent.highlight_assistant_display_line", side_effect=lambda s: s),
        ):
            line = self.agent._format_tool_call_feedback_line("read", {"path": "a.txt"}, failed=False)
        rows = line.splitlines()
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(rows[1].startswith("<G>  │ </G>"))

    def test_format_direct_shell_command_feedback_line_wraps_long_command_with_gray_pipe_prefix(self):
        with (
            patch.object(self.agent, "_terminal_columns_for_command_feedback", return_value=18),
            patch("src.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"),
            patch("src.agent._ansi_gray", side_effect=lambda s: f"<G>{s}</G>"),
            patch("src.agent.highlight_assistant_display_line", side_effect=lambda s: s),
        ):
            line = self.agent._format_direct_shell_command_feedback_line("git status --short --branch --untracked-files", failed=False)
        rows = line.splitlines()
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(rows[1].startswith("<G>  │ </G>"))

    def test_format_direct_shell_command_feedback_line_rewraps_tail_with_continuation_width(self):
        with (
            patch.object(self.agent, "_terminal_columns_for_command_feedback", return_value=20),
            patch("src.agent._ansi_rgb", side_effect=lambda text, r, g, b: text),
            patch("src.agent._ansi_gray", side_effect=lambda s: s),
            patch("src.agent.highlight_assistant_display_line", side_effect=lambda s: s),
        ):
            line = self.agent._format_direct_shell_command_feedback_line(
                "alpha beta gamma delta",
                failed=False,
            )
        rows = line.splitlines()
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(rows[1].startswith("  │ "))
        self.assertLessEqual(len(rows[1]) - len("  │ "), 16)

    def test_format_direct_shell_command_feedback_line_highlights_once_before_wrapping(self):
        with (
            patch.object(self.agent, "_terminal_columns_for_command_feedback", return_value=16),
            patch("src.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"),
            patch("src.agent._ansi_gray", side_effect=lambda s: f"<G>{s}</G>"),
            patch("src.agent.highlight_assistant_display_line", side_effect=lambda s: s) as mock_hl,
        ):
            line = self.agent._format_direct_shell_command_feedback_line(
                "Get-Content -Path helloworld.py -replace 'print(\\\"Hello\\\")'",
                failed=False,
            )
        self.assertEqual(mock_hl.call_count, 1)
        rows = line.splitlines()
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(rows[1].startswith("<G>  │ </G>"))

    def test_format_direct_shell_command_feedback_line_prefixes_cjk_soft_wraps(self):
        command = (
            "powershell -ExecutionPolicy Bypass -Command "
            '\\"(Get-Content helloworld.py) -replace \\"print(\\"Hello\\")\\",'
            '\\"print(\\"Alice was beginning to get very tired of sitting by her sister on the bank, and of having nothing to do\\")\\""'
        )
        with (
            patch.object(self.agent, "_terminal_columns_for_command_feedback", return_value=42),
            patch("src.agent._ansi_rgb", side_effect=lambda text, r, g, b: text),
            patch("src.agent._ansi_gray", side_effect=lambda s: s),
            patch("src.agent.highlight_assistant_display_line", side_effect=lambda s: s),
        ):
            line = self.agent._format_direct_shell_command_feedback_line(command, failed=False)
        rows = line.splitlines()
        self.assertGreaterEqual(len(rows), 4)
        for row in rows[1:]:
            self.assertTrue(row.startswith("  │ "), row)
            self.assertLessEqual(self.agent._feedback_text_display_width(row), 42, row)

    def test_direct_shell_feedback_inside_slash_reload_uses_captured_columns(self):
        class _FakeStdout:
            encoding = "utf-8"

            def __init__(self):
                self.writes = []

            def write(self, text):
                self.writes.append(str(text))
                return len(str(text))

            def flush(self):
                return None

            def isatty(self):
                return True

        command = (
            "!powershell -ExecutionPolicy Bypass -Command "
            '\\"(Get-Content helloworld.py) -replace \\"print(\\"Hello, world!\\")\\", '
            '\\"print(\\"Alice was beginning to get very tired of sitting by her sister on the bank, and of having nothing to do; she had peeped into the book her sister was reading, but it had no pictures or conversations in it\\")\\" '
            '| Set-Content helloworld.py\\"'
        )
        fake_stdout = _FakeStdout()
        stream = self.agent._build_internal_slash_output_stream(fake_stdout, terminal_columns=42)
        with (
            patch("src.agent.sys.stdout", stream),
            patch("src.agent.shutil.get_terminal_size", return_value=types.SimpleNamespace(columns=80)),
            patch("src.agent._ansi_rgb", side_effect=lambda text, r, g, b: text),
            patch("src.agent._ansi_gray", side_effect=lambda s: s),
            patch("src.agent.highlight_assistant_display_line", side_effect=lambda s: s),
        ):
            print(self.agent._format_direct_shell_command_feedback_line(command, failed=False))

        rows = [line for line in "".join(fake_stdout.writes).splitlines() if line.strip()]
        self.assertGreaterEqual(len(rows), 4)
        self.assertTrue(rows[0].startswith("  • You ran "))
        for row in rows[1:]:
            self.assertTrue(row.startswith("    │ "), row)
            self.assertLessEqual(self.agent._feedback_text_display_width(row), 42, row)

    def test_format_direct_shell_command_feedback_line_preserves_color_after_wrap_prefix_reset(self):
        with (
            patch.object(self.agent, "_terminal_columns_for_command_feedback", return_value=22),
            patch("src.agent._ansi_rgb", side_effect=lambda text, r, g, b: text),
            patch("src.agent._ansi_gray", side_effect=lambda s: f"\x1b[90m{s}\x1b[0m"),
            patch(
                "src.agent.highlight_assistant_display_line",
                side_effect=lambda s: f"\x1b[32m{s}\x1b[0m",
            ),
        ):
            line = self.agent._format_direct_shell_command_feedback_line(
                "abcdefghij klmnopqrst uvwxyz",
                failed=False,
            )
        rows = line.splitlines()
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(rows[1].startswith("\x1b[90m  │ \x1b[0m"))
        self.assertIn("\x1b[32m", rows[1])

    def test_format_user_chat_display_message_wraps_by_window_width_and_indents_continuation(self):
        with (
            patch.object(self.agent, "_terminal_columns_for_line_estimate", return_value=8),
            patch("src.agent._ansi_gray", side_effect=lambda s: s),
        ):
            rendered = self.agent._format_user_chat_display_message("123456 7890")
        rows = rendered.splitlines()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], "› 123456")
        self.assertEqual(rows[1], "  7890")

    def test_format_slash_command_display_wraps_with_two_space_continuation(self):
        with (
            patch.object(self.agent, "_terminal_columns_for_line_estimate", return_value=8),
            patch("src.agent._ansi_gray", side_effect=lambda s: s),
        ):
            rendered = self.agent._format_user_chat_display_message("/abcde fghi")
        rows = rendered.splitlines()
        self.assertEqual(rows, ["› /abcde", "  fghi"])

    def test_format_slash_command_display_does_not_split_single_word(self):
        with (
            patch.object(self.agent, "_terminal_columns_for_line_estimate", return_value=8),
            patch("src.agent._ansi_gray", side_effect=lambda s: s),
        ):
            rendered = self.agent._format_user_chat_display_message("/abcdefghij")
        self.assertEqual(rendered.splitlines(), ["› /abcdefghij"])

    def test_format_assistant_chat_display_message_keeps_ansi_color_after_wrap(self):
        with (
            patch.object(self.agent, "_terminal_columns_for_line_estimate", return_value=10),
            patch("src.agent._ansi_gray", side_effect=lambda s: s),
        ):
            rendered = self.agent._format_assistant_chat_display_message("\x1b[32mabcdef ghijk\x1b[0m")
        rows = rendered.splitlines()
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].startswith("• "))
        self.assertTrue(rows[1].startswith("  "))
        self.assertIn("\x1b[32m", rows[1])

    def test_format_internal_slash_output_indents_logical_and_wrapped_lines(self):
        with patch.object(self.agent, "_terminal_columns_for_line_estimate", return_value=8):
            rendered = self.agent._format_internal_slash_output("alpha beta\nabc")
        self.assertEqual(rendered.splitlines(), ["  alpha", "  beta", "  abc"])

    def test_format_internal_slash_output_does_not_split_single_word(self):
        with patch.object(self.agent, "_terminal_columns_for_line_estimate", return_value=8):
            rendered = self.agent._format_internal_slash_output("abcdefghij")
        self.assertEqual(rendered.splitlines(), ["  abcdefghij"])

    def test_print_internal_slash_history_output_uses_indented_formatter(self):
        class _FakeStdout:
            def __init__(self):
                self.writes = []

            def write(self, text):
                self.writes.append(str(text))
                return len(str(text))

            def flush(self):
                return None

        fake_stdout = _FakeStdout()
        with (
            patch("src.agent.sys.stdout", fake_stdout),
            patch.object(self.agent, "_terminal_columns_for_line_estimate", return_value=8),
        ):
            self.agent._print_internal_slash_history_output("alpha beta\n")
        self.assertEqual("".join(fake_stdout.writes), "  alpha\n  beta\n")

    def test_internal_slash_output_stream_indents_auto_wraps(self):
        class _FakeStdout:
            encoding = "utf-8"

            def __init__(self):
                self.writes = []

            def write(self, text):
                self.writes.append(str(text))
                return len(str(text))

            def flush(self):
                return None

            def isatty(self):
                return True

        class _Sz:
            columns = 8

        fake_stdout = _FakeStdout()
        stream = self.agent._build_internal_slash_output_stream(fake_stdout)
        with patch("src.agent.shutil.get_terminal_size", return_value=_Sz()):
            stream.write("alpha beta\nabc")
        self.assertEqual("".join(fake_stdout.writes), "  alpha\n  beta\n  abc")

    def test_startup_overview_inside_slash_stream_stays_within_terminal_width(self):
        from src.runtime.runtime_loop import _print_startup_overview

        class _FakeStdout:
            encoding = "utf-8"

            def __init__(self):
                self.writes = []

            def write(self, text):
                self.writes.append(str(text))
                return len(str(text))

            def flush(self):
                return None

            def isatty(self):
                return True

        class _Sz:
            columns = 43

        class _Agent:
            model_name = "gpt-oss-120b"
            workspace_name = "Test"
            workspace_root = r"D:\tmp\test"
            _startup_chat_state_warning = ""

        fake_stdout = _FakeStdout()
        stream = self.agent._build_internal_slash_output_stream(fake_stdout, terminal_columns=_Sz.columns)
        identity = lambda s: s
        with (
            patch("src.agent.shutil.get_terminal_size", return_value=types.SimpleNamespace(columns=80)),
            patch("src.runtime.runtime_loop.sys.stdout", stream),
            patch("src.runtime.runtime_loop.get_app_name", return_value=get_app_name()),
            patch("src.runtime.runtime_loop.get_app_display_version", return_value="v0.1.0"),
            patch("src.runtime.runtime_loop.get_random_startup_tip_entry", return_value={"text": "", "highlights": []}),
            patch("src.runtime.runtime_loop._ansi_gray", side_effect=identity),
            patch("src.runtime.runtime_loop._ansi_cyan", side_effect=identity),
            patch("src.runtime.runtime_loop._ansi_bold", side_effect=identity),
        ):
            _print_startup_overview(_Agent())
        out = "".join(fake_stdout.writes)
        box_lines = [
            line
            for line in out.splitlines()
            if line.startswith("  ╭") or line.startswith("  │") or line.startswith("  ╰")
        ]
        self.assertGreaterEqual(len(box_lines), 6)
        self.assertTrue(all(len(line) <= _Sz.columns for line in box_lines))
        self.assertNotIn("  │", box_lines)

    def test_startup_overview_box_rows_align_in_chinese_and_english(self):
        """All four content rows of the startup banner must end at the same
        visual column, in every supported display language. Previously, the
        Chinese model row over-padded its plain text because the renderer
        emitted ``/model`` + a short suffix while the width calculation used
        a longer ``model_change_hint`` translation, shifting the right border
        only on that row."""
        import unicodedata as _ud
        from src.runtime.runtime_loop import _print_startup_overview, _startup_text_display_width

        class _FakeStdout:
            encoding = "utf-8"

            def __init__(self):
                self.writes = []

            def write(self, text):
                self.writes.append(str(text))
                return len(str(text))

            def flush(self):
                return None

            def isatty(self):
                return True

        class _Agent:
            model_name = "Gemma-4-31B"
            workspace_name = "Test"
            workspace_root = r"D:\tmp\test"
            _startup_chat_state_warning = ""

        def _strip_ansi(text):
            # Strip CSI escape sequences so we measure on-screen visible width.
            import re as _re
            return _re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)

        def _visible_width(text):
            width = 0
            for ch in text:
                if _ud.combining(ch):
                    continue
                if _ud.category(ch) in ("Cc", "Cf"):
                    continue
                width += 2 if _ud.east_asian_width(ch) in ("W", "F") else 1
            return width

        for lang in ("zh-CN", "en"):
            with self.subTest(language=lang):
                fake_stdout = _FakeStdout()
                stream = self.agent._build_internal_slash_output_stream(
                    fake_stdout, terminal_columns=80
                )
                with (
                    patch(
                        "src.agent.shutil.get_terminal_size",
                        return_value=types.SimpleNamespace(columns=80),
                    ),
                    patch("src.runtime.runtime_loop.sys.stdout", stream),
                    patch(
                        "src.runtime.runtime_loop.get_app_name",
                        return_value=get_app_name(),
                    ),
                    patch(
                        "src.runtime.runtime_loop.get_app_display_version",
                        return_value="v0.1.0",
                    ),
                    patch(
                        "src.runtime.runtime_loop.get_random_startup_tip_entry",
                        return_value={"text": "", "highlights": []},
                    ),
                    patch(
                        "src.core.localization.get_display_language",
                        return_value=lang,
                    ),
                ):
                    _print_startup_overview(_Agent())

                rendered = "".join(fake_stdout.writes)
                box_rows = [
                    _strip_ansi(line)
                    for line in rendered.splitlines()
                    if line.lstrip().startswith("│") and line.rstrip().endswith("│")
                ]
                self.assertGreaterEqual(
                    len(box_rows), 4, f"{lang}: expected 4+ content rows"
                )
                widths = [_visible_width(line) for line in box_rows]
                self.assertEqual(
                    len(set(widths)),
                    1,
                    f"{lang}: box rows must share one visual width but got "
                    f"widths={widths} for rows={box_rows!r}",
                )
                # Sanity: the visible widths we just computed should also agree
                # with the runtime helper used for padding decisions.
                self.assertEqual(
                    widths[0],
                    _startup_text_display_width(box_rows[0]),
                )

    def test_startup_overview_label_values_share_left_column_in_chinese(self):
        """The values for ``模型``/``工作区``/``目录`` (and their English
        counterparts) must all begin at the same visual column inside the
        startup box, so the three lines look like a tidy two-column layout
        rather than a ragged left edge."""
        import re as _re
        import unicodedata as _ud
        from src.runtime.runtime_loop import _print_startup_overview

        class _FakeStdout:
            encoding = "utf-8"

            def __init__(self):
                self.writes = []

            def write(self, text):
                self.writes.append(str(text))
                return len(str(text))

            def flush(self):
                return None

            def isatty(self):
                return True

        class _Agent:
            model_name = "Gemma-4-31B"
            workspace_name = "Test"
            workspace_root = r"D:\tmp\test"
            _startup_chat_state_warning = ""

        def _strip_ansi(text):
            return _re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)

        def _visible_width(text):
            width = 0
            for ch in text:
                if _ud.combining(ch):
                    continue
                if _ud.category(ch) in ("Cc", "Cf"):
                    continue
                width += 2 if _ud.east_asian_width(ch) in ("W", "F") else 1
            return width

        # For each language, locate the rows whose value we want to align and
        # measure the visible width of everything before the value sentinel.
        cases = [
            (
                "zh-CN",
                [
                    ("模型：", "Gemma-4-31B"),
                    ("工作区：", "Test"),
                    ("目录：", r"D:\tmp\test"),
                ],
            ),
            (
                "en",
                [
                    ("model:", "Gemma-4-31B"),
                    ("workspace:", "Test"),
                    ("directory:", r"D:\tmp\test"),
                ],
            ),
        ]
        for lang, label_value_pairs in cases:
            with self.subTest(language=lang):
                fake_stdout = _FakeStdout()
                stream = self.agent._build_internal_slash_output_stream(
                    fake_stdout, terminal_columns=80
                )
                with (
                    patch(
                        "src.agent.shutil.get_terminal_size",
                        return_value=types.SimpleNamespace(columns=80),
                    ),
                    patch("src.runtime.runtime_loop.sys.stdout", stream),
                    patch(
                        "src.runtime.runtime_loop.get_app_name",
                        return_value=get_app_name(),
                    ),
                    patch(
                        "src.runtime.runtime_loop.get_app_display_version",
                        return_value="v0.1.0",
                    ),
                    patch(
                        "src.runtime.runtime_loop.get_random_startup_tip_entry",
                        return_value={"text": "", "highlights": []},
                    ),
                    patch(
                        "src.core.localization.get_display_language",
                        return_value=lang,
                    ),
                ):
                    _print_startup_overview(_Agent())

                rendered = "".join(fake_stdout.writes)
                stripped_lines = [_strip_ansi(line) for line in rendered.splitlines()]
                value_columns = []
                for label, value in label_value_pairs:
                    match_line = next(
                        (
                            ln
                            for ln in stripped_lines
                            if label in ln and value in ln
                        ),
                        None,
                    )
                    self.assertIsNotNone(
                        match_line,
                        f"{lang}: expected to find label {label!r} with value {value!r}",
                    )
                    value_idx = match_line.index(value)
                    value_columns.append(_visible_width(match_line[:value_idx]))
                self.assertEqual(
                    len(set(value_columns)),
                    1,
                    f"{lang}: values must share one left column but got "
                    f"columns={value_columns} for {label_value_pairs!r}",
                )

    def test_slash_reload_full_width_lines_reserve_output_indent(self):
        class _FakeStdout:
            encoding = "utf-8"

            def __init__(self):
                self.writes = []

            def write(self, text):
                self.writes.append(str(text))
                return len(str(text))

            def flush(self):
                return None

            def isatty(self):
                return True

        class _Sz:
            columns = 43

        fake_stdout = _FakeStdout()
        stream = self.agent._build_internal_slash_output_stream(fake_stdout, terminal_columns=_Sz.columns)
        with (
            patch("src.agent.sys.stdout", stream),
            patch("src.agent.shutil.get_terminal_size", return_value=types.SimpleNamespace(columns=80)),
            patch("src.agent._ansi_gray", side_effect=lambda s: s),
        ):
            self.agent._print_direct_shell_history_separator()
            self.agent._print_task_worked_summary_line(14)

        rendered_lines = [line for line in "".join(fake_stdout.writes).splitlines() if line.strip()]
        self.assertTrue(rendered_lines)
        self.assertTrue(all(len(line) <= _Sz.columns for line in rendered_lines))
        self.assertTrue(any("Worked for 14s" in line for line in rendered_lines))

    def test_slash_reload_text_wrap_uses_captured_columns_over_stale_stdout_width(self):
        class _FakeStdout:
            encoding = "utf-8"

            def __init__(self):
                self.writes = []

            def write(self, text):
                self.writes.append(str(text))
                return len(str(text))

            def flush(self):
                return None

            def isatty(self):
                return True

        fake_stdout = _FakeStdout()
        stream = self.agent._build_internal_slash_output_stream(fake_stdout, terminal_columns=20)
        with (
            patch("src.agent.sys.stdout", stream),
            patch("src.agent.shutil.get_terminal_size", return_value=types.SimpleNamespace(columns=80)),
            patch("src.agent._ansi_gray", side_effect=lambda s: s),
        ):
            print(self.agent._format_user_chat_display_message("alpha beta gamma delta"))

        rows = [line for line in "".join(fake_stdout.writes).splitlines() if line.strip()]
        self.assertEqual(rows, ["  › alpha beta", "    gamma delta"])

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
            patch("src.agent.sys.stdout", fake_stdout),
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

    def test_repaint_tool_call_feedback_if_failed_non_tty_does_not_print_duplicate_line(self):
        class _FakeStdout:
            def isatty(self):
                return False

        with (
            patch("src.agent.sys.stdout", _FakeStdout()),
            patch.object(self.agent, "_print_tool_call_feedback") as print_feedback,
        ):
            self.agent._repaint_tool_call_feedback_if_failed(
                "shell",
                {"command": "echo fail"},
                failed=True,
                up_lines=1,
            )
        print_feedback.assert_not_called()

    def test_tool_call_summary_for_powershell_shell_is_not_truncated(self):
        long_payload = "(Get-Content -Path helloworld.py) -replace 'a','b' " + ("x" * 220)
        cmd = f'powershell -ExecutionPolicy Bypass -Command "{long_payload}"'
        s = self.agent._tool_call_summary("shell", {"command": cmd})
        self.assertEqual(s, long_payload)
        self.assertNotIn("...", s)

    def test_feedback_width_ignores_osc_hyperlink_control_sequences(self):
        osc_link = "\x1b]8;;https://example.com\x07abc\x1b]8;;\x07"
        self.assertEqual(self.agent._feedback_text_display_width(osc_link), 3)
        chunks = self.agent._wrap_ansi_text_by_display_width(osc_link, 3)
        self.assertEqual(len(chunks), 1)

    def test_format_direct_shell_feedback_prefers_real_terminal_width_over_stale_input_handler_width(self):
        class _InputHandlerCols80:
            def __init__(self):
                self.session = object()

            def get_terminal_columns(self, default=80):
                return 80

        class _Sz:
            def __init__(self, columns):
                self.columns = columns

        self.agent.input_handler = _InputHandlerCols80()
        with (
            patch("src.agent.os.get_terminal_size", side_effect=[_Sz(120), _Sz(120)]),
            patch("src.agent._ansi_rgb", side_effect=lambda text, r, g, b: text),
            patch("src.agent._ansi_gray", side_effect=lambda s: s),
            patch("src.agent.highlight_assistant_display_line", side_effect=lambda s: s),
        ):
            line = self.agent._format_direct_shell_command_feedback_line("x" * 90, failed=False)
        self.assertNotIn("\n", line)


if __name__ == "__main__":
    unittest.main()

