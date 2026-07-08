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


def _tool_result(tool: str, args: dict, output: str = "") -> str:
    return "[MODEL_TOOL_RESULT]" + json.dumps(
        {
            "kind": "model_tool_result",
            "tool": tool,
            "args": args,
            "success": True,
            "output": output,
            "created_at": "2026-07-08 18:21:43",
        },
        ensure_ascii=False,
    )


class _FakeSessionMemoryService:
    def parse_context_compaction_notice_content(self, content):
        return None

    def parse_context_compaction_summary_content(self, content):
        return None


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
            {
                "role": "assistant",
                "content": _tool_result(
                    "request_skill_prompt",
                    {"skill_id": "codex-usage"},
                    output="skill prompt body",
                ),
                "created_at": "2026-07-08 18:21:42",
            },
            {
                "role": "assistant",
                "content": _tool_plan("shell", {"command": "npx ccusage codex daily --compact"}),
                "created_at": "2026-07-08 18:21:45",
            },
            {
                "role": "assistant",
                "content": _tool_result(
                    "shell",
                    {"command": "npx ccusage codex daily --compact"},
                    output="command output",
                ),
                "created_at": "2026-07-08 18:21:46",
            },
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

    def _parse_model_tool_result_history_content(self, content):
        text = str(content or "")
        prefix = "[MODEL_TOOL_RESULT]"
        if not text.startswith(prefix):
            return None
        try:
            return json.loads(text[len(prefix) :])
        except Exception:
            return None

    def _parse_internal_slash_result_history_content(self, content):
        return None

    def _parse_request_user_input_answer_history_content(self, content):
        return None

    def _render_transcript_single_message(self, idx, msg, hist):
        plan = self._parse_model_tool_plan_history_content(msg.get("content"))
        if plan is not None:
            tool = str(plan.get("tool") or "")
            if tool == "request_skill_prompt":
                print("• Request skill prompt (skill_id=codex-usage)")
            return
        result = self._parse_model_tool_result_history_content(msg.get("content"))
        if result is not None:
            tool = str(result.get("tool") or "")
            if tool == "request_skill_prompt":
                print("• Request skill prompt (skill_id=codex-usage)")
            elif tool == "shell":
                print("• Ran npx ccusage codex daily --compact")
            return
        content = str(msg.get("content") or "").strip()
        if content:
            print(content)


class StructuredTurnGroupingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
