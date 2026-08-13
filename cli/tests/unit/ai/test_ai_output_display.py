import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from cli.config.app_info import get_app_name
from cli.core import text_output_renderer as aoh


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from cli.agent import Agent


class AiOutputDisplayTests(unittest.TestCase):
    def setUp(self):
        self.agent = Agent.__new__(Agent)

    def test_strip_tool_json_fence_removes_trailing_call(self):
        # Models that emit a tool call as a ```json fenced block at the end
        # of the assistant reply have those JSON envelopes recognised and
        # executed by the runtime; the display path must hide the raw JSON
        # so neither the TUI replay nor the GUI ``chat_history`` payload
        # shows it as natural-language text.
        text = (
            "I will read the file first.\n\n"
            "```json\n"
            "{\"tool\":\"shell\",\"args\":{\"command\":\"Get-Content a.py\"}}\n"
            "```\n"
        )
        out = aoh.strip_tool_json_blocks_for_display(text)
        self.assertEqual(out, "I will read the file first.")

    def test_tool_call_summary_prefers_path_like_fields(self):
        s = self.agent._tool_call_summary(
            "read",
            {"path": "cli/main.py", "line_count": 10, "start_line": 101},
        )
        self.assertIn("read", s)
        self.assertIn("path=cli/main.py", s)
        self.assertNotIn("line_count=10", s)
        self.assertNotIn("start_line=101", s)

    def test_strip_tool_json_unclosed_fence_keeps_text(self):
        # Without the closing ```` ``` ```` the fence parser can't isolate the
        # JSON body and ``json.loads`` would reject the candidate, so the
        # stripper leaves the whole reply intact rather than guess.
        # (The trailing object-opener path also can't parse multi-line JSON
        # whose closing brace isn't followed by a recognisable end, so the
        # stripper bails out and keeps the user-visible text untouched.)
        text = (
            "Step 2 [in_progress]: Continue reading the file.\n\n"
            "```json\n"
            "{\n"
            '  "tool": "shell",\n'
            '  "args": {"command": "Get-Content cli/main.py"}\n'
            "}\n"
        )
        out = aoh.strip_tool_json_blocks_for_display(text)
        # The trailing-object scan now does parse the inner JSON object
        # successfully because everything after the opening ``{`` is valid
        # JSON. That's still a recognised tool-call shape so it gets
        # stripped along with the orphan ``\`\`\`json`` fence line above
        # it. The natural-language prefix line must survive.
        self.assertIn("Step 2 [in_progress]: Continue reading the file.", out)
        self.assertNotIn('"tool": "shell"', out)

    def test_strip_tool_json_fence_with_patch_text_containing_fence_markers_strips_outer_block(self):
        # Even when the embedded patch payload itself contains fence
        # markers and escaped JSON, the OUTER ```json block is the real
        # tool call and must still be stripped — otherwise the user sees
        # the apply_patch envelope leak into the reply.
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
        self.assertIn("Step 1 [in_progress]: Apply the patch.", out)
        self.assertNotIn('"tool": "apply_patch"', out)

    def test_strip_tool_json_array_fence_removes_call_array(self):
        # A top-level JSON array of tool-call objects is the multi-call
        # variant the runtime recognises; the display strip treats it the
        # same as a single-call object and removes the whole array.
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
        self.assertEqual(out, "Let's handle this in two steps first.")

    def test_strip_assistant_tool_call_marker_block_keeps_text(self):
        text = (
            "Hello! What task can I help you with?\n"
            "<|assistant tool_calls|>{\"tool\":\"done\",\"args\":{}}<|assistant tool_calls|>"
        )
        out = aoh.strip_tool_json_blocks_for_display(text)
        self.assertEqual(out, text.strip())

    def test_format_assistant_display_response_plain_strips_channel_markers(self):
        text = (
            "前缀\n"
            "<|channel>\n"
            "这段自然语言应该保留\n"
            "<channel|>\n"
            "后缀"
        )
        out = aoh.format_assistant_display_response_plain(text)
        self.assertIn("这段自然语言应该保留", out)
        self.assertNotIn("<|channel>", out)
        self.assertNotIn("<channel|>", out)
        self.assertIn("前缀", out)
        self.assertIn("后缀", out)

    def test_strip_multiple_chained_tool_call_blocks(self):
        # Reproduces the live bug: the model emitted an ``update_plan``
        # tool call followed by an ``request_user_input`` envelope inside a
        # single assistant turn (the runtime only stashed the final
        # block's text into ``pseudo_tool_call_text``, leaving the
        # earlier ``update_plan`` JSON visible in ``content``). The
        # display stripper must peel BOTH blocks off so neither one
        # leaks into the GUI panel or the TUI history replay.
        text = (
            "Hi! I searched the gmail-related skills, please pick one:\n\n"
            "{\n"
            '  "tool": "update_plan",\n'
            '  "args": {\n'
            '    "explanation": "Search completed, moving to selection",\n'
            '    "plan": [\n'
            '      { "step": "Search for Gmail skill", "status": "completed" },\n'
            '      { "step": "Select detail URL", "status": "in_progress" },\n'
            '      { "step": "Install selected Gmail skill", "status": "pending" }\n'
            "    ]\n"
            "  }\n"
            "},\n"
            "{\n"
            '  "tool": "request_user_input",\n'
            '  "args": {\n'
            '    "question": "pick one",\n'
            '    "options": ["a", "b"]\n'
            "  }\n"
            "}"
        )
        out = aoh.strip_tool_json_blocks_for_display(text)
        self.assertIn("Hi! I searched the gmail-related skills, please pick one:", out)
        self.assertNotIn('"tool": "update_plan"', out)
        self.assertNotIn('"tool": "request_user_input"', out)

    def test_strip_pseudo_tool_calls_block_keeps_text(self):
        text = (
            "I will run a ping check.\n\n"
            "<tool_calls>\n"
            "{\"tool\":\"shell\",\"name\":\"shell\",\"arguments\":{\"command\":\"ping www.baidu.com -n 4\"}}\n"
            "</tool_calls>"
        )
        out = aoh.strip_tool_json_blocks_for_display(text)
        self.assertEqual(out, text.strip())

    def test_format_assistant_display_response_plain_keeps_proposed_plan(self):
        text = (
            "Here is my plan.\n\n"
            "<proposed_plan>\n# Plan\n- step one\n- step two\n</proposed_plan>"
        )
        out = aoh.format_assistant_display_response_plain(text)
        self.assertIn("<proposed_plan>", out)
        self.assertIn("</proposed_plan>", out)
        self.assertIn("- step one", out)
        self.assertNotIn("Proposed Plan", out)
        self.assertNotIn("\x1b[", out)

    def test_format_assistant_display_response_plain_preserves_multiline_latex_for_gui(self):
        text = (
            "### 架构流程图 (文字描述)\n"
            "`Main Agent` $\\xrightarrow{call\\ run\\_subagent}$ `Executor` "
            "$\\begin{cases}\n"
            "\\text{LLM Call} \\\\\n"
            "\\text{SSE Event}\n"
            "\\end{cases}$ "
            "$\\xrightarrow{return}$ `Main Agent`"
        )
        out = aoh.format_assistant_display_response_plain(text)
        self.assertIn("$\\begin{cases}\n", out)
        self.assertIn("\\text{LLM Call} \\\\\n", out)
        self.assertIn("\\end{cases}$", out)
        self.assertNotIn("LLM Call SSE Event", out)

    def test_format_assistant_display_response_reframes_proposed_plan(self):
        text = "<proposed_plan>\n# Plan\n- step\n</proposed_plan>"
        out = aoh.format_assistant_display_response(text)
        self.assertNotIn("<proposed_plan>", out)
        self.assertIn("Proposed Plan", out)

    def test_format_assistant_display_response_highlights_key_tokens(self):
        text = (
            "1. Check https://127.0.0.1:4001 and OPENAI_API_KEY\n"
            "./scripts/start-gateway.ps1 # Windows wrapper"
        )
        with patch("cli.core.text_output_renderer._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"), patch(
            "cli.core.text_output_renderer._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"
        ), patch("cli.core.text_output_renderer._ansi_gray", side_effect=lambda s: f"<G>{s}</G>"):
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
        with patch("cli.core.text_output_renderer._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"), patch(
            "cli.core.text_output_renderer._ansi_yellow", side_effect=lambda s: f"<Y>{s}</Y>"
        ), patch("cli.core.text_output_renderer._ansi_green", side_effect=lambda s: f"<G>{s}</G>"), patch(
            "cli.core.text_output_renderer._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"
        ), patch(
            "cli.core.text_output_renderer._ansi_ps_command", side_effect=lambda s: f"<PSC>{s}</PSC>"
        ), patch(
            "cli.core.text_output_renderer._ansi_ps_parameter", side_effect=lambda s: f"<PSP>{s}</PSP>"
        ), patch("cli.core.text_output_renderer._ansi_gray", side_effect=lambda s: f"<GR>{s}</GR>"):
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
        with patch("cli.core.text_output_renderer._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"), patch(
            "cli.core.text_output_renderer._ansi_yellow", side_effect=lambda s: f"<Y>{s}</Y>"
        ), patch("cli.core.text_output_renderer._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"), patch(
            "cli.core.text_output_renderer._ansi_green", side_effect=lambda s: f"<G>{s}</G>"
        ), patch(
            "cli.core.text_output_renderer._ansi_ps_command", side_effect=lambda s: f"<PSC>{s}</PSC>"
        ), patch(
            "cli.core.text_output_renderer._ansi_ps_parameter", side_effect=lambda s: f"<PSP>{s}</PSP>"
        ), patch(
            "cli.core.text_output_renderer._ansi_ps_operator", side_effect=lambda s: f"<PSO>{s}</PSO>"
        ):
            out = aoh.highlight_assistant_display_line(text)

        self.assertIn("<PSC>(Get-Content</PSC>", out)
        self.assertIn("<PSP>-Path</PSP>", out)
        self.assertIn("<C>helloworld.py)</C>", out)
        self.assertIn("<PSO>-replace</PSO>", out)

    def test_highlight_quoted_path_with_trailing_parenthesis_keeps_replace_as_operator(self):
        text = "(Get-Content -Path 'helloworld.py') -replace 'a', 'b'"
        with patch("cli.core.text_output_renderer._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"), patch(
            "cli.core.text_output_renderer._ansi_green", side_effect=lambda s: f"<G>{s}</G>"
        ), patch(
            "cli.core.text_output_renderer._ansi_ps_command", side_effect=lambda s: f"<PSC>{s}</PSC>"
        ), patch(
            "cli.core.text_output_renderer._ansi_ps_parameter", side_effect=lambda s: f"<PSP>{s}</PSP>"
        ), patch(
            "cli.core.text_output_renderer._ansi_ps_operator", side_effect=lambda s: f"<PSO>{s}</PSO>"
        ):
            out = aoh.highlight_assistant_display_line(text)

        self.assertIn("<PSO>-replace</PSO>", out)
        self.assertNotIn("<G>-replace</G>", out)

    def test_highlight_shell_pipeline_colors_pipe_and_following_cmdlet(self):
        text = "Get-Content -Path a.txt | Set-Content -Path b.txt"
        with patch("cli.core.text_output_renderer._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"), patch(
            "cli.core.text_output_renderer._ansi_yellow", side_effect=lambda s: f"<Y>{s}</Y>"
        ), patch(
            "cli.core.text_output_renderer._ansi_ps_command", side_effect=lambda s: f"<PSC>{s}</PSC>"
        ), patch(
            "cli.core.text_output_renderer._ansi_ps_parameter", side_effect=lambda s: f"<PSP>{s}</PSP>"
        ), patch(
            "cli.core.text_output_renderer._ansi_ps_pipe", side_effect=lambda s: f"<PSPIPE>{s}</PSPIPE>"
        ), patch("cli.core.text_output_renderer._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"):
            out = aoh.highlight_assistant_display_line(text)
        self.assertIn("<PSC>Get-Content</PSC>", out)
        self.assertIn("<PSPIPE>|</PSPIPE>", out)
        self.assertIn("<PSC>Set-Content</PSC>", out)

    def test_chinese_narrative_with_inline_flags_is_not_treated_as_shell_command_line(self):
        text = (
            "- Used PowerShell `Get-Content -Path agent.py -Raw` to read the file.\n"
            "- The command ran on Windows via `powershell -ExecutionPolicy Bypass`."
        )
        with patch("cli.core.text_output_renderer._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"), patch(
            "cli.core.text_output_renderer._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"
        ):
            out = aoh.format_assistant_display_response(text)

        self.assertIn("<BB>• </BB>", out)
        self.assertNotIn("<BB>Used</BB>", out)
        self.assertNotIn("<BB>The command</BB>", out)
        self.assertIn("<C>`Get-Content -Path agent.py -Raw`</C>", out)
        self.assertIn("<C>`powershell -ExecutionPolicy Bypass`</C>", out)

    def test_bang_prefixed_powershell_command_with_inner_command_string_is_highlighted(self):
        text = '!powershell -ExecutionPolicy Bypass -Command "Get-Content -Path agent.py -Raw"'
        with patch("cli.core.text_output_renderer._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"), patch(
            "cli.core.text_output_renderer._ansi_yellow", side_effect=lambda s: f"<Y>{s}</Y>"
        ), patch("cli.core.text_output_renderer._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"), patch(
            "cli.core.text_output_renderer._ansi_green", side_effect=lambda s: f"<G>{s}</G>"
        ), patch(
            "cli.core.text_output_renderer._ansi_ps_command", side_effect=lambda s: f"<PSC>{s}</PSC>"
        ), patch(
            "cli.core.text_output_renderer._ansi_ps_parameter", side_effect=lambda s: f"<PSP>{s}</PSP>"
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
        with patch("cli.core.text_output_renderer._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"), patch(
            "cli.core.text_output_renderer._ansi_yellow", side_effect=lambda s: f"<Y>{s}</Y>"
        ), patch("cli.core.text_output_renderer._ansi_green", side_effect=lambda s: f"<G>{s}</G>"), patch(
            "cli.core.text_output_renderer._ansi_ps_command", side_effect=lambda s: f"<PSC>{s}</PSC>"
        ), patch(
            "cli.core.text_output_renderer._ansi_ps_parameter", side_effect=lambda s: f"<PSP>{s}</PSP>"
        ), patch(
            "cli.core.text_output_renderer._ansi_ps_pipe", side_effect=lambda s: f"<PSPIPE>{s}</PSPIPE>"
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

    def test_format_tool_call_feedback_line_uses_natural_label_and_default_bullet_color(self):
        # Non-shell tools now read as a natural action phrase (no "Ran"): the
        # snake_case tool name is humanized and the args follow in parentheses.
        with patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "cli.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ), patch("cli.agent._ansi_bold", side_effect=lambda text: text):
            line = self.agent._format_tool_call_feedback_line("read", {"path": "a.txt"}, failed=False)
        self.assertTrue(line.startswith("<RGB:19,161,14>•</RGB> Read "))
        self.assertIn("<H>a.txt</H>", line)

    def test_format_tool_call_feedback_line_shell_uses_language_specific_prefix(self):
        # Shell keeps the localized "Ran <command>" phrasing.
        self.agent.display_language = "zh-CN"
        with patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "cli.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ), patch("cli.agent._ansi_bold", side_effect=lambda text: text):
            line = self.agent._format_tool_call_feedback_line("shell", {"command": "git status"}, failed=False)
        self.assertTrue(line.startswith("<RGB:19,161,14>•</RGB> 执行 "))
        self.assertIn("<H>git status</H>", line)

    def test_format_tool_call_feedback_line_shell_emits_cmd_copy_segment_in_gui_mode(self):
        # In GUI mode the shell feedback line carries the raw command in its own
        # sentinel segment so the frontend can offer a "copy full command line"
        # button without guessing where a localized verb ends.
        self.agent._gui_no_wrap = True
        with patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "cli.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ), patch("cli.agent._ansi_bold", side_effect=lambda text: text):
            line = self.agent._format_tool_call_feedback_line(
                "shell", {"command": "git status --short"}, failed=False
            )
        self.assertTrue(line.startswith("\ue004"))
        self.assertIn("\ue005", line)
        # The raw command is embedded verbatim (unhighlighted) in its segment.
        self.assertIn("\ue00agit status --short\ue00b", line)

    def test_format_tool_call_feedback_line_shell_background_emits_cmd_copy_segment(self):
        self.agent._gui_no_wrap = True
        with patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "cli.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ), patch("cli.agent._ansi_bold", side_effect=lambda text: text):
            line = self.agent._format_tool_call_feedback_line(
                "shell", {"command": "ping localhost", "background": True}, failed=False
            )
        self.assertIn("\ue00aping localhost\ue00b", line)

    def test_format_tool_call_feedback_line_non_shell_has_no_cmd_copy_segment(self):
        # Only shell calls get the copyable command segment; natural-language
        # labels (apply_patch etc.) must stay sentinel-free apart from the
        # prompt wrapper.
        self.agent._gui_no_wrap = True
        with patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "cli.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ), patch("cli.agent._ansi_bold", side_effect=lambda text: text), patch.object(
            self.agent, "_is_apply_patch_add_file", return_value=False
        ):
            line = self.agent._format_tool_call_feedback_line(
                "apply_patch", {"path": "a.txt", "patch": "x"}, failed=False
            )
        self.assertNotIn("\ue00a", line)
        self.assertNotIn("\ue00b", line)

    def test_format_tool_call_feedback_line_localizes_non_shell_label(self):
        # Non-shell tool labels are localized in zh-CN via tool.label.* keys.
        self.agent.display_language = "zh-CN"
        with patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "cli.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ), patch("cli.agent._ansi_bold", side_effect=lambda text: text):
            line = self.agent._format_tool_call_feedback_line(
                "run_subagent", {"subagent": "coder"}, failed=False
            )
        self.assertTrue(line.startswith("<RGB:19,161,14>•</RGB> 调用子智能体 "))
        self.assertIn("<H>(subagent=coder)</H>", line)

    def test_format_tool_call_feedback_line_includes_explore_topic(self):
        with patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "cli.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ), patch("cli.agent._ansi_bold", side_effect=lambda text: text):
            line = self.agent._format_tool_call_feedback_line(
                "run_subagent",
                {"subagent": "explore", "topic": "sub-agent architecture"},
                failed=False,
            )
        self.assertIn("Exploring sub-agent architecture...", line)
        self.assertNotIn("(subagent=explore", line)

    def test_format_explore_running_line_bolds_only_the_verb(self):
        with patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "cli.agent.highlight_assistant_display_line", side_effect=lambda s: s
        ), patch("cli.agent._ansi_bold", side_effect=lambda text: f"[B]{text}[/B]"):
            line = self.agent._format_tool_call_feedback_line(
                "run_subagent",
                {"subagent": "explore", "topic": "sub-agent architecture"},
                failed=False,
            )
        self.assertIn("[B]Exploring[/B]", line)
        self.assertNotIn("[B] sub-agent architecture[/B]", line)
        self.assertNotIn("sub-agent architecture[/B]", line)

    def test_explore_completed_label_includes_truncated_topic(self):
        long_topic = "x" * 90

        label = self.agent._explore_completed_label(
            {"subagent": "explore", "topic": long_topic},
            elapsed=12.3,
        )

        self.assertEqual(label, f"Explored {'x' * 77}... for 12s")

    def test_format_tool_call_feedback_line_apply_patch_localized(self):
        self.agent.display_language = "zh-CN"
        with patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "cli.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ), patch("cli.agent._ansi_bold", side_effect=lambda text: text):
            line = self.agent._format_tool_call_feedback_line(
                "apply_patch",
                {"path": "new.py", "patch": "*** Add File: new.py\n+x\n"},
                failed=False,
            )
        self.assertTrue(line.startswith("<RGB:19,161,14>•</RGB> 新增 "))

    def test_format_tool_call_feedback_line_apply_patch_create_file(self):
        # apply_patch that adds a new file reads as "Add".
        with patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "cli.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ), patch("cli.agent._ansi_bold", side_effect=lambda text: text):
            line = self.agent._format_tool_call_feedback_line(
                "apply_patch",
                {"path": "new.py", "patch": "*** Add File: new.py\n+hello\n"},
                failed=False,
            )
        self.assertTrue(line.startswith("<RGB:19,161,14>•</RGB> Add "))
        self.assertIn("<H>new.py</H>", line)

    def test_format_tool_call_feedback_line_apply_patch_edit(self):
        # apply_patch editing an existing file reads as "Edit".
        with patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "cli.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ), patch("cli.agent._ansi_bold", side_effect=lambda text: text), patch.object(
            self.agent, "_is_apply_patch_add_file", return_value=False
        ):
            line = self.agent._format_tool_call_feedback_line(
                "apply_patch",
                {"path": "edit.py", "patch": "@@\n-old\n+new\n"},
                failed=False,
            )
        self.assertTrue(line.startswith("<RGB:19,161,14>•</RGB> Edit "))
        self.assertIn("<H>edit.py</H>", line)

    def test_tool_display_path_relativizes_workspace_absolute_path(self):
        # A path inside the workspace root always displays as a
        # workspace-relative path (forward slashes), no matter how the
        # model passed it.
        root = Path(tempfile.gettempdir()) / "cw_ws_display_rel"
        self.agent.workspace_root = root
        target = root / "cli" / "main.py"
        self.assertEqual(self.agent._tool_display_path(str(target)), "cli/main.py")

    def test_tool_display_path_keeps_relative_path_unchanged(self):
        root = Path(tempfile.gettempdir()) / "cw_ws_display_keep_rel"
        self.agent.workspace_root = root
        self.assertEqual(self.agent._tool_display_path("cli/main.py"), "cli/main.py")

    def test_tool_display_path_keeps_path_outside_workspace(self):
        root = Path(tempfile.gettempdir()) / "cw_ws_display_outside_root"
        outside = Path(tempfile.gettempdir()) / "cw_ws_display_outside" / "x.py"
        self.agent.workspace_root = root
        self.assertEqual(
            self.agent._tool_display_path(str(outside)),
            str(outside),
        )

    def test_tool_display_path_falls_back_when_no_workspace_root(self):
        # Agent.__new__(Agent) carries no workspace_root; the raw arg must
        # pass through untouched.
        raw = r"D:\some\file.py"
        self.assertEqual(self.agent._tool_display_path(raw), raw)

    def test_format_tool_call_feedback_line_apply_patch_shows_workspace_relative_path(self):
        # apply_patch with an absolute path inside the workspace renders the
        # tool-call description with the workspace-relative path.
        root = Path(tempfile.gettempdir()) / "cw_ws_display_feedback"
        self.agent.workspace_root = root
        abs_p = root / "sub" / "edit.py"
        with patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "cli.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ), patch("cli.agent._ansi_bold", side_effect=lambda text: text), patch.object(
            self.agent, "_is_apply_patch_add_file", return_value=False
        ):
            line = self.agent._format_tool_call_feedback_line(
                "apply_patch",
                {"path": str(abs_p), "patch": "@@\n-old\n+new\n"},
                failed=False,
            )
        self.assertIn("<H>sub/edit.py</H>", line)
        self.assertNotIn(str(abs_p), line)

    def test_format_tool_call_feedback_line_switches_bullet_color_when_failed(self):
        with patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "cli.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ), patch("cli.agent._ansi_bold", side_effect=lambda text: text):
            line = self.agent._format_tool_call_feedback_line("read", {"path": "a.txt"}, failed=True)
        self.assertTrue(line.startswith("<RGB:197,15,31>•</RGB> Read "))
        self.assertIn("<H>a.txt</H>", line)

    def test_format_tool_call_feedback_line_request_user_input_shows_question(self):
        # The clarifying question rides on the tool call line itself so the
        # transcript reads "Ask: <question>" instead of a bare tool name.
        with patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "cli.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ), patch("cli.agent._ansi_bold", side_effect=lambda text: text):
            line = self.agent._format_tool_call_feedback_line(
                "request_user_input",
                {"question": "Which env?", "options": ["Prod", "Stg"]},
                failed=False,
            )
        self.assertTrue(line.startswith("<RGB:19,161,14>•</RGB> Ask:"))
        self.assertIn("<H>Which env?</H>", line)

    def test_extract_tool_result_output_request_user_input_lists_options(self):
        out = self.agent._extract_tool_result_output(
            "request_user_input",
            {"question": "Which env?", "options": ["Prod", "Stg", ""]},
        )
        self.assertEqual(out, "1. Prod\n2. Stg")

    def test_extract_tool_result_output_browser_eval_uses_result_field(self):
        out = self.agent._extract_tool_result_output(
            "browser_eval",
            {"success": True, "result": "document.title"},
        )
        self.assertEqual(out, "document.title")

    def test_extract_tool_result_output_browser_eval_serializes_structured_result(self):
        out = self.agent._extract_tool_result_output(
            "browser_eval",
            {"success": True, "result": {"count": 3, "items": ["a", "b"]}},
        )
        self.assertEqual(out, '{"count": 3, "items": ["a", "b"]}')

    def test_extract_tool_result_output_browser_dom_console_url(self):
        self.assertEqual(
            self.agent._extract_tool_result_output(
                "browser_read_dom", {"success": True, "dom": "<html></html>"}
            ),
            "<html></html>",
        )
        self.assertEqual(
            self.agent._extract_tool_result_output(
                "browser_read_console", {"success": True, "console": [{"level": "log", "text": "hi"}]}
            ),
            '[{"level": "log", "text": "hi"}]',
        )
        self.assertEqual(
            self.agent._extract_tool_result_output(
                "browser_get_url", {"success": True, "url": "https://example.com"}
            ),
            "https://example.com",
        )

    def test_format_direct_shell_command_feedback_line_uses_shared_highlighter(self):
        with patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "cli.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ), patch("cli.agent._ansi_bold", side_effect=lambda text: text):
            line = self.agent._format_direct_shell_command_feedback_line("git status", failed=False)
        self.assertTrue(line.startswith("<RGB:19,161,14>•</RGB> You ran "))
        self.assertIn("<H>git status</H>", line)

    def test_format_direct_shell_command_feedback_line_uses_language_specific_prefix(self):
        self.agent.display_language = "zh-CN"
        with patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"), patch(
            "cli.agent.highlight_assistant_display_line", side_effect=lambda s: f"<H>{s}</H>"
        ), patch("cli.agent._ansi_bold", side_effect=lambda text: text):
            line = self.agent._format_direct_shell_command_feedback_line("git status", failed=False)
        self.assertTrue(line.startswith("<RGB:19,161,14>•</RGB> 你执行了 "))
        self.assertIn("<H>git status</H>", line)

    def test_format_tool_call_feedback_line_wraps_long_command_with_gray_pipe_prefix(self):
        with (
            patch.object(self.agent, "_tool_call_summary", return_value="abcdef ghijkl mnopqrstuvwxyz"),
            patch.object(self.agent, "_terminal_columns_for_command_feedback", return_value=16),
            patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"),
            patch("cli.agent._ansi_gray", side_effect=lambda s: f"<G>{s}</G>"),
            patch("cli.agent.highlight_assistant_display_line", side_effect=lambda s: s),
        ):
            line = self.agent._format_tool_call_feedback_line("shell", {"command": "x"}, failed=False)
        rows = line.splitlines()
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(rows[1].startswith("<G>  │ </G>"))

    def test_format_direct_shell_command_feedback_line_wraps_long_command_with_gray_pipe_prefix(self):
        with (
            patch.object(self.agent, "_terminal_columns_for_command_feedback", return_value=18),
            patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"),
            patch("cli.agent._ansi_gray", side_effect=lambda s: f"<G>{s}</G>"),
            patch("cli.agent.highlight_assistant_display_line", side_effect=lambda s: s),
        ):
            line = self.agent._format_direct_shell_command_feedback_line("git status --short --branch --untracked-files", failed=False)
        rows = line.splitlines()
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(rows[1].startswith("<G>  │ </G>"))

    def test_format_direct_shell_command_feedback_line_rewraps_tail_with_continuation_width(self):
        with (
            patch.object(self.agent, "_terminal_columns_for_command_feedback", return_value=20),
            patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: text),
            patch("cli.agent._ansi_gray", side_effect=lambda s: s),
            patch("cli.agent.highlight_assistant_display_line", side_effect=lambda s: s),
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
            patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: f"<RGB:{r},{g},{b}>{text}</RGB>"),
            patch("cli.agent._ansi_gray", side_effect=lambda s: f"<G>{s}</G>"),
            patch("cli.agent.highlight_assistant_display_line", side_effect=lambda s: s) as mock_hl,
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
            patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: text),
            patch("cli.agent._ansi_gray", side_effect=lambda s: s),
            patch("cli.agent.highlight_assistant_display_line", side_effect=lambda s: s),
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
            patch("cli.agent.sys.stdout", stream),
            patch("cli.agent.shutil.get_terminal_size", return_value=types.SimpleNamespace(columns=80)),
            patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: text),
            patch("cli.agent._ansi_gray", side_effect=lambda s: s),
            patch("cli.agent.highlight_assistant_display_line", side_effect=lambda s: s),
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
            patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: text),
            patch("cli.agent._ansi_gray", side_effect=lambda s: f"\x1b[90m{s}\x1b[0m"),
            patch(
                "cli.agent.highlight_assistant_display_line",
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
            patch("cli.agent._ansi_gray", side_effect=lambda s: s),
        ):
            rendered = self.agent._format_user_chat_display_message("123456 7890")
        rows = rendered.splitlines()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], "› 123456")
        self.assertEqual(rows[1], "  7890")

    def test_format_slash_command_display_wraps_with_two_space_continuation(self):
        with (
            patch.object(self.agent, "_terminal_columns_for_line_estimate", return_value=8),
            patch("cli.agent._ansi_gray", side_effect=lambda s: s),
        ):
            rendered = self.agent._format_user_chat_display_message("/abcde fghi")
        rows = rendered.splitlines()
        self.assertEqual(rows, ["› /abcde", "  fghi"])

    def test_format_slash_command_display_does_not_split_single_word(self):
        with (
            patch.object(self.agent, "_terminal_columns_for_line_estimate", return_value=8),
            patch("cli.agent._ansi_gray", side_effect=lambda s: s),
        ):
            rendered = self.agent._format_user_chat_display_message("/abcdefghij")
        self.assertEqual(rendered.splitlines(), ["› /abcdefghij"])

    def test_format_assistant_chat_display_message_keeps_ansi_color_after_wrap(self):
        with (
            patch.object(self.agent, "_terminal_columns_for_line_estimate", return_value=10),
            patch("cli.agent._ansi_gray", side_effect=lambda s: s),
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
            patch("cli.agent.sys.stdout", fake_stdout),
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
        with patch("cli.agent.shutil.get_terminal_size", return_value=_Sz()):
            stream.write("alpha beta\nabc")
        self.assertEqual("".join(fake_stdout.writes), "  alpha\n  beta\n  abc")

    def test_startup_overview_inside_slash_stream_stays_within_terminal_width(self):
        from cli.runtime.runtime_loop import _print_startup_overview

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
            patch("cli.agent.shutil.get_terminal_size", return_value=types.SimpleNamespace(columns=80)),
            patch("cli.runtime.runtime_loop.sys.stdout", stream),
            patch("cli.runtime.runtime_loop.get_app_name", return_value=get_app_name()),
            patch("cli.runtime.runtime_loop.get_app_display_version", return_value="v0.1.0"),
            patch("cli.runtime.runtime_loop.get_random_startup_tip_entry", return_value={"text": "", "highlights": []}),
            patch("cli.runtime.runtime_loop._ansi_gray", side_effect=identity),
            patch("cli.runtime.runtime_loop._ansi_cyan", side_effect=identity),
            patch("cli.runtime.runtime_loop._ansi_bold", side_effect=identity),
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
        from cli.runtime.runtime_loop import _print_startup_overview, _startup_text_display_width

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
                        "cli.agent.shutil.get_terminal_size",
                        return_value=types.SimpleNamespace(columns=80),
                    ),
                    patch("cli.runtime.runtime_loop.sys.stdout", stream),
                    patch(
                        "cli.runtime.runtime_loop.get_app_name",
                        return_value=get_app_name(),
                    ),
                    patch(
                        "cli.runtime.runtime_loop.get_app_display_version",
                        return_value="v0.1.0",
                    ),
                    patch(
                        "cli.runtime.runtime_loop.get_random_startup_tip_entry",
                        return_value={"text": "", "highlights": []},
                    ),
                    patch(
                        "cli.core.localization.get_display_language",
                        return_value=lang,
                    ),
                ):
                    _print_startup_overview(_Agent())

                rendered = "".join(fake_stdout.writes)
                # Strip CSI escape sequences BEFORE filtering. When the
                # host terminal has colors enabled (or the venv keeps
                # ``NO_COLOR`` unset) the renderer wraps the border
                # characters with ``\x1b[90m...\x1b[0m`` so the raw
                # string starts with the SGR introducer rather than
                # ``│``; without the early strip the filter silently
                # drops every box row and we get a misleading
                # "0 not greater than or equal to 4" failure.
                box_rows = [
                    stripped
                    for stripped in (
                        _strip_ansi(line) for line in rendered.splitlines()
                    )
                    if stripped.lstrip().startswith("│")
                    and stripped.rstrip().endswith("│")
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
        from cli.runtime.runtime_loop import _print_startup_overview

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
                        "cli.agent.shutil.get_terminal_size",
                        return_value=types.SimpleNamespace(columns=80),
                    ),
                    patch("cli.runtime.runtime_loop.sys.stdout", stream),
                    patch(
                        "cli.runtime.runtime_loop.get_app_name",
                        return_value=get_app_name(),
                    ),
                    patch(
                        "cli.runtime.runtime_loop.get_app_display_version",
                        return_value="v0.1.0",
                    ),
                    patch(
                        "cli.runtime.runtime_loop.get_random_startup_tip_entry",
                        return_value={"text": "", "highlights": []},
                    ),
                    patch(
                        "cli.core.localization.get_display_language",
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
            patch("cli.agent.sys.stdout", stream),
            patch("cli.agent.shutil.get_terminal_size", return_value=types.SimpleNamespace(columns=80)),
            patch("cli.agent._ansi_gray", side_effect=lambda s: s),
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
            patch("cli.agent.sys.stdout", stream),
            patch("cli.agent.shutil.get_terminal_size", return_value=types.SimpleNamespace(columns=80)),
            patch("cli.agent._ansi_gray", side_effect=lambda s: s),
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
            patch("cli.agent.sys.stdout", fake_stdout),
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
            patch("cli.agent.sys.stdout", _FakeStdout()),
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
            patch("cli.agent.os.get_terminal_size", side_effect=[_Sz(120), _Sz(120)]),
            patch("cli.agent._ansi_rgb", side_effect=lambda text, r, g, b: text),
            patch("cli.agent._ansi_gray", side_effect=lambda s: s),
            patch("cli.agent.highlight_assistant_display_line", side_effect=lambda s: s),
        ):
            line = self.agent._format_direct_shell_command_feedback_line("x" * 90, failed=False)
        self.assertNotIn("\n", line)


class MarkdownRenderingTests(unittest.TestCase):
    """Lightweight Markdown rendering in the terminal display path."""

    def _render(self, text):
        with (
            patch("cli.core.text_output_renderer._ansi_bold", side_effect=lambda s: f"<B>{s}</B>"),
            patch("cli.core.text_output_renderer._ansi_italic", side_effect=lambda s: f"<I>{s}</I>"),
            patch("cli.core.text_output_renderer._ansi_cyan", side_effect=lambda s: f"<C>{s}</C>"),
            patch("cli.core.text_output_renderer._ansi_gray", side_effect=lambda s: f"<G>{s}</G>"),
            patch("cli.core.text_output_renderer._ansi_green", side_effect=lambda s: f"<GR>{s}</GR>"),
            patch("cli.core.text_output_renderer._ansi_bright_blue", side_effect=lambda s: f"<BB>{s}</BB>"),
        ):
            return aoh.highlight_assistant_display_text(text)

    def test_bold_and_italic_inline_spans(self):
        out = self._render("This is **bold** and *italic* text.")
        self.assertIn("<B>bold</B>", out)
        self.assertIn("<I>italic</I>", out)

    def test_underscore_emphasis_does_not_fire_inside_identifiers(self):
        out = self._render("call some_function_name and __strong__ here")
        # snake_case must not be italicised
        self.assertNotIn("<I>function</I>", out)
        self.assertIn("<B>strong</B>", out)

    def test_atx_heading_renders_bold_without_hashes(self):
        out = self._render("## Heading Title")
        self.assertIn("<B>", out)
        self.assertNotIn("#", out)
        self.assertIn("Heading Title", out)

    def test_inline_code_keeps_backticks(self):
        out = self._render("run `git status` now")
        self.assertIn("<C>`git status`</C>", out)

    def test_fenced_code_block_is_dimmed_and_not_token_painted(self):
        text = "```python\nx = 1  # **keep stars**\n```"
        out = self._render(text)
        self.assertIn("┌─ python", out)
        self.assertIn("└─", out)
        # Stars inside the fence must survive verbatim (no bold painting).
        self.assertIn("**keep stars**", out)
        self.assertNotIn("<B>keep stars</B>", out)

    def test_horizontal_rule_becomes_separator(self):
        out = self._render("---")
        self.assertNotIn("-", out.replace("<G>", "").replace("</G>", "").replace("─", ""))
        self.assertIn("─", out)

    def test_blockquote_prefixes_bar(self):
        out = self._render("> quoted line")
        self.assertIn("│", out)
        self.assertIn("<I>quoted line</I>", out)

    def test_dollar_amount_is_not_italicised(self):
        out = self._render("it costs $5 and $10 total")
        self.assertNotIn("<I>", out)

    def test_unordered_list_marker_renders_bullet_glyph(self):
        for marker in ("-", "*", "+"):
            out = self._render(f"{marker} an item")
            self.assertIn("<BB>• </BB>", out)
            self.assertNotIn(f"{marker} an", out.replace("•", marker))

    def test_nested_list_keeps_indentation(self):
        out = self._render("  - nested item")
        self.assertIn("<BB>  • </BB>", out)

    def test_ordered_list_marker_is_preserved(self):
        out = self._render("1. first step")
        self.assertIn("<BB>1. </BB>", out)

    def test_bold_item_inside_bullet(self):
        out = self._render("- **important** point")
        self.assertIn("<BB>• </BB>", out)
        self.assertIn("<B>important</B>", out)

    def test_standalone_bold_line(self):
        out = self._render("**Whole line bold**")
        self.assertIn("<B>Whole line bold</B>", out)
        self.assertNotIn("**", out)

    def test_bold_span_containing_inline_code_keeps_no_raw_stars(self):
        # Regression: bold wrapping inline code used to leave the ** markers raw
        # because the inner code span was painted (occupied) first.
        out = self._render("**translate `hello.py` output**")
        self.assertNotIn("**", out)
        self.assertIn("<C>`hello.py`</C>", out)
        self.assertIn("<B>", out)

    def test_bold_with_inline_code_real_ansi_reapplies_bold_after_code(self):
        # With real ANSI, the inner code reset must not cancel the outer bold for
        # the remainder of the span.
        import os
        from unittest.mock import patch as _patch

        with _patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=False):
            os.environ.pop("NO_COLOR", None)
            out = aoh.format_assistant_display_response("**a `b` c**")
        # bold opens, code is cyan, then bold re-opens before " c"
        self.assertIn("\x1b[1m", out)
        self.assertIn("\x1b[36m", out)
        self.assertIn("\x1b[0m\x1b[1m", out)

    def test_inline_math_superscript_converts_to_unicode(self):
        out = aoh.convert_inline_latex_math("The result $E = mc^2$ holds.")
        self.assertIn("E = mc\u00b2", out)
        self.assertNotIn("$", out)

    def test_inline_math_sqrt_converts(self):
        out = aoh.convert_inline_latex_math(r"We have $\sqrt{a^2 + b^2} = c$ here.")
        self.assertIn("\u221a", out)  # √
        self.assertIn("a\u00b2", out)  # a²
        self.assertNotIn("\\sqrt", out)

    def test_inline_math_leaves_prices_and_shell_vars_untouched(self):
        out = aoh.convert_inline_latex_math("It costs $5 and $PATH is set")
        self.assertEqual(out, "It costs $5 and $PATH is set")

    def test_inline_math_relation_spans_in_prose_convert(self):
        # Spans with a relation but no backslash/script (e.g. ``$y = 3$``) must
        # still be recognized as math when embedded in ordinary prose.
        out = aoh.convert_inline_latex_math("\u5c06 $y = 3$ \u4ee3\u5165 $x = y - 1$")
        self.assertEqual(out, "\u5c06 y = 3 \u4ee3\u5165 x = y - 1")

    def test_inline_math_currency_pairs_not_eaten(self):
        # ``$100 到 $200`` must not pair its dollars into a bogus math span.
        out = aoh.convert_inline_latex_math("\u7ea6 $100 \u5230 $200 \u4e4b\u95f4")
        self.assertEqual(out, "\u7ea6 $100 \u5230 $200 \u4e4b\u95f4")

    def test_inline_math_decimal_price_untouched(self):
        out = aoh.convert_inline_latex_math("price is $5.00 only")
        self.assertEqual(out, "price is $5.00 only")

    def test_inline_math_single_variable_spans_convert(self):
        # Bare single-variable spans (``$x$`` / ``$y$``) in prose are math.
        out = aoh.convert_inline_latex_math(
            "\u4e24\u4e2a\u672a\u77e5\u6570 $x$ \u548c $y$ \u7684\u65b9\u7a0b\u7ec4"
        )
        self.assertEqual(
            out, "\u4e24\u4e2a\u672a\u77e5\u6570 x \u548c y \u7684\u65b9\u7a0b\u7ec4"
        )

    def test_inline_math_cases_environment_preserves_multiline_shape(self):
        out = aoh.convert_inline_latex_math(
            "$\\begin{cases} "
            "\\text{LLM Call} \\rightarrow \\text{Tool Call} \\\\ "
            "\\text{SSE Event} \\rightarrow \\text{GUI Viewer} \\\\ "
            "\\text{Session Store} \\rightarrow \\text{Disk (.json)} "
            "\\end{cases}$"
        )
        self.assertIn("\n", out)
        self.assertIn("LLM Call", out)
        self.assertIn("SSE Event", out)
        self.assertIn("Session Store", out)
        self.assertIn("\u23a7", out)  # ⎧
        self.assertIn("\u23a8", out)  # ⎨
        self.assertIn("\u23a9", out)  # ⎩

    def test_inline_math_cases_environment_starts_on_new_line_in_prose(self):
        out = aoh.convert_inline_latex_math(
            "Nested Loop $\\begin{cases} "
            "\\text{LLM Call} \\rightarrow \\text{Tool Call} \\\\ "
            "\\text{SSE Event} \\rightarrow \\text{GUI Viewer} "
            "\\end{cases}$ final"
        )
        self.assertIn("Nested Loop \n\u23b0", out)
        self.assertIn("\u23b0 LLM Call", out)
        self.assertIn("final", out)
        block_lines = [
            ln for ln in out.split("\n")
            if ln[:1] in {"\u23b0", "\u23b1", "\u23a7", "\u23a8", "\u23a9"}
        ]
        target = next(ln for ln in block_lines if "final" in ln)
        split_at = target.rfind("final")
        prefix_width = aoh._md_cell_display_width(target[:split_at])
        block_only_width = aoh._md_cell_display_width(target[:split_at].rstrip())
        other_widths = [
            aoh._md_cell_display_width(ln)
            for ln in block_lines
            if ln != target
        ]
        self.assertEqual(prefix_width, max(other_widths + [block_only_width]) + 1)

    def test_inline_math_cases_environment_attaches_following_math_to_middle_row(self):
        out = aoh.convert_inline_latex_math(
            "Nested Loop $\\begin{cases} "
            "\\text{LLM Call} \\rightarrow \\text{Tool Call} \\\\ "
            "\\text{SSE Event} \\rightarrow \\text{GUI Viewer} \\\\ "
            "\\text{Session Store} \\rightarrow \\text{Disk (.json)} "
            "\\end{cases}$ $\\rightarrow$ Final Answer $\\xrightarrow{return}$ Main Agent"
        )
        self.assertIn("\u23a7 LLM Call", out)
        self.assertIn("\u23a9 Session Store \u2192Disk (.json)", out)
        self.assertNotIn(
            "\u23a9 Session Store \u2192Disk (.json) \u2192 Final Answer",
            out,
        )
        block_lines = [
            ln for ln in out.split("\n")
            if ln[:1] in {"\u23b0", "\u23b1", "\u23a7", "\u23a8", "\u23a9"}
        ]
        target = next(ln for ln in block_lines if "Final Answer" in ln)
        split_at = target.rfind("\u2192 Final Answer")
        self.assertGreater(split_at, 0)
        prefix_width = aoh._md_cell_display_width(target[:split_at])
        block_only_width = aoh._md_cell_display_width(target[:split_at].rstrip())
        other_widths = [
            aoh._md_cell_display_width(ln)
            for ln in block_lines
            if ln != target
        ]
        self.assertEqual(prefix_width, max(other_widths + [block_only_width]) + 1)

    def test_display_math_block_single_line_renders_centered(self):
        out = self._render("Here:\n$$ E = mc^2 $$\nDone.")
        self.assertIn("E = mc\u00b2", out)
        self.assertNotIn("$$", out)

    def test_display_math_block_multiline_renders(self):
        out = self._render("$$\n\\frac{-b \\pm \\sqrt{b^2-4ac}}{2a}\n$$")
        self.assertIn("\u221a", out)  # √
        self.assertNotIn("$$", out)
        self.assertNotIn("\\frac", out)

    def test_display_math_bracket_delimiters_render(self):
        out = self._render("\\[ a^2 + b^2 = c^2 \\]")
        self.assertIn("a\u00b2", out)
        self.assertNotIn("\\[", out)

    def test_unterminated_math_block_keeps_source_visible(self):
        # Still-streaming block (no closing fence): the raw source must remain so
        # nothing is swallowed.
        out = self._render("$$\nE = mc^2")
        self.assertIn("$$", out)

    def test_cases_environment_renders_tall_left_brace(self):
        out = self._render(
            "$$\n\\begin{cases}\nx + y = 1 \\\\\n2x - y = 3\n\\end{cases}\n$$"
        )
        # Two rows -> the two-piece tall brace (⎰ / ⎱).
        self.assertIn("\u23b0", out)
        self.assertIn("\u23b1", out)
        self.assertIn("x + y = 1", out)
        self.assertNotIn("\\begin", out)

    def test_single_line_cases_rows_are_left_aligned(self):
        # Regression: a single-line ``$$ \begin{cases} x=2 \\ y=3 \end{cases} $$``
        # left an extra leading space on the inner row(s) because the block-level
        # strip only trimmed the first/last row. Every equation must left-align
        # under the brace.
        out = self._render("$$ \\begin{cases} x = 2 \\\\ y = 3 \\end{cases} $$")
        plain = out.replace("<C>", "").replace("</C>", "")
        rows = [ln for ln in plain.split("\n") if ln.strip()]
        self.assertEqual(len(rows), 2)
        # Strip the leading indent + brace glyph; the remaining text must start
        # at the same column for both rows (no extra leading space).
        bodies = [r.lstrip()[1:].lstrip(" ") for r in rows]  # drop indent+brace
        self.assertEqual(bodies[0], "x = 2")
        self.assertEqual(bodies[1], "y = 3")

    def test_cases_environment_three_rows_uses_three_piece_brace(self):
        out = self._render(
            "$$\n\\begin{cases}\na = 1 \\\\\nb = 2 \\\\\nc = 3\n\\end{cases}\n$$"
        )
        self.assertIn("\u23a7", out)  # ⎧ top
        self.assertIn("\u23a8", out)  # ⎨ middle
        self.assertIn("\u23a9", out)  # ⎩ bottom


if __name__ == "__main__":
    unittest.main()
