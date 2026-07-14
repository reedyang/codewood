import unittest
import io
import json
import unicodedata
from pathlib import Path
import threading
import time
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from cli.config.app_info import (
    get_app_config_dirname,
    get_app_global_config_dir,
    get_app_runtime_attr_name,
    get_app_slug_kebab,
)
from cli.agent import Agent
from cli.ai.ai_special_mode_prompts import SESSION_SUMMARY_SYSTEM_PROMPT
from cli.services.session_memory_service import SessionMemoryService

DIRECT_SHELL_USER_HISTORY_PREFIX = "[DIRECT_SHELL_USER_COMMAND]"
DIRECT_SHELL_RESULT_HISTORY_PREFIX = "[DIRECT_SHELL_RESULT]"
CONVERSATION_INTERRUPTED_HISTORY_PREFIX = "[CONVERSATION_INTERRUPTED]"
INTERNAL_SLASH_USER_HISTORY_PREFIX = "[INTERNAL_SLASH_USER_COMMAND]"
INTERNAL_SLASH_RESULT_HISTORY_PREFIX = "[INTERNAL_SLASH_RESULT]"
TASK_WORKED_SUMMARY_HISTORY_PREFIX = "[TASK_WORKED_SUMMARY]"


def _display_width(text: str) -> int:
    total = 0
    for ch in str(text or ""):
        if not ch or unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


class _FakeAgent:
    def __init__(self):
        config_dirname = get_app_config_dirname()
        app_slug_kebab = get_app_slug_kebab()
        self.conversation_history = []
        self.operation_results = []
        self._session_summary_llm = ""
        self._session_summary_rolling = ""
        self._last_llm_summary_pair_count = 0
        self._skills_routing_prefix = ""
        self._active_skill_full_prompt = ""
        self._self_repo_root = Path(f"D:/SourceCode/opensource/{app_slug_kebab}")
        self.config_dir = Path(f"D:/Users/fake/{config_dirname}")
        self.workspace_name = "Default"
        self.active_chat_name = "New Chat"
        self.workspace_root = Path("D:/tmp/test")
        self.workspace_config_dir = Path(f"D:/Users/fake/{config_dirname}/workspace/default")
        self.work_directory = Path(f"D:/SourceCode/opensource/{app_slug_kebab}")
        self.system_prompt = ""
        self.params = {"context_window": 128000}
        self._force_current_input_as_requirement_once = False
        self._last_cancelled_task = ""
        self.active_chat_id = "chat-1"
        self._chat_state = {"chats": []}
        self.sync_active_chat_messages_calls = 0

    def _reload_skills(self):
        return None

    def _compose_system_prompt_snapshot(self, include_tools: bool = True):
        _ = include_tools
        return "SYSTEM PROMPT " + ("X" * 1200) + " [SYSTEM_PROMPT_END_MARK]"

    def _ensure_memory_service(self):
        return False

    def _find_chat_by_id(self, chat_id: str):
        for c in self._chat_state.get("chats", []):
            if str(c.get("id") or "") == str(chat_id or ""):
                return c
        return None

    def _sync_active_chat_messages(self):
        self.sync_active_chat_messages_calls += 1

    def _build_direct_shell_user_history_content(self, raw_user_command: str) -> str:
        return f"{DIRECT_SHELL_USER_HISTORY_PREFIX}{str(raw_user_command or '').strip()}"

    def _build_direct_shell_result_history_content(
        self,
        raw_user_command: str,
        executed_command: str,
        cwd: str,
        return_code: int,
        stdout_text: str,
        stderr_text: str,
        aborted_by_user: bool = False,
    ) -> str:
        payload = {
            "kind": "direct_shell_result",
            "invoked_by": "user",
            "raw_user_command": str(raw_user_command or ""),
            "executed_command": str(executed_command or ""),
            "cwd": str(cwd or ""),
            "return_code": int(return_code),
            "stdout": str(stdout_text or ""),
            "stderr": str(stderr_text or ""),
            "aborted_by_user": bool(aborted_by_user),
        }
        return f"{DIRECT_SHELL_RESULT_HISTORY_PREFIX}{json.dumps(payload, ensure_ascii=False)}"

    def _parse_direct_shell_user_history_content(self, content: str) -> str:
        text = str(content or "")
        if not text.startswith(DIRECT_SHELL_USER_HISTORY_PREFIX):
            return ""
        return text[len(DIRECT_SHELL_USER_HISTORY_PREFIX):].strip()

    def _parse_direct_shell_result_history_content(self, content: str):
        text = str(content or "")
        if not text.startswith(DIRECT_SHELL_RESULT_HISTORY_PREFIX):
            return None
        body = text[len(DIRECT_SHELL_RESULT_HISTORY_PREFIX):].strip()
        if not body:
            return None
        try:
            payload = json.loads(body)
        except Exception:
            return None
        return payload if isinstance(payload, dict) and str(payload.get("kind") or "") == "direct_shell_result" else None

    def _is_direct_shell_result_aborted(self, result):
        if not isinstance(result, dict):
            return False
        if bool(result.get("aborted_by_user", False)):
            return True
        return "command aborted by user" in str(result.get("stdout") or "").lower()

    def _normalize_aborted_direct_shell_stdout_for_history(self, stdout_text: str) -> str:
        text = str(stdout_text or "")
        marker = "command aborted by user"
        if marker not in text.lower():
            return text
        lines = text.splitlines(keepends=True)
        kept = []
        found = False
        for line in lines:
            low = line.lower()
            if marker not in low:
                kept.append(line)
                continue
            found = True
            idx = low.find(marker)
            rebuilt = line[:idx] + line[idx + len(marker):]
            if rebuilt.endswith("\n"):
                rebuilt = rebuilt.rstrip(" \t\r\n") + "\n"
            if rebuilt.strip():
                kept.append(rebuilt)
        merged = "".join(kept)
        if found:
            if merged and not merged.endswith("\n"):
                merged += "\n"
            merged += "command aborted by user\n"
        return merged

    def _build_conversation_interrupted_history_content(
        self, interrupted_kind: str = "task", reason: str = "user_interrupt", detail: str = ""
    ) -> str:
        payload = {
            "kind": "conversation_interrupted",
            "interrupted_kind": str(interrupted_kind or "task"),
            "reason": str(reason or "user_interrupt"),
            "detail": str(detail or ""),
        }
        return f"{CONVERSATION_INTERRUPTED_HISTORY_PREFIX}{json.dumps(payload, ensure_ascii=False)}"

    def _parse_conversation_interrupted_history_content(self, content: str):
        text = str(content or "")
        if not text.startswith(CONVERSATION_INTERRUPTED_HISTORY_PREFIX):
            return None
        body = text[len(CONVERSATION_INTERRUPTED_HISTORY_PREFIX):].strip()
        if not body:
            return None
        try:
            payload = json.loads(body)
        except Exception:
            return None
        return payload if isinstance(payload, dict) and str(payload.get("kind") or "") == "conversation_interrupted" else None

    def _build_internal_slash_user_history_content(self, raw_user_command: str) -> str:
        return f"{INTERNAL_SLASH_USER_HISTORY_PREFIX}{str(raw_user_command or '').strip()}"

    def _parse_internal_slash_user_history_content(self, content: str) -> str:
        text = str(content or "")
        if not text.startswith(INTERNAL_SLASH_USER_HISTORY_PREFIX):
            return ""
        return text[len(INTERNAL_SLASH_USER_HISTORY_PREFIX):].strip()

    def _build_internal_slash_result_history_content(self, raw_user_command: str, output_text: str) -> str:
        payload = {
            "kind": "internal_slash_result",
            "invoked_by": "user",
            "raw_user_command": str(raw_user_command or ""),
            "output": str(output_text or ""),
        }
        return f"{INTERNAL_SLASH_RESULT_HISTORY_PREFIX}{json.dumps(payload, ensure_ascii=False)}"

    def _parse_internal_slash_result_history_content(self, content: str):
        text = str(content or "")
        if not text.startswith(INTERNAL_SLASH_RESULT_HISTORY_PREFIX):
            return None
        body = text[len(INTERNAL_SLASH_RESULT_HISTORY_PREFIX):].strip()
        if not body:
            return None
        try:
            payload = json.loads(body)
        except Exception:
            return None
        return payload if isinstance(payload, dict) and str(payload.get("kind") or "") == "internal_slash_result" else None

    def _build_task_worked_summary_history_content(self, elapsed_seconds: int) -> str:
        payload = {
            "kind": "task_worked_summary",
            "elapsed_seconds": max(0, int(elapsed_seconds or 0)),
        }
        return f"{TASK_WORKED_SUMMARY_HISTORY_PREFIX}{json.dumps(payload, ensure_ascii=False)}"

    def _parse_task_worked_summary_history_content(self, content: str):
        text = str(content or "")
        if not text.startswith(TASK_WORKED_SUMMARY_HISTORY_PREFIX):
            return None
        body = text[len(TASK_WORKED_SUMMARY_HISTORY_PREFIX):].strip()
        if not body:
            return None
        try:
            payload = json.loads(body)
        except Exception:
            return None
        return payload if isinstance(payload, dict) and str(payload.get("kind") or "") == "task_worked_summary" else None


class _FakeTerminalStream:
    def __init__(self, columns: int):
        setattr(self, get_app_runtime_attr_name("terminal_columns"), lambda: columns)


class _FakeWrappedTerminalStream:
    def __init__(self, base_stream: _FakeTerminalStream):
        self._base_stream = base_stream


class SessionMemoryBudgetingTests(unittest.TestCase):
    def test_regular_task_history_starts_at_latest_compaction_summary(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 16000}
        agent._compose_system_prompt_snapshot = lambda include_tools=True: "SYSTEM"
        svc = SessionMemoryService(agent)
        summary = svc.build_context_compaction_summary_content(
            summary="The old content has been compressed here",
            mode="manual",
            covered_message_count=2,
        )
        agent.conversation_history = [
            {"role": "user", "content": "The old request should not enter the context directly"},
            {"role": "assistant", "content": "The old reply should not enter the context directly"},
            {"role": "assistant", "content": summary},
            {"role": "user", "content": "User message after summarization"},
            {"role": "assistant", "content": "Assistant message after summarization"},
        ]

        messages, _ = svc.build_regular_task_messages("Continue")
        joined = "\n".join(str(m.get("content") or "") for m in messages)

        self.assertIn("[Context summary]", joined)
        self.assertIn("The old content has been compressed here", joined)
        self.assertIn("User message after summarization", joined)
        self.assertIn("Assistant message after summarization", joined)
        self.assertNotIn("The old request should not enter the context directly", joined)
        self.assertNotIn("The old reply should not enter the context directly", joined)

    def test_manual_compact_appends_summary_and_uses_regular_call_path(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 16000}
        agent._compose_system_prompt_snapshot = lambda include_tools=True: "SYSTEM"
        svc = SessionMemoryService(agent)
        previous = svc.build_context_compaction_summary_content(
            summary="Previous summary",
            mode="auto",
            covered_message_count=4,
        )
        agent.conversation_history = [
            {"role": "user", "content": "Older message"},
            {"role": "assistant", "content": previous},
            {"role": "user", "content": "Subsequent user message"},
            {"role": "assistant", "content": "Subsequent assistant message"},
        ]
        captured = {}

        def _fake_call_ai(*args, **kwargs):
            captured["user_input"] = args[0] if args else kwargs.get("user_input")
            captured["messages_override"] = kwargs.get("messages_override")
            captured["record_history_override"] = kwargs.get("record_history_override")
            return "New merged summary"

        agent.call_ai = _fake_call_ai  # type: ignore[attr-defined]

        with redirect_stdout(io.StringIO()):
            ok = svc.compact_context("manual")

        self.assertTrue(ok)
        self.assertEqual(captured.get("record_history_override"), False)
        self.assertIsNone(captured.get("messages_override"))
        user_input = str(captured.get("user_input") or "")
        self.assertIn("compact_mode=manual", user_input)
        self.assertIn("CONTEXT CHECKPOINT COMPACTION", user_input)
        self.assertIn("Generate a concise checkpoint handoff summary", user_input)
        inserted = agent.conversation_history[-1]
        payload = svc.parse_context_compaction_summary_content(str(inserted.get("content") or ""))
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get("summary"), "New merged summary")
        self.assertEqual(payload.get("mode"), "manual")
        self.assertEqual(str(agent.conversation_history[2].get("content") or ""), "Subsequent user message")
        self.assertEqual(str(agent.conversation_history[3].get("content") or ""), "Subsequent assistant message")

    def test_compact_appends_summary_and_excludes_compaction_meta_from_model_context(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 16000}
        agent._compose_system_prompt_snapshot = lambda include_tools=True: "SYSTEM"
        svc = SessionMemoryService(agent)
        agent.conversation_history = [
            {"role": "user", "content": "Older message"},
            {
                "role": "assistant",
                "content": svc.build_context_compaction_summary_content(
                    summary="Previous summary",
                    mode="auto",
                    covered_message_count=2,
                ),
            },
            {"role": "user", "content": "Message needing summarization"},
            {"role": "assistant", "content": "Answer needing summarization"},
        ]
        agent.call_ai = lambda *args, **kwargs: "Summary body"  # type: ignore[attr-defined]

        with redirect_stdout(io.StringIO()):
            ok = svc.compact_context("manual")

        self.assertTrue(ok)
        summary_payload = svc.parse_context_compaction_summary_content(str(agent.conversation_history[-1].get("content") or ""))
        self.assertIsInstance(summary_payload, dict)
        self.assertEqual(summary_payload.get("summary"), "Summary body")
        self.assertEqual(summary_payload.get("mode"), "manual")

        messages, _ = svc.build_regular_task_messages("Continue")
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        self.assertIn("Summary body", joined)
        self.assertNotIn("Context compacted", joined)
        self.assertNotIn("Message needing summarization", joined)
        self.assertNotIn("Answer needing summarization", joined)

    def test_gui_compact_streams_summary_chunks_into_compact_notice(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 16000}
        agent._compose_system_prompt_snapshot = lambda include_tools=True: "SYSTEM"
        svc = SessionMemoryService(agent)
        agent.conversation_history = [
            {"role": "user", "content": "Older message"},
            {
                "role": "assistant",
                "content": svc.build_context_compaction_summary_content(
                    summary="Previous summary",
                    mode="auto",
                    covered_message_count=2,
                ),
            },
            {"role": "user", "content": "Message needing summarization"},
            {"role": "assistant", "content": "Answer needing summarization"},
        ]
        notices = []

        def _notice(phase, mode, title, body, text):
            notices.append((phase, mode, title, body, text))

        class _FakeStream:
            def __iter__(self):
                yield "Summary "
                yield "body"

            def close(self):
                return None

        def _fake_call_ai(*args, **kwargs):
            self.assertTrue(kwargs.get("stream"))
            return _FakeStream()

        agent._gui_compaction_notice = _notice
        agent.call_ai = _fake_call_ai  # type: ignore[attr-defined]

        with redirect_stdout(io.StringIO()):
            ok = svc.compact_context("manual")

        self.assertTrue(ok)
        self.assertGreaterEqual(len(notices), 4)
        self.assertEqual(notices[0][:4], ("start", "manual", "Compacting context", ""))
        self.assertEqual(notices[1][:4], ("stream", "manual", "Compacting context", "Summary"))
        self.assertEqual(notices[2][:4], ("stream", "manual", "Compacting context", "Summary body"))
        self.assertEqual(notices[-1][:4], ("done", "manual", "Context compacted", "Summary body"))

    def test_tui_compact_streams_summary_to_terminal_before_final_notice(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 16000}
        agent._compose_system_prompt_snapshot = lambda include_tools=True: "SYSTEM"
        svc = SessionMemoryService(agent)
        agent.conversation_history = [
            {"role": "user", "content": "Older message"},
            {
                "role": "assistant",
                "content": svc.build_context_compaction_summary_content(
                    summary="Previous summary",
                    mode="auto",
                    covered_message_count=2,
                ),
            },
            {"role": "user", "content": "Message needing summarization"},
            {"role": "assistant", "content": "Answer needing summarization"},
        ]
        reloads = []
        notices = []

        class _FakeStream:
            def __iter__(self):
                yield "Summary "
                yield "body"

            def close(self):
                return None

        def _fake_call_ai(*args, **kwargs):
            self.assertTrue(kwargs.get("stream"))
            return _FakeStream()

        def _fake_print_notice(title, body=""):
            notices.append((str(title), str(body)))
            return 3

        agent._remember_active_chat_history_tail_anchor = lambda: reloads.append("tail")  # type: ignore[attr-defined]
        agent._reload_chat_history_from_anchor_on_resize = lambda include_startup_overview=False: reloads.append(  # type: ignore[attr-defined]
            ("reload", bool(include_startup_overview))
        )

        agent.call_ai = _fake_call_ai  # type: ignore[attr-defined]

        with (
            patch.object(svc.llm_context_manager, "_print_compaction_notice", side_effect=_fake_print_notice),
            patch("cli.runtime.runtime_loop._consume_streaming_ai_response", return_value=("Summary body", True)) as mock_consume,
        ):
            ok = svc.compact_context("manual")

        self.assertTrue(ok)
        self.assertEqual(notices[0], ("Compacting context", ""))
        self.assertEqual(len(notices), 1)
        mock_consume.assert_called_once()
        self.assertEqual(reloads, ["tail", ("reload", False)])

    def test_auto_compact_candidate_selection_includes_recent_tail(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 1000}
        agent._compose_system_prompt_snapshot = lambda include_tools=True: "SYSTEM"
        agent.token_estimator = lambda s: len(str(s or ""))
        svc = SessionMemoryService(agent)
        previous = svc.build_context_compaction_summary_content(
            summary="Previous summary",
            mode="manual",
            covered_message_count=2,
        )
        agent.conversation_history = [
            {"role": "assistant", "content": previous},
            {"role": "user", "content": "Earlier user message to be summarized " + ("x" * 80)},
            {"role": "assistant", "content": "Earlier assistant message to be summarized " + ("y" * 80)},
            {"role": "user", "content": "Short tail"},
            {"role": "assistant", "content": "Short reply"},
        ]

        candidates = svc._compaction_candidate_rows("auto")
        candidate_text = "\n".join(str(m.get("content") or "") for _idx, m in candidates)

        self.assertIn("Previous summary", candidate_text)
        self.assertIn("Earlier user message to be summarized", candidate_text)
        self.assertIn("Earlier assistant message to be summarized", candidate_text)
        self.assertIn("Short tail", candidate_text)
        self.assertIn("Short reply", candidate_text)

    def test_manual_compact_candidate_selection_includes_recent_tail(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 1000}
        agent._compose_system_prompt_snapshot = lambda include_tools=True: "SYSTEM"
        agent.token_estimator = lambda s: len(str(s or ""))
        svc = SessionMemoryService(agent)
        previous = svc.build_context_compaction_summary_content(
            summary="Previous summary",
            mode="manual",
            covered_message_count=2,
        )
        agent.conversation_history = [
            {"role": "assistant", "content": previous},
            {"role": "user", "content": "Earlier user message to be summarized " + ("x" * 80)},
            {"role": "assistant", "content": "Earlier assistant message to be summarized " + ("y" * 80)},
            {"role": "user", "content": "Short tail"},
            {"role": "assistant", "content": "Short reply"},
        ]

        candidates = svc._compaction_candidate_rows("manual")
        candidate_text = "\n".join(str(m.get("content") or "") for _idx, m in candidates)

        self.assertIn("Previous summary", candidate_text)
        self.assertIn("Earlier user message to be summarized", candidate_text)
        self.assertIn("Earlier assistant message to be summarized", candidate_text)
        self.assertIn("Short tail", candidate_text)
        self.assertIn("Short reply", candidate_text)

    def test_compaction_banner_line_is_centered_and_full_width(self):
        agent = _FakeAgent()
        agent._terminal_columns_for_prompt_separator = lambda default=80: 41
        svc = SessionMemoryService(agent)

        line = svc._format_compaction_banner_line("Compacting context")

        self.assertEqual(len(line), 41)
        self.assertIn(" Compacting context ", line)
        self.assertTrue(line.startswith("─"))
        self.assertTrue(line.endswith("─"))

    def test_compaction_banner_line_uses_display_width_for_chinese_text(self):
        agent = _FakeAgent()
        agent._terminal_columns_for_prompt_separator = lambda default=80: 41
        svc = SessionMemoryService(agent)

        line = svc._format_compaction_banner_line("上下文已压缩")

        self.assertEqual(_display_width(line), 41)
        self.assertIn(" 上下文已压缩 ", line)
        self.assertTrue(line.startswith("─"))
        self.assertTrue(line.endswith("─"))

    def test_compaction_banner_uses_same_width_as_prompt_separator(self):
        agent = _FakeAgent()
        agent._terminal_columns_for_prompt_separator = lambda default=80: 41
        svc = SessionMemoryService(agent)
        fake_stdout = _FakeTerminalStream(42)

        with patch("cli.services.session_memory_service.sys.stdout", fake_stdout):
            line = svc._format_compaction_banner_line("Compacting context")

        self.assertEqual(len(line), 41)

    def test_compaction_banner_uses_visible_raw_width_when_stdout_is_wrapped(self):
        agent = _FakeAgent()
        agent._terminal_columns_for_prompt_separator = lambda default=80: 41
        svc = SessionMemoryService(agent)
        fake_stdout = _FakeWrappedTerminalStream(_FakeTerminalStream(42))

        with patch("cli.services.session_memory_service.sys.stdout", fake_stdout):
            line = svc._format_compaction_banner_line("Compacting context")

        self.assertEqual(len(line), 41)

    def test_compaction_banner_prints_gray_line(self):
        agent = _FakeAgent()
        agent._terminal_columns_for_prompt_separator = lambda default=80: 41
        svc = SessionMemoryService(agent)

        with patch("cli.services.session_memory_service._ansi_gray", side_effect=lambda s: f"<gray>{s}</gray>"):
            out = io.StringIO()
            with redirect_stdout(out):
                rendered = svc._print_compaction_banner("Automatically compacting context")

        self.assertEqual(rendered, 3)
        self.assertIn("<gray>", out.getvalue())
        self.assertIn(" Automatically compacting context ", out.getvalue())

    def test_format_context_compaction_title_localizes_summary_mode(self):
        agent = _FakeAgent()
        agent.display_language = "zh-CN"
        svc = SessionMemoryService(agent)

        msg = svc.format_context_compaction_title(
            {"summary": "Summary body", "mode": "auto"}
        )

        self.assertEqual(msg, "上下文已自动压缩")

    def test_build_context_compaction_display_payload_uses_summary_mode_title(self):
        agent = _FakeAgent()
        agent.display_language = "zh-CN"
        svc = SessionMemoryService(agent)

        content = svc.build_context_compaction_summary_content(
            summary="Summary body",
            mode="manual",
            covered_message_count=2,
        )
        payload = svc.build_context_compaction_display_payload(content)

        self.assertEqual(payload["title"], "上下文已压缩")
        self.assertEqual(payload["body"], "Summary body")

    def test_refresh_context_usage_snapshot_persists_chat_state_immediately(self):
        agent = _FakeAgent()
        agent.conversation_history = [
            {"role": "user", "content": "Implement feature A"},
            {"role": "assistant", "content": "Got it, I will inspect the structure first"},
        ]
        svc = SessionMemoryService(agent)

        svc.refresh_context_usage_snapshot(user_input_hint="Continue", context_hint="ctx")

        self.assertGreater(agent.sync_active_chat_messages_calls, 0)

    def test_software_development_prompt_is_always_appended(self):
        agent = _FakeAgent()
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("Please fix the build error")
        system_content = str(messages[0].get("content") or "")
        self.assertIn("Domain Prompt: Software Development", system_content)

    def test_experiential_memory_is_injected_into_user_message(self):
        agent = _FakeAgent()
        agent._compose_system_prompt_snapshot = lambda include_tools=True: "SYSTEM"
        agent.conversation_history = [
            {"role": "user", "content": "History user message"},
            {"role": "assistant", "content": "History assistant message"},
        ]
        svc = SessionMemoryService(agent)
        svc.memory_context_for_user_message = lambda user_input, max_chars=2400: (
            "[Experiential memory test]\nRemember this"
        )

        messages, _ = svc.build_regular_task_messages("Current request")
        joined = "\n".join(str(m.get("content") or "") for m in messages)

        self.assertIn("History user message", joined)
        self.assertIn("History assistant message", joined)
        # Memory context is injected into the last user message, not as a separate system message
        last_msg = messages[-1]
        self.assertEqual("user", last_msg.get("role"))
        self.assertIn("[Experiential memory test]", str(last_msg.get("content") or ""))
        self.assertIn("_memory_context", last_msg)

    def test_skill_prompt_not_injected_into_system_messages(self):
        agent = _FakeAgent()
        agent._active_skill_id = "codex-usage"
        agent._active_skill_source = "local"
        agent._active_skill_section = 1
        agent._active_skill_total_sections = 1
        agent._active_skill_chunked = False
        agent._active_skill_full_prompt = (
            "ACTIVE_SKILL_PROMPT_START\n"
            + ("long skill rule body that used to be clipped " * 240)
            + "\nACTIVE_SKILL_PROMPT_END"
        )
        svc = SessionMemoryService(agent)

        messages, _ = svc.build_regular_task_messages("Check my Codex usage")
        joined = "\n".join(str(m.get("content") or "") for m in messages)

        self.assertNotIn("[Dynamic skill body (front-loaded full injection)]", joined)
        self.assertNotIn("----- BEGIN ACTIVE SKILL PROMPT -----", joined)
        self.assertNotIn("----- END ACTIVE SKILL PROMPT -----", joined)

    def test_context_window_below_64k_uses_history_only_chat_context(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 63999}
        calls = {"reload": 0, "compose": 0}

        def _reload_skills():
            calls["reload"] += 1

        def _compose(include_tools=True):
            _ = include_tools
            calls["compose"] += 1
            return "SYSTEM SHOULD NOT BE SENT"

        agent._reload_skills = _reload_skills
        agent._compose_system_prompt_snapshot = _compose
        agent.operation_results = [{"secret": "operation-context"}]
        agent.conversation_history = [
            {"role": "user", "content": "Previous round question"},
            {"role": "assistant", "content": "Previous round answer"},
        ]
        svc = SessionMemoryService(agent)

        messages, _ = svc.build_regular_task_messages("Hello", context="ctx-should-not-be-sent")
        joined = "\n".join(str(m.get("content") or "") for m in messages)

        self.assertTrue(any(str(m.get("role") or "") == "system" for m in messages))
        self.assertIn("Software Development", joined)
        self.assertEqual(messages[-1], {"role": "user", "content": "Hello"})
        self.assertIn("Previous round question", joined)
        self.assertIn("Previous round answer", joined)
        self.assertNotIn("SYSTEM SHOULD NOT BE SENT", joined)
        self.assertNotIn("Original user request:", joined)
        self.assertNotIn("ctx-should-not-be-sent", joined)
        self.assertNotIn("operation-context", joined)
        self.assertEqual(calls, {"reload": 0, "compose": 0})

    def test_context_window_at_64k_keeps_full_system_context(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 64000}
        agent._compose_system_prompt_snapshot = lambda include_tools=True: "SYSTEM AT 64K"
        svc = SessionMemoryService(agent)

        messages, _ = svc.build_regular_task_messages("Hello")
        joined = "\n".join(str(m.get("content") or "") for m in messages)

        self.assertEqual(messages[0].get("role"), "system")
        self.assertIn("SYSTEM AT 64K", joined)

    def test_context_eligible_history_returns_all_context_eligible_messages(self):
        agent = _FakeAgent()
        agent._chat_state = {
            "chats": [
                {
                    "id": "chat-1",
                    "messages": [
                        {"role": "user", "content": "C1"},
                        {"role": "user", "content": "A1"},
                        {"role": "assistant", "content": "A2"},
                        {"role": "user", "content": "B1"},
                        {"role": "assistant", "content": "B2"},
                    ],
                }
            ]
        }
        agent.conversation_history = list(agent._chat_state["chats"][0]["messages"])
        svc = SessionMemoryService(agent)
        filtered = svc._context_eligible_history()
        joined = " | ".join(str(x.get("content") or "") for x in filtered)
        self.assertIn("A1", joined)
        self.assertIn("B1", joined)
        self.assertIn("C1", joined)

    def test_context_eligible_history_ignores_slash_builtin_messages(self):
        agent = _FakeAgent()
        agent._chat_state = {
            "chats": [
                {
                    "id": "chat-1",
                    "messages": [
                        {"role": "user", "content": "A1"},
                        {"role": "assistant", "content": "A2"},
                        {"role": "user", "content": "/chat list"},
                    ],
                }
            ]
        }
        agent.conversation_history = list(agent._chat_state["chats"][0]["messages"])
        svc = SessionMemoryService(agent)
        filtered = svc._context_eligible_history()
        joined = " | ".join(str(x.get("content") or "") for x in filtered)
        self.assertIn("A1", joined)
        self.assertIn("A2", joined)
        self.assertNotIn("/chat list", joined)

    def test_context_eligible_history_skips_cancelled_unanswered_user_message(self):
        agent = _FakeAgent()
        agent._chat_state = {
            "chats": [
                {
                    "id": "chat-1",
                    "messages": [
                        {"role": "user", "content": "A1"},
                        {"role": "assistant", "content": "A2"},
                        {
                            "role": "user",
                            "content": "B1-pending",
                            "exclude_from_model_context": True,
                        },
                    ],
                }
            ]
        }
        agent.conversation_history = list(agent._chat_state["chats"][0]["messages"])
        svc = SessionMemoryService(agent)
        filtered = svc._context_eligible_history()
        joined = " | ".join(str(x.get("content") or "") for x in filtered)
        self.assertIn("A1", joined)
        self.assertIn("A2", joined)
        self.assertNotIn("B1-pending", joined)

    def test_context_eligible_history_keeps_internal_interrupted_marker(self):
        agent = _FakeAgent()
        interrupted = agent._build_conversation_interrupted_history_content(
            interrupted_kind="task",
            reason="user_interrupt",
            detail="B1-pending",
        )
        agent._chat_state = {
            "chats": [
                {
                    "id": "chat-1",
                    "messages": [
                        {"role": "user", "content": "A1"},
                        {"role": "assistant", "content": "A2"},
                        {
                            "role": "user",
                            "content": "B1-pending",
                            "exclude_from_model_context": True,
                        },
                        {"role": "assistant", "content": interrupted},
                    ],
                }
            ]
        }
        agent.conversation_history = list(agent._chat_state["chats"][0]["messages"])
        svc = SessionMemoryService(agent)

        filtered = svc._context_eligible_history()

        joined = " | ".join(str(x.get("content") or "") for x in filtered)
        self.assertIn("A1", joined)
        self.assertIn("A2", joined)
        self.assertFalse(any(str(x.get("content") or "") == "B1-pending" for x in filtered))
        self.assertIn("[CONVERSATION_INTERRUPTED]", joined)

    def test_mark_latest_unanswered_user_message_for_cancel_marks_global_tail(self):
        agent = _FakeAgent()
        agent.conversation_history = [
            {"role": "user", "content": "answered"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "pending-tail"},
        ]
        svc = SessionMemoryService(agent)

        marked = svc.mark_latest_unanswered_user_message_for_cancel()

        self.assertEqual(marked, 1)
        self.assertTrue(bool(agent.conversation_history[2].get("exclude_from_model_context", False)))

    def test_build_history_budget_skips_cancelled_unanswered_user_message(self):
        agent = _FakeAgent()
        svc = SessionMemoryService(agent)
        hist = [
            {"role": "user", "content": "keep-me"},
            {
                "role": "user",
                "content": "skip-me",
                "exclude_from_model_context": True,
            },
            {"role": "assistant", "content": "assistant-1"},
        ]
        msgs, _ = svc._build_history_messages_by_budget(
            history_budget=2000,
            summary_budget=120,
            assistant_clip_tokens=300,
            source_history=hist,
        )
        joined = " | ".join(str(m.get("content") or "") for m in msgs)
        self.assertIn("keep-me", joined)
        self.assertIn("assistant-1", joined)
        self.assertNotIn("skip-me", joined)

    def test_first_user_requirement_skips_cancelled_unanswered_user_message(self):
        agent = _FakeAgent()
        agent._chat_state = {
            "chats": [
                {
                    "id": "chat-1",
                    "messages": [
                        {"role": "user", "content": "old-requirement"},
                        {
                            "role": "user",
                            "content": "pending-requirement",
                            "exclude_from_model_context": True,
                        },
                    ],
                }
            ]
        }
        agent.conversation_history = list(agent._chat_state["chats"][0]["messages"])
        svc = SessionMemoryService(agent)
        self.assertEqual(svc._first_user_requirement("fallback"), "old-requirement")

    def test_context_window_budget_prefers_recent_history(self):
        agent = _FakeAgent()
        # First requirement should be preserved explicitly.
        agent.conversation_history.append({"role": "user", "content": "Initial request: build a task planner"})
        # Create long history to trigger budget trimming.
        for i in range(1, 24):
            role = "assistant" if i % 2 == 0 else "user"
            agent.conversation_history.append(
                {"role": role, "content": f"msg-{i} " + ("z" * 180)}
            )
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("Now please continue with step 2", context="ctx-" + ("c" * 400))

        self.assertGreaterEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["role"], "user")
        # Latest history should be present (reverse fill by budget).
        joined = "\n".join(str(m.get("content") or "") for m in messages[1:-1])
        self.assertIn("msg-23", joined)

    def test_regular_task_messages_include_recent_aborted_command_context(self):
        agent = _FakeAgent()
        agent.conversation_history = [
            {"role": "user", "content": agent._build_direct_shell_user_history_content("d:/tmp/builds/install-zr.bat")},
            {
                "role": "assistant",
                "content": agent._build_direct_shell_result_history_content(
                    "!d:/tmp/builds/install-zr.bat",
                    "d:/tmp/builds/install-zr.bat",
                    "D:/ws",
                    137,
                    "02:20:33 [Info] Searching artifacts...\ncommand aborted by user\n",
                    "",
                    aborted_by_user=True,
                ),
            },
        ]
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("Continue processing the follow-up steps")
        history_joined = "\n".join(str(m.get("content") or "") for m in messages[1:-1])
        self.assertIn("[Command result]", history_joined)
        self.assertIn("executed_success=false", history_joined)
        self.assertIn("interrupted_by_user=true", history_joined)
        self.assertIn("The most recent direct command execution was forcibly terminated by the user", str(messages[-1]["content"]))

    def test_regular_task_messages_include_direct_command_success_status(self):
        agent = _FakeAgent()
        agent.conversation_history = [
            {"role": "user", "content": agent._build_direct_shell_user_history_content("echo hello")},
            {
                "role": "assistant",
                "content": agent._build_direct_shell_result_history_content(
                    "!echo hello",
                    "echo hello",
                    "D:/ws",
                    0,
                    "hello\n",
                    "",
                    aborted_by_user=False,
                ),
            },
        ]
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("Continue")
        history_joined = "\n".join(str(m.get("content") or "") for m in messages[1:-1])
        self.assertIn("[Command result]", history_joined)
        self.assertIn("executed_success=true", history_joined)
        self.assertIn("interrupted_by_user=false", history_joined)

    def test_regular_task_messages_include_recent_interrupted_task_context(self):
        agent = _FakeAgent()
        agent.conversation_history = [
            {"role": "user", "content": "Please continue fixing the build"},
            {
                "role": "assistant",
                "content": agent._build_conversation_interrupted_history_content(
                    interrupted_kind="task",
                    reason="user_interrupt",
                    detail="Fix the build script",
                ),
            },
        ]
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("Continue")
        history_joined = "\n".join(str(m.get("content") or "") for m in messages[1:-1])
        self.assertIn("[Session interruption event]", history_joined)
        self.assertIn("The most recent task execution was interrupted by the user (ESC)", str(messages[-1]["content"]))

    def test_internal_slash_history_is_excluded_from_model_context_and_requirement(self):
        agent = _FakeAgent()
        agent.conversation_history = [
            {"role": "user", "content": agent._build_internal_slash_user_history_content("/chat reload")},
            {
                "role": "assistant",
                "content": agent._build_internal_slash_result_history_content(
                    "/chat reload",
                    "reloaded\n",
                ),
            },
            {"role": "user", "content": "Initial request: fix the build"},
            {"role": "assistant", "content": "Got it"},
        ]
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("Continue execution")
        history_joined = "\n".join(str(m.get("content") or "") for m in messages[1:-1])
        self.assertNotIn("/chat reload", history_joined)
        self.assertNotIn("reloaded", history_joined)

    def test_raw_slash_builtin_user_message_is_excluded_from_model_context_and_requirement(self):
        agent = _FakeAgent()
        agent.conversation_history = [
            {"role": "user", "content": "/chat reload"},
            {"role": "assistant", "content": "reloaded"},
            {"role": "user", "content": "Initial request: fix the build"},
            {"role": "assistant", "content": "Got it"},
        ]
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("Continue execution")
        history_joined = "\n".join(str(m.get("content") or "") for m in messages[1:-1])
        self.assertNotIn("/chat reload", history_joined)

    def test_task_worked_summary_history_is_excluded_from_model_context(self):
        agent = _FakeAgent()
        agent.conversation_history = [
            {"role": "user", "content": "Initial request: fix the build"},
            {"role": "assistant", "content": "Got it"},
            {"role": "assistant", "content": agent._build_task_worked_summary_history_content(95)},
        ]
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("Continue execution")
        history_joined = "\n".join(str(m.get("content") or "") for m in messages[1:-1])
        self.assertNotIn("TASK_WORKED_SUMMARY", history_joined)
        self.assertNotIn("Worked for", history_joined)

    def test_original_requirement_falls_back_to_current_input(self):
        agent = _FakeAgent()
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("Please write a script")

    def test_cancelled_previous_task_forces_new_requirement_once(self):
        agent = _FakeAgent()
        agent.conversation_history.append({"role": "user", "content": "Old task: fix module A"})
        agent._force_current_input_as_requirement_once = True
        agent._last_cancelled_task = "Old task: fix module A"
        svc = SessionMemoryService(agent)

        messages, _ = svc.build_regular_task_messages("New task: implement feature B")
        user_block = messages[-1]["content"]
        self.assertIn("Recently cancelled task: Old task: fix module A", user_block)
        self.assertIn("do not proactively resume or redo the cancelled task", user_block)
        self.assertFalse(bool(getattr(agent, "_force_current_input_as_requirement_once", True)))

    def test_degradation_adds_history_summary_before_full_drop(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 64000}
        agent.token_estimator = lambda s: len(str(s or ""))
        agent.conversation_history.append({"role": "user", "content": "Initial request: build a task planner"})
        for i in range(1, 100):
            role = "assistant" if i % 2 == 0 else "user"
            content = f"msg-{i} " + ("assistant-long " * 700 if role == "assistant" else ("user-long " * 450))
            agent.conversation_history.append({"role": role, "content": content})
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("Continue", context="ctx")
        history_messages = messages[1:-1]
        joined = "\n".join(str(m.get("content") or "") for m in history_messages)
        self.assertIn("[History summary]", joined)
        self.assertIn("msg-99", joined)

    def test_session_summary_prompt_is_experience_dense(self):
        self.assertIn("dense, retrieval-friendly summary", SESSION_SUMMARY_SYSTEM_PROMPT)
        self.assertIn("Goals: ...", SESSION_SUMMARY_SYSTEM_PROMPT)
        self.assertIn(
            "Facts: Paths/Commands/Tool results: ...; Environment/Workspace: ...; Errors/Fixes: ...",
            SESSION_SUMMARY_SYSTEM_PROMPT,
        )
        self.assertIn("Preferences: ...", SESSION_SUMMARY_SYSTEM_PROMPT)
        self.assertIn("Decisions: ...", SESSION_SUMMARY_SYSTEM_PROMPT)
        self.assertIn("Errors: ...", SESSION_SUMMARY_SYSTEM_PROMPT)
        self.assertIn("Next steps: ...", SESSION_SUMMARY_SYSTEM_PROMPT)
        self.assertIn("Tools, commands, files, paths", SESSION_SUMMARY_SYSTEM_PROMPT)
        self.assertIn("one-off details that could change future behavior or retrieval results", SESSION_SUMMARY_SYSTEM_PROMPT)

    def test_compaction_user_input_requests_concrete_experience_details(self):
        agent = _FakeAgent()
        svc = SessionMemoryService(agent)

        user_content = svc.build_compaction_user_input("manual")

        self.assertIn("compact_mode=manual", user_content)
        self.assertIn("CONTEXT CHECKPOINT COMPACTION", user_content)
        self.assertIn("Create a handoff summary for another LLM", user_content)
        self.assertIn("Current progress and key decisions made", user_content)
        self.assertIn(
            "What remains to be done (clear next steps)",
            user_content,
        )
        self.assertIn("generate a concise checkpoint handoff summary", user_content.lower())
        self.assertNotIn("six-field format", user_content)

    def test_rolling_session_summary_uses_fixed_fields_and_preserves_details(self):
        agent = _FakeAgent()
        agent.conversation_history = [
            {
                "role": "user",
                "content": "Goal: improve cache hit rate for DeepSeek prompts; command: python -m unittest tests.unit.runtime.test_session_memory_budgeting -q",
            },
            {
                "role": "assistant",
                "content": "Workspace: C:/Users/reed/SourceCode/codewood; OS: Windows; repository path: src/services/session_memory_service.py",
            },
            {
                "role": "user",
                "content": "Preference: keep summaries fielded and avoid losing error details",
            },
            {
                "role": "assistant",
                "content": "Error: the old summary was too generic; fix tried: rewrite the summary prompt",
            },
            {
                "role": "assistant",
                "content": "Next steps: add more structured fact buckets and rerun tests",
            },
        ]
        svc = SessionMemoryService(agent)

        svc.update_session_summary_rolling()
        roll = agent._session_summary_rolling

        self.assertTrue(roll.startswith("[Session excerpt]\n"))
        self.assertIn("Goals:", roll)
        self.assertIn("Facts:", roll)
        self.assertIn("Paths/Commands/Tool results:", roll)
        self.assertIn("Environment/Workspace:", roll)
        self.assertIn("Errors/Fixes:", roll)
        self.assertIn("Preferences:", roll)
        self.assertIn("Decisions:", roll)
        self.assertIn("Errors:", roll)
        self.assertIn("Next steps:", roll)
        self.assertIn("cache hit rate", roll)
        self.assertIn("python -m unittest tests.unit.runtime.test_session_memory_budgeting -q", roll)
        self.assertIn("C:/Users/reed/SourceCode/codewood", roll)
        self.assertIn("too generic", roll)

    def test_session_summary_for_retrieval_uses_full_llm_budget(self):
        agent = _FakeAgent()
        agent._session_summary_llm = "S" * 1400
        svc = SessionMemoryService(agent)

        summary = svc.session_summary_for_retrieval()
        prefix, body = summary.split("\n", 1)

        self.assertEqual(prefix, "[Session summary]")
        self.assertEqual(len(body), 1200)
        self.assertTrue(body.endswith("S"))

    def test_large_context_keeps_more_history_than_small_context(self):
        small = _FakeAgent()
        large = _FakeAgent()
        small.params = {"context_window": 8000}
        large.params = {"context_window": 128000}
        for i in range(1, 28):
            role = "assistant" if i % 2 == 0 else "user"
            item = {"role": role, "content": f"round-{i} " + ("z" * 200)}
            small.conversation_history.append(dict(item))
            large.conversation_history.append(dict(item))
        sm_svc = SessionMemoryService(small)
        lg_svc = SessionMemoryService(large)
        small_messages, _ = sm_svc.build_regular_task_messages("Continue")
        large_messages, _ = lg_svc.build_regular_task_messages("Continue")
        small_hist_count = len(small_messages[1:-1])
        large_hist_count = len(large_messages[1:-1])
        self.assertGreaterEqual(large_hist_count, small_hist_count)

    def test_refresh_model_dependent_caches_clears_domain_prompt_cache_for_model_switch(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 128000}
        agent._small_model = False
        agent.tool_specs = []
        agent._base_system_prompt = "BASE"
        agent._load_tools_spec_from_jsonc = lambda: []
        agent._load_tools_prompt_template = lambda small_model=False: f"TOOLS:{small_model}"
        agent._load_tools_prompt_memory_template = lambda small_model=False: f"MEM:{small_model}"
        compose_calls = []

        def _compose(include_tools=True):
            compose_calls.append(bool(include_tools))
            return f"PROMPT:{agent.params.get('context_window')}"

        agent._compose_system_prompt_snapshot = _compose
        agent.session_memory_service = SessionMemoryService(agent)

        prompt_dir = Path(__file__).resolve().parents[3] / "prompts"
        large_expected = "\n\n" + (prompt_dir / "domain_software_development.md").read_text(encoding="utf-8").strip() + "\n"
        small_expected = "\n\n" + (prompt_dir / "small" / "domain_software_development.md").read_text(encoding="utf-8").strip() + "\n"

        first = agent.session_memory_service.llm_context_manager._software_development_prompt_append()
        self.assertEqual(first, large_expected)

        agent.params = {"context_window": 32000}
        Agent._refresh_model_dependent_caches(agent)

        second = agent.session_memory_service.llm_context_manager._software_development_prompt_append()
        self.assertEqual(second, small_expected)
        self.assertNotEqual(first, second)
        self.assertEqual(agent.system_prompt, "PROMPT:32000")
        self.assertIn(False, compose_calls)

    def test_custom_token_estimator_is_used(self):
        base = _FakeAgent()
        custom = _FakeAgent()
        base.params = {"context_window": 2000}
        custom.params = {"context_window": 2000}
        custom.token_estimator = lambda s: len(str(s or ""))
        for i in range(1, 18):
            role = "assistant" if i % 2 == 0 else "user"
            item = {"role": role, "content": f"msg-{i} " + ("q" * 140)}
            base.conversation_history.append(dict(item))
            custom.conversation_history.append(dict(item))
        base_svc = SessionMemoryService(base)
        custom_svc = SessionMemoryService(custom)
        base_messages, _ = base_svc.build_regular_task_messages("Continue")
        custom_messages, _ = custom_svc.build_regular_task_messages("Continue")
        self.assertLessEqual(len(custom_messages[1:-1]), len(base_messages[1:-1]))

    def test_token_counter_resolution_is_non_blocking_before_warmup_ready(self):
        agent = _FakeAgent()
        svc = SessionMemoryService(agent)
        svc._builtin_token_counter_init_done = False
        svc._builtin_token_counter = None
        called = {"n": 0}

        def _fake_warmup():
            called["n"] += 1

        svc._start_token_counter_warmup = _fake_warmup  # type: ignore[assignment]
        out = svc._resolve_token_counter()
        self.assertIsNone(out)
        self.assertEqual(called["n"], 1)
        # Falls back to heuristic immediately instead of blocking.
        self.assertGreater(svc._estimate_text_tokens("hello world"), 0)

    def test_schedule_context_usage_refresh_async_is_non_blocking_and_debounced(self):
        agent = _FakeAgent()
        svc = SessionMemoryService(agent)
        calls = []
        entered = threading.Event()
        replayed = threading.Event()
        release = threading.Event()

        def _slow_refresh(*_args, **kwargs):
            calls.append(dict(kwargs))
            if len(calls) == 1:
                entered.set()
                release.wait(timeout=1.0)
                return
            replayed.set()

        svc.refresh_context_usage_snapshot = _slow_refresh  # type: ignore[assignment]
        first = svc.schedule_context_usage_refresh_async()
        self.assertTrue(first)
        self.assertTrue(entered.wait(timeout=0.5))
        # Inflight refresh should reject immediate duplicate scheduling but keep latest request pending.
        second = svc.schedule_context_usage_refresh_async(user_input_hint="u2", context_hint="ctx2")
        self.assertFalse(second)
        release.set()
        self.assertTrue(replayed.wait(timeout=0.5))
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[-1].get("context_hint"), "ctx2")
        # allow worker to exit
        time.sleep(0.05)
        third = svc.schedule_context_usage_refresh_async()
        self.assertTrue(third)

    def test_refresh_context_usage_snapshot_skips_when_expected_chat_mismatch(self):
        agent = _FakeAgent()
        agent.active_chat_id = "chat-2"
        agent._last_context_usage_percent = 12
        agent._last_context_input_tokens = 111
        svc = SessionMemoryService(agent)

        svc.refresh_context_usage_snapshot(
            user_input_hint="new",
            context_hint="ctx",
            expected_chat_id="chat-1",
        )

        self.assertEqual(agent._last_context_usage_percent, 12)
        self.assertEqual(agent._last_context_input_tokens, 111)

    def test_refresh_context_usage_snapshot_uses_composed_prompt_snapshot(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 100000}
        agent.system_prompt = "MUTATED-" + ("X" * 6000)
        compose_calls = {"n": 0}

        def _compose(include_tools=True):
            _ = include_tools
            compose_calls["n"] += 1
            return "STABLE_PROMPT"

        agent._compose_system_prompt_snapshot = _compose
        svc = SessionMemoryService(agent)

        svc.refresh_context_usage_snapshot(user_input_hint="Continue", context_hint="ctx")
        first = int(getattr(agent, "_last_context_input_tokens", 0) or 0)

        # Mutate global system_prompt again; refresh should stay stable because it uses composed snapshot.
        agent.system_prompt = "MUTATED-2-" + ("Y" * 8000)
        svc.refresh_context_usage_snapshot(user_input_hint="Continue", context_hint="ctx")
        second = int(getattr(agent, "_last_context_input_tokens", 0) or 0)

        self.assertGreaterEqual(compose_calls["n"], 2)
        self.assertEqual(first, second)

    def test_history_tokens_cumulative_uses_last_cache_anchor_and_anchor_output(self):
        agent = _FakeAgent()
        svc = SessionMemoryService(agent)

        messages = [
            {"role": "user", "content": "查看我的Codex用量", "_token_count": 14},
            {
                "role": "assistant",
                "content": '{"tool_calls": []}',
                "_cache_stats": {"input_tokens": 15097},
                "_output_tokens": 176,
                "_reasoning_tokens": 0,
                "_token_count_includes_reasoning": False,
            },
            {
                "role": "user",
                "content": "...",
                "_internal": True,
                "_token_count": 981,
            },
            {
                "role": "assistant",
                "content": '{"tool_calls": []}',
                "_cache_stats": {"input_tokens": 16059},
                "_output_tokens": 171,
                "_reasoning_tokens": 0,
                "_token_count_includes_reasoning": False,
            },
        ]

        total = svc.llm_context_manager._history_tokens_cumulative(messages)

        self.assertEqual(total, 16059 + 171)

    def test_history_tokens_cumulative_estimates_anchor_when_output_tokens_missing(self):
        agent = _FakeAgent()
        svc = SessionMemoryService(agent)

        anchor = {
            "role": "assistant",
            "content": "pending approval",
            "_cache_stats": {"input_tokens": 2000},
        }
        messages = [
            {"role": "user", "content": "hello", "_token_count": 10},
            anchor,
            {"role": "user", "content": "next", "_token_count": 33},
        ]
        estimated_anchor = svc.llm_context_manager._estimate_message_tokens("assistant", "pending approval")

        total = svc.llm_context_manager._history_tokens_cumulative(messages)

        self.assertEqual(total, 2000 + estimated_anchor + 33)

    def test_build_regular_task_messages_snapshot_uses_history_only_when_cache_anchor_exists(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 131072}
        agent.conversation_history = [
            {"role": "user", "content": "查看我的Codex用量", "_token_count": 14},
            {
                "role": "assistant",
                "content": '{"tool_calls": [{"id": "a"}]}',
                "_cache_stats": {"input_tokens": 15097},
                "_output_tokens": 135,
                "_reasoning_tokens": 0,
                "_token_count_includes_reasoning": False,
            },
            {
                "role": "user",
                "content": "...",
                "_internal": True,
                "_token_count": 981,
            },
            {
                "role": "assistant",
                "content": '{"tool_calls": [{"id": "b"}]}',
                "_cache_stats": {"input_tokens": 16060},
                "_output_tokens": 253,
                "_reasoning_tokens": 0,
                "_token_count_includes_reasoning": False,
            },
        ]
        svc = SessionMemoryService(agent)

        _messages, _ = svc.build_regular_task_messages("继续")

        self.assertEqual(int(getattr(agent, "_last_context_input_tokens", 0) or 0), 16060 + 253)

    def test_refresh_context_usage_snapshot_includes_system_prompt_for_small_models(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 32000}
        compose_calls = {"n": 0}

        def _compose(include_tools=True):
            _ = include_tools
            compose_calls["n"] += 1
            return "PROMPT-" + ("X" * 40000)

        agent._compose_system_prompt_snapshot = _compose
        svc = SessionMemoryService(agent)

        svc.refresh_context_usage_snapshot(user_input_hint="Continue", context_hint="ctx")

        self.assertEqual(compose_calls["n"], 0)
        # Small models now inject a compact system prompt via
        # _build_small_model_system_prompt, so usage > 1%.
        self.assertGreater(int(getattr(agent, "_last_context_usage_percent", 0) or 0), 1)

    def test_refresh_context_usage_snapshot_skips_when_state_key_mismatch(self):
        agent = _FakeAgent()
        agent.active_chat_id = "chat-1"
        agent._last_context_usage_percent = 9
        agent._last_context_input_tokens = 999
        svc = SessionMemoryService(agent)

        stale_key = svc._context_usage_state_key()
        agent.conversation_history.append({"role": "user", "content": "state changed"})

        svc.refresh_context_usage_snapshot(
            user_input_hint="Continue",
            context_hint="ctx",
            expected_chat_id="chat-1",
            expected_state_key=stale_key,
        )

        self.assertEqual(agent._last_context_usage_percent, 9)
        self.assertEqual(agent._last_context_input_tokens, 999)

    def test_refresh_context_usage_snapshot_without_expected_state_key_does_not_self_drop(self):
        agent = _FakeAgent()
        agent.active_chat_id = "chat-1"
        agent._last_context_usage_percent = 0
        agent._last_context_input_tokens = 0
        svc = SessionMemoryService(agent)
        calls = {"n": 0}

        def _unstable_state_key() -> str:
            calls["n"] += 1
            return f"key-{calls['n']}"

        svc._context_usage_state_key = _unstable_state_key  # type: ignore[assignment]
        svc.refresh_context_usage_snapshot(user_input_hint="Continue", context_hint="ctx")
        self.assertEqual(int(getattr(agent, "_last_context_input_tokens", 0) or 0), 0)

    def test_schedule_context_usage_refresh_async_captures_chat_id_at_schedule_time(self):
        agent = _FakeAgent()
        agent.active_chat_id = "chat-1"
        svc = SessionMemoryService(agent)
        calls = []
        entered = threading.Event()
        release = threading.Event()

        def _slow_refresh(*_args, **kwargs):
            calls.append(dict(kwargs))
            entered.set()
            release.wait(timeout=1.0)

        svc.refresh_context_usage_snapshot = _slow_refresh  # type: ignore[assignment]
        scheduled = svc.schedule_context_usage_refresh_async()
        self.assertTrue(scheduled)
        self.assertTrue(entered.wait(timeout=0.5))
        agent.active_chat_id = "chat-2"
        release.set()
        time.sleep(0.05)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("expected_chat_id"), "chat-1")

    def test_logs_budget_and_keeps_hard_anchors(self):
        agent = _FakeAgent()
        agent.operation_results = [{"command": {"action": "read"}, "result": {"success": True, "message": "ok"}}]
        agent.conversation_history.append({"role": "user", "content": "Initial request: fix the build script"})
        svc = SessionMemoryService(agent)
        with patch("cli.services.session_memory_service.get_logger") as mock_get_logger:
            logger = MagicMock()
            mock_get_logger.return_value = logger
            messages, _ = svc.build_regular_task_messages("Now execute the fix", context="operation-context-" + ("c" * 300))
        self.assertTrue(logger.info.called)
        user_block = messages[-1]["content"]
        self.assertTrue(hasattr(agent, "_last_context_usage_percent"))
        self.assertGreaterEqual(int(getattr(agent, "_last_context_usage_percent", -1)), 0)

    def test_aggressive_compression_triggers_over_80_percent(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 64000}
        agent.token_estimator = lambda s: len(str(s or ""))  # deterministic pressure
        agent.operation_results = [{"command": {"action": "test"}, "result": {"success": True, "output": "x" * 400}}]
        agent.conversation_history.append({"role": "user", "content": "Initial request: complete a complex refactor"})
        for i in range(1, 90):
            role = "assistant" if i % 2 == 0 else "user"
            agent.conversation_history.append(
                {"role": role, "content": f"round-{i} " + ("x" * 1800)}
            )
        svc = SessionMemoryService(agent)
        svc._context_token_budgets = lambda: {
            "profile": "medium",
            "context_window": 64000,
            "input_budget": 100000,
            "system_budget": 30000,
            "history_budget": 58000,
            "op_context_budget": 20000,
            "history_summary_budget": 8000,
            "memory_share_ratio": 45,
            "assistant_clip_tokens": 1800,
        }
        agent.operation_results = []
        _messages, _ = svc.build_regular_task_messages("Continue moving forward", context="ctx-" + ("y" * 1200))
        pre = int(getattr(agent, "_last_context_usage_percent_precompression", 0))
        post = int(getattr(agent, "_last_context_usage_percent", 0))
        self.assertGreater(pre, 80)
        self.assertTrue(bool(getattr(agent, "_last_context_aggressive_compression_applied", False)))
        self.assertLess(post, pre)

    def test_system_prompt_core_not_clipped_under_aggressive_compress(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 64000}
        agent.token_estimator = lambda s: len(str(s or ""))
        for i in range(1, 80):
            role = "assistant" if i % 2 == 0 else "user"
            agent.conversation_history.append({"role": role, "content": f"round-{i} " + ("z" * 1800)})
        svc = SessionMemoryService(agent)
        svc._context_token_budgets = lambda: {
            "profile": "medium",
            "context_window": 64000,
            "input_budget": 64000,
            "system_budget": 30000,
            "history_budget": 42000,
            "op_context_budget": 12000,
            "history_summary_budget": 6000,
            "memory_share_ratio": 45,
            "assistant_clip_tokens": 1800,
        }
        messages, _ = svc.build_regular_task_messages("Continue moving forward", context="ctx-" + ("q" * 1200))
        system_content = str(messages[0].get("content") or "")
        self.assertIn("[SYSTEM_PROMPT_END_MARK]", system_content)
        self.assertIn("Current workspace name: Default", system_content)
        self.assertNotIn(str(agent._self_repo_root), system_content)
        self.assertNotIn(str(agent.work_directory), system_content)
        self.assertIn(f"Current workspace root (absolute path): {agent.workspace_root}", system_content)
        self.assertIn(
            f"Current workspace data directory (absolute path): {agent.workspace_config_dir}",
            system_content,
        )
        self.assertIn(
            f"Default skill install path (absolute path): {(get_app_global_config_dir() / 'skills').resolve()}",
            system_content,
        )
        self.assertIn(
            f"Current workspace skills directory (absolute path): {(agent.workspace_config_dir / 'skills').resolve()}",
            system_content,
        )
        self.assertIn(
            "When installing a third-party skill: if the user does not specify an install location, "
            "you must use the Default skill install path (absolute path); use the Current workspace skills directory "
            "(absolute path) only when the user explicitly asks to install into the workspace.",
            system_content,
        )


if __name__ == "__main__":
    unittest.main()
