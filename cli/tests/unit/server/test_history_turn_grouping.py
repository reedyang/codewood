import json
import json
import unittest

from cli.server.serve_app import _build_structured_turns


def _tool_plan(tool: str, args: dict) -> str:
    return json.dumps(
        {
            "tool_calls": [
                {
                    "type": "function",
                    "function": {
                        "name": tool,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }
            ]
        },
        ensure_ascii=False,
    )


def _tool_result(tool: str, args: dict, output: str = "") -> dict:
    content = json.dumps({
        "kind": "model_tool_result",
        "tool": tool,
        "args": args,
        "success": True,
        "output": output,
        "created_at": "2026-07-08 18:21:43",
    }, ensure_ascii=False)
    return {
        "role": "tool",
        "name": tool,
        "content": content,
        "tool_call_id": "call_0",
        "created_at": "2026-07-08 18:21:43",
    }


class _FakeSessionMemoryService:
    def parse_context_compaction_summary_content(self, content):
        return None

    def format_context_compaction_title(self, payload_or_content):
        _ = payload_or_content
        return "Context compacted"


class _FakeCompactionSessionMemoryService(_FakeSessionMemoryService):
    def parse_context_compaction_summary_content(self, content):
        if content == "SUMMARY":
            return {"summary": "Compacted summary body", "mode": "manual"}
        return None

    def build_context_compaction_display_payload(self, summary_payload_or_content):
        title = "Context compacted"
        body = ""
        if isinstance(summary_payload_or_content, dict):
            body = str(summary_payload_or_content.get("summary") or "")
        return {
            "title": title,
            "body": body,
            "text": title if not body else f"{title}\n\n{body}",
        }


class _FakeAgent:
    def __init__(self) -> None:
        self.conversation_history = [
            {
                "role": "user",
                "content": "查看我的Codex用量",
                "created_at": "2026-07-08 18:21:31",
            },
            {
                "role": "assistant",
                "content": _tool_plan("request_skill_prompt", {"skill_id": "codex-usage"}),
                "created_at": "2026-07-08 18:21:41",
            },
            _tool_result(
                "request_skill_prompt",
                {"skill_id": "codex-usage"},
                output="skill prompt body",
            ),
            {
                "role": "assistant",
                "content": _tool_plan("shell", {"command": "npx ccusage codex daily --compact"}),
                "created_at": "2026-07-08 18:21:45",
            },
            _tool_result(
                "shell",
                {"command": "npx ccusage codex daily --compact"},
                output="command output",
            ),
            {
                "role": "assistant",
                "content": "最终答案",
                "created_at": "2026-07-08 18:21:55",
            },
        ]
        self.session_memory_service = _FakeSessionMemoryService()

    def _parse_conversation_interrupted_history_content(self, content):
        return None

    def _parse_direct_shell_result_history_content(self, content):
        return None

    def _parse_task_worked_summary_history_content(self, content):
        return None

    def _parse_model_tool_plan_history_content(self, content):
        text = str(content or "").strip()
        if not text.startswith("{"):
            return None
        try:
            payload = json.loads(text)
        except Exception:
            return None
        calls = payload.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            return None
        first = calls[0]
        if not isinstance(first, dict):
            return None
        function = first.get("function") or {}
        if not isinstance(function, dict):
            return None
        tool = str(function.get("name") or "").strip()
        args_text = str(function.get("arguments") or "{}")
        try:
            args = json.loads(args_text)
        except Exception:
            args = {}
        return {"tool": tool, "args": args}

    def _parse_internal_slash_result_history_content(self, content):
        return None

    def _parse_request_user_input_answer_history_content(self, content):
        return None

    def _render_transcript_single_message(self, idx, msg, hist):
        if str(msg.get("role") or "").strip().lower() == "tool":
            content = str(msg.get("content") or "")
            try:
                payload = json.loads(content)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                tool = str(payload.get("tool") or "")
                if tool == "request_skill_prompt":
                    print("• Request skill prompt (skill_id=codex-usage)")
                elif tool == "shell":
                    print("• Ran npx ccusage codex daily --compact")
            return
        plan = self._parse_model_tool_plan_history_content(msg.get("content"))
        if plan is not None:
            return
        content = str(msg.get("content") or "").strip()
        if content:
            print(content)


class _FakeAgentWithReadRender(_FakeAgent):
    """A fake agent whose per-message renderer actually emits the "Ran read"
    feedback line (like the real renderer), so a duplicate would be visible."""

    def _rerender_tool_rounds(self, raw_list):
        out = []
        for item in raw_list:
            tool = str(item.get("tool") or "").strip().lower()
            args = item.get("args") or {}
            line = f"• Ran {tool} {args.get('path', '')}"
            payload = item.get("output")
            if tool == "read" and payload:
                line = f"{line}\uE000{payload}\uE001"
            out.append(line)
        return out

    def _render_transcript_single_message(self, idx, msg, hist):
        if str(msg.get("role") or "").strip().lower() == "tool":
            return
        plan = self._parse_model_tool_plan_history_content(msg.get("content"))
        if plan is not None:
            tool = str(plan.get("tool") or "")
            if tool == "read":
                print(f"• Ran read {plan.get('args', {}).get('path', '')}")
            return
        content = str(msg.get("content") or "").strip()
        if content:
            print(content)


class StructuredTurnGroupingTests(unittest.TestCase):
    def test_falls_back_to_raw_answer_when_clean_content_is_backslash_fragment(self):
        agent = _FakeAgent()
        agent.conversation_history = [
            {
                "role": "user",
                "content": "查看我的codex用量",
                "created_at": "2026-07-29 00:29:46",
            },
            {
                "role": "assistant",
                "content": "逍遥哥哥，您的 Codex 总用量约为 16.87 亿 Tokens。",
                "_clean_content": "\\",
                "created_at": "2026-07-29 00:30:03",
            },
        ]

        turns = _build_structured_turns(agent)

        self.assertEqual(len(turns), 1)
        self.assertEqual(len(turns[0]["rounds"]), 1)
        self.assertEqual(
            turns[0]["rounds"][0]["text"],
            "逍遥哥哥，您的 Codex 总用量约为 16.87 亿 Tokens。",
        )

    def test_no_duplicate_render_when_tool_rounds_raw_present(self):
        # Regression: an assistant message carrying both a recognized tool plan
        # and a pre-rendered ``_tool_rounds_raw`` must render the call ONCE.
        # Before the fix, ``_render_step`` emitted the bare "Ran read" prompt
        # AND ``_rerender_tool_rounds`` emitted the full call with payload,
        # producing two blocks (the first without syntax highlighting).
        agent = _FakeAgentWithReadRender()
        read_payload = "1: def main():\n2:     print('hello world')\n"
        agent.conversation_history = [
            {
                "role": "user",
                "content": "看一下 helloworld.py",
                "created_at": "2026-07-15 15:02:07",
            },
            {
                "role": "assistant",
                "content": _tool_plan("read", {"path": "helloworld.py"}),
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": json.dumps({"path": "helloworld.py"}),
                        },
                    }
                ],
                "_tool_rounds_raw": [
                    {
                        "tool": "read",
                        "args": {"path": "helloworld.py"},
                        "failed": False,
                        "output": read_payload,
                    }
                ],
                "created_at": "2026-07-15 15:02:25",
            },
            {
                "role": "assistant",
                "content": "这是 helloworld.py 的内容",
                "created_at": "2026-07-15 15:02:27",
            },
        ]

        turns = _build_structured_turns(agent)

        self.assertEqual(len(turns), 1)
        turn = turns[0]
        tool_rounds = [r for r in turn["rounds"] if r["tools"].strip()]
        self.assertEqual(len(tool_rounds), 1)
        tools = tool_rounds[0]["tools"]
        # The read call appears exactly once, and with its payload so the GUI
        # can syntax-highlight it.
        self.assertEqual(tools.count("Ran read helloworld.py"), 1)
        self.assertIn("def main()", tools)

    def test_groups_consecutive_tool_calls_and_deduplicates_skill_prompt(self):
        turns = _build_structured_turns(_FakeAgent())

        self.assertEqual(len(turns), 1)
        turn = turns[0]
        self.assertEqual(turn["userText"], "查看我的Codex用量")
        self.assertEqual(len(turn["rounds"]), 2)

        first_round = turn["rounds"][0]
        self.assertEqual(first_round["tools"].count("Request skill prompt"), 1)
        self.assertEqual(first_round["tools"].count("Ran npx ccusage codex daily --compact"), 1)
        self.assertNotIn("\n\n", first_round["tools"])

        second_round = turn["rounds"][1]
        self.assertEqual(second_round["text"], "最终答案")

    def test_emits_compaction_turn_from_summary_without_notice(self):
        agent = _FakeAgent()
        agent.session_memory_service = _FakeCompactionSessionMemoryService()
        agent.conversation_history = [
            {
                "role": "user",
                "content": "old question",
                "created_at": "2026-07-08 18:21:31",
            },
            {
                "role": "assistant",
                "content": "SUMMARY",
                "created_at": "2026-07-08 18:21:41",
            },
        ]

        turns = _build_structured_turns(agent)

        self.assertEqual(len(turns), 2)
        compact_round = turns[-1]["rounds"][0]
        self.assertEqual(turns[-1]["userText"], "")
        self.assertEqual(compact_round["compactNoticeTitle"], "Context compacted")
        self.assertEqual(compact_round["compactNoticeBody"], "Compacted summary body")


if __name__ == "__main__":
    unittest.main()
