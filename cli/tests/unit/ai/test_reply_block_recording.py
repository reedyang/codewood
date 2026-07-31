import unittest

from cli.ai.ai_orchestrator import (
    _build_reply_records,
    _coalesce_reply_events,
    _synthesize_reply_nodes,
)
from cli.runtime.llm_context_manager import LLMContextManager
from cli.runtime.runtime_loop import _rebuild_block_tool_call_nodes
from cli.services.session_memory_service import (
    _assistant_display_view,
    _assistant_model_view,
    _assistant_reply_nodes,
    _assistant_reply_raw_node,
    _assistant_tool_calls,
    _is_reply_block,
)


def _block(records, **meta):
    return {"role": "assistant", "content": "", "_reply_records": records, **meta}


class ReplyBlockRecordingTests(unittest.TestCase):
    def test_unsplit_events_keep_natural_order_no_raw(self):
        events = [
            ("reasoning", "think", "native"),
            ("content", "Hello", "native"),
            ("content", " world", "native"),
            ("tool_call", {"id": "c1", "function": {"name": "read", "arguments": "{}"}}, "native"),
        ]
        records = _build_reply_records({}, events, None)
        self.assertEqual(
            [(r.get("kind"), r.get("from")) for r in records],
            [("reasoning", "native"), ("content", "native"), ("tool_call", "native")],
        )
        self.assertEqual(records[1]["data"], "Hello world")
        self.assertIsNone(_assistant_reply_raw_node(_block(records)))
        self.assertTrue(all(r.get("from") == "native" for r in records))

    def test_split_events_append_raw_and_mark_nodes(self):
        events = [
            ("content", "Answer text", "native"),
            ("thinking", "hidden reasoning", "content_split"),
            ("content", " more", "native"),
        ]
        msg = {
            "content": "Answer text<|channel>thought\nhidden reasoning<channel|> more",
            "_clean_content": "Answer text more",
            "_thinking": "hidden reasoning",
            "_thinking_from_content": True,
        }
        records = _build_reply_records(msg, events, None)
        raw = _assistant_reply_raw_node(_block(records))
        self.assertIsNotNone(raw)
        self.assertEqual(raw["content"], msg["content"])
        # All nodes here are split out of the raw (content + extracted
        # thinking), so the raw comes FIRST and they follow it.
        self.assertEqual(records[0]["kind"], "raw")
        for r in records:
            if r.get("kind") == "raw":
                continue
            self.assertEqual(r.get("from"), "content_split", r)

    def test_model_view_uses_raw_for_split_keeps_native_only(self):
        events = [
            ("reasoning", "native think", "native"),
            ("content", "Answer text", "native"),
            ("thinking", "hidden reasoning", "content_split"),
            ("content", " more", "native"),
        ]
        msg = {
            "content": "Answer text<|channel>thought\nhidden reasoning<channel|> more",
            "_clean_content": "Answer text more",
        }
        records = _build_reply_records(msg, events, None)
        block = _block(records, _output_tokens=100, _reasoning_tokens=10)
        view = _assistant_model_view(block)
        self.assertEqual(view["content"], msg["content"])
        self.assertNotIn("_clean_content", view)
        self.assertEqual(view["_thinking"], "native think")
        self.assertNotIn("tool_calls", view)
        self.assertEqual(view["_output_tokens"], 100)

    def test_empty_thinking_block_content_node_is_content_split(self):
        """A content node cleaned from an empty <|channel>thought<channel|>
        block is split out of the raw text and marked from: content_split."""
        msg = {
            "content": "<|channel>thought\n<channel|>逍遥哥哥，已经帮你修改好。",
            "_clean_content": "逍遥哥哥，已经帮你修改好。",
        }
        records = _build_reply_records(msg, None, None)
        self.assertIsNotNone(_assistant_reply_raw_node(_block(records)))
        content_node = next(r for r in records if r.get("kind") == "content")
        self.assertEqual(content_node.get("from"), "content_split")
        raw = _assistant_reply_raw_node(_block(records))
        self.assertIn("<|channel>", raw["content"])

    def test_native_tool_call_in_split_reply_is_not_content_split(self):
        """A native tool_call node is NOT split out of the raw content text,
        so it keeps ``from: "native"`` even in a split reply."""
        events = [
            ("thinking", "hidden reasoning", "content_split"),
            ("content", "Answer text", "native"),
            ("tool_call", {"id": "chatcmpl-tool-abc", "function": {"name": "shell", "arguments": "{}"}}, "native"),
        ]
        msg = {
            "content": "<|channel>thought\nhidden reasoning<channel|>Answer text",
            "_clean_content": "Answer text",
            "_thinking": "hidden reasoning",
            "_thinking_from_content": True,
            "tool_calls": [{"id": "chatcmpl-tool-abc", "function": {"name": "shell", "arguments": "{}"}}],
        }
        records = _build_reply_records(msg, events, msg.get("tool_calls"))
        raw = _assistant_reply_raw_node(_block(records))
        self.assertIsNotNone(raw)  # split reply keeps the raw node
        for r in records:
            expect_split = r.get("from") == "content_split"
            self.assertEqual(r.get("from") == "content_split", expect_split, r)
        tool_node = next(r for r in records if r.get("kind") == "tool_call")
        self.assertEqual(tool_node.get("from"), "native")
        # The native tool call still reaches the model context for cache fidelity.
        block = _block(records)
        view = _assistant_model_view(block)
        self.assertEqual(view["tool_calls"][0]["id"], "chatcmpl-tool-abc")
        self.assertEqual(view["content"], msg["content"])

    def test_display_view_cleans_content_and_covers_all_reasoning(self):
        events = [
            ("content", "Answer text", "native"),
            ("thinking", "hidden reasoning", "content_split"),
            ("content", " more", "native"),
        ]
        msg = {
            "content": "Answer text<|channel>thought\nhidden reasoning<channel|> more",
            "_clean_content": "Answer text more",
        }
        records = _build_reply_records(msg, events, None)
        block = _block(records)
        view = _assistant_display_view(block)
        self.assertEqual(view["content"], "Answer text more")
        self.assertEqual(view["_thinking"], "hidden reasoning")

    def test_synthesize_native_thinking_is_unsplit(self):
        msg = {"content": "plain answer", "_thinking": "native reasoning"}
        records = _build_reply_records(msg, None, None)
        self.assertEqual([(r.get("kind"), r.get("from")) for r in records],
                         [("reasoning", "native"), ("content", "native")])
        self.assertIsNone(_assistant_reply_raw_node(_block(records)))

    def test_synthesize_thinking_from_content_is_split(self):
        msg = {
            "content": "a<think>x</think>b",
            "_clean_content": "ab",
            "_thinking": "x",
            "_thinking_from_content": True,
        }
        records = _build_reply_records(msg, None, None)
        self.assertTrue(_assistant_reply_raw_node(_block(records)) is not None)
        kinds = [(r.get("kind"), r.get("from")) for r in records]
        # both pieces are split out of the raw -> raw first, pieces follow
        self.assertEqual(kinds[0], ("raw", None))
        self.assertEqual(kinds[1], ("reasoning", "content_split"))
        self.assertEqual(kinds[2], ("content", "content_split"))

    def test_tool_only_reply_no_raw_no_content(self):
        msg = {"content": "", "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "shell", "arguments": "{}"}}]}
        records = _build_reply_records(msg, None, None)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "tool_call")
        block = _block(records)
        view = _assistant_display_view(block)
        # Display synthesizes the plan payload so existing renderers work.
        self.assertIn("tool_calls", view["content"])
        self.assertEqual(view["tool_calls"][0]["id"], "t1")

    def test_coalesce_merges_consecutive_same_kind(self):
        events = [
            ("content", "a", "native"),
            ("content", "b", "native"),
            ("thinking", "r1", "content_split"),
            ("thinking", "r2", "content_split"),
            ("content", "c", "native"),
        ]
        nodes = _coalesce_reply_events(events)
        self.assertEqual(
            [(n.get("kind"), n.get("from"), n.get("data")) for n in nodes],
            [
                ("content", "native", "ab"),
                ("reasoning", "content_split", "r1r2"),
                ("content", "native", "c"),
            ],
        )

    def test_dedup_replaces_tool_call_nodes(self):
        call_a = {"id": "a", "function": {"name": "read", "arguments": "{}"}}
        call_b = {"id": "b", "function": {"name": "read", "arguments": "{}"}}
        events = [("tool_call", call_a, "native"), ("tool_call", call_b, "native")]
        deduped = [call_a]
        records = _build_reply_records({}, events, deduped)
        tool_nodes = [r for r in records if r.get("kind") == "tool_call"]
        self.assertEqual(len(tool_nodes), 1)
        self.assertEqual(tool_nodes[0]["data"]["id"], "a")

    def test_compatible_pseudo_tool_calls_become_content_split_nodes(self):
        msg = {
            "content": "Let me check the files.\n<tool_calls>\n{\"tool\":\"shell\",\"args\":{\"cmd\":\"ls\"}}\n</tool_calls>\nDone.",
        }
        records = _build_reply_records(msg, None, None)
        tool_nodes = [r for r in records if r.get("kind") == "tool_call"]
        self.assertEqual(len(tool_nodes), 1)
        self.assertEqual(tool_nodes[0]["from"], "content_split")
        self.assertEqual(tool_nodes[0]["data"]["function"]["name"], "shell")
        # pseudo calls embedded in content force a split (raw preserved)
        raw = _assistant_reply_raw_node(_block(records))
        self.assertIsNotNone(raw)
        self.assertIn("tool_calls", raw["content"])

    def test_incompatible_pseudo_tool_calls_discarded(self):
        msg = {
            "content": "Here is the plan: <tool_calls>\nnot valid json at all\n</tool_calls>",
        }
        records = _build_reply_records(msg, None, None)
        tool_nodes = [r for r in records if r.get("kind") == "tool_call"]
        self.assertEqual(tool_nodes, [])

    def test_rebuild_block_tool_call_nodes_preserves_ids_never_sets_outer(self):
        block = _block([
            {"kind": "tool_call", "tool_call_id": "chatcmpl-tool-abc",
             "data": {"id": "chatcmpl-tool-abc", "type": "function",
                      "function": {"name": "request_skill_prompt", "arguments": "{\"skill_id\": \"x\"}"}},
             "from": "native"},
        ])
        changed = _rebuild_block_tool_call_nodes(block, [("request_skill_prompt", {"skill_id": "x"})])
        self.assertFalse(changed)
        self.assertNotIn("tool_calls", block)
        node = _assistant_reply_nodes(block)[0]
        self.assertEqual(node["tool_call_id"], "chatcmpl-tool-abc")

    def test_rebuild_block_tool_call_nodes_appends_when_missing(self):
        block = _block([{"kind": "content", "data": "text", "from": "native"}])
        changed = _rebuild_block_tool_call_nodes(block, [("shell", {"cmd": "ls"})])
        self.assertTrue(changed)
        self.assertNotIn("tool_calls", block)
        nodes = _assistant_reply_nodes(block)
        tool_nodes = [n for n in nodes if n.get("kind") == "tool_call"]
        self.assertEqual(len(tool_nodes), 1)
        self.assertEqual(tool_nodes[0]["data"]["function"]["name"], "shell")
        self.assertEqual(tool_nodes[0]["tool_call_id"], "call_0")

    def test_rebuild_block_tool_call_nodes_repairs_invalid_arguments(self):
        block = _block([
            {"kind": "tool_call", "tool_call_id": "chatcmpl-tool-abc",
             "data": {"id": "chatcmpl-tool-abc", "type": "function",
                      "function": {"name": "shell", "arguments": "{not valid json"}},
             "from": "native"},
        ])
        changed = _rebuild_block_tool_call_nodes(block, [("shell", {"cmd": "ls"})])
        self.assertTrue(changed)
        node = _assistant_reply_nodes(block)[0]
        self.assertEqual(node["tool_call_id"], "chatcmpl-tool-abc")
        self.assertEqual(node["data"]["function"]["name"], "shell")

    def test_assistant_tool_calls_helpers(self):
        msg = {"content": "", "tool_calls": [{"id": "x", "function": {"name": "ls", "arguments": "{}"}}]}
        records = _build_reply_records(msg, None, None)
        block = _block(records)
        self.assertTrue(_is_reply_block(block))
        self.assertEqual(_assistant_tool_calls(block)[0]["id"], "x")
        self.assertEqual(_assistant_tool_calls({"role": "assistant", "tool_calls": [{"id": "y"}]})[0]["id"], "y")
        self.assertEqual(_assistant_tool_calls({"role": "assistant"}), [])

    def _build(self, records, history, api_mode="chat"):
        agent = _StubAgent()
        agent.params = {"api_mode": api_mode}
        manager = LLMContextManager(agent, _StubMemory(), None)
        return manager._build_history_messages_by_budget(10000, 80, 180, source_history=history)

    def test_build_history_chat_merged_replay(self):
        """Chat Completions (default / api_mode=chat): a split block replays as
        ONE flattened assistant message (reasoning_content + raw content +
        tool_calls), reproducing the original message boundary for cache hits."""
        events = [
            ("reasoning", "native think", "native"),
            ("content", "Answer text", "native"),
            ("thinking", "hidden", "content_split"),
            ("tool_call", {"id": "c1", "function": {"name": "read", "arguments": "{}"}}, "native"),
        ]
        msg = {
            "content": "Answer text<think>hidden</think>",
            "_clean_content": "Answer text",
            "_thinking": "hidden",
            "_thinking_from_content": True,
            "tool_calls": [{"id": "c1", "function": {"name": "read", "arguments": "{}"}}],
        }
        records = _build_reply_records(msg, events, msg.get("tool_calls"))
        history = [
            {"role": "user", "content": "q"},
            _block(records, _model="p/m"),
            {"role": "tool", "tool_call_id": "c1", "name": "read", "content": "{}"},
        ]
        built, _stats = self._build(records, history, api_mode="chat")
        assistants = [m for m in built if m.get("role") == "assistant"]
        self.assertEqual(len(assistants), 1)
        # Cache fidelity: single message, content = raw uncleaned text.
        self.assertEqual(assistants[0]["content"], "Answer text<think>hidden</think>")
        self.assertEqual(assistants[0].get("_thinking"), "native think")
        self.assertEqual(assistants[0]["tool_calls"][0]["id"], "c1")

    def test_build_history_responses_interleaved_replay(self):
        """Responses API preserves multi-segment reasoning: the same split block
        replays as ordered [reasoning+raw, tool_call] messages, not merged."""
        events = [
            ("reasoning", "native think", "native"),
            ("content", "Answer text", "native"),
            ("thinking", "hidden", "content_split"),
            ("tool_call", {"id": "c1", "function": {"name": "read", "arguments": "{}"}}, "native"),
        ]
        msg = {
            "content": "Answer text<think>hidden</think>",
            "_clean_content": "Answer text",
            "_thinking": "hidden",
            "_thinking_from_content": True,
            "tool_calls": [{"id": "c1", "function": {"name": "read", "arguments": "{}"}}],
        }
        records = _build_reply_records(msg, events, msg.get("tool_calls"))
        history = [
            {"role": "user", "content": "q"},
            _block(records, _model="p/m"),
            {"role": "tool", "tool_call_id": "c1", "name": "read", "content": "{}"},
        ]
        built, _stats = self._build(records, history, api_mode="responses")
        assistants = [m for m in built if m.get("role") == "assistant"]
        # Interleaved order preserved: [reasoning(raw content), tool_call].
        self.assertEqual(len(assistants), 2)
        self.assertEqual(assistants[0]["content"], "Answer text<think>hidden</think>")
        self.assertEqual(assistants[0].get("_thinking"), "native think")
        self.assertEqual(assistants[1]["content"], "")
        self.assertEqual(assistants[1]["tool_calls"][0]["id"], "c1")

    def test_build_history_unsplit_chat_merges_into_one_message(self):
        events = [
            ("reasoning", "think", "native"),
            ("content", "hello", "native"),
            ("tool_call", {"id": "t", "function": {"name": "ls", "arguments": "{}"}}, "native"),
        ]
        records = _build_reply_records({}, events, None)
        history = [
            {"role": "user", "content": "q"},
            _block(records),
            {"role": "tool", "tool_call_id": "t", "name": "ls", "content": "{}"},
        ]
        built, _stats = self._build(records, history, api_mode="chat")
        assistants = [m for m in built if m.get("role") == "assistant"]
        self.assertEqual(len(assistants), 1)
        self.assertEqual(assistants[0]["content"], "hello")
        self.assertEqual(assistants[0].get("_thinking"), "think")
        self.assertEqual(assistants[0]["tool_calls"][0]["id"], "t")

    def test_build_history_unsplit_responses_preserves_order(self):
        events = [
            ("reasoning", "think", "native"),
            ("content", "hello", "native"),
            ("tool_call", {"id": "t", "function": {"name": "ls", "arguments": "{}"}}, "native"),
        ]
        records = _build_reply_records({}, events, None)
        history = [
            {"role": "user", "content": "q"},
            _block(records),
            {"role": "tool", "tool_call_id": "t", "name": "ls", "content": "{}"},
        ]
        built, _stats = self._build(records, history, api_mode="responses")
        assistants = [m for m in built if m.get("role") == "assistant"]
        # Unsplit block: [reasoning+content, tool_call] in recorded order.
        self.assertEqual(len(assistants), 2)
        self.assertEqual(assistants[0]["content"], "hello")
        self.assertEqual(assistants[0].get("_thinking"), "think")
        self.assertEqual(assistants[1]["content"], "")
        self.assertEqual(assistants[1]["tool_calls"][0]["id"], "t")


class _StubAgent:
    params: dict = {}

    def _parse_internal_slash_result_history_content(self, content):
        return None

    def _parse_task_worked_summary_history_content(self, content):
        return None


class _StubMemory:
    def _normalize_history_content_for_model(self, role, content, message=None):
        if role == "user" and isinstance(message, dict) and message.get("_memory_context"):
            return str(message["_memory_context"]) + "\n" + str(content or "")
        return str(content or "")

    def _clip_text_to_token_budget(self, text, max_tokens):
        text = str(text or "")
        if max_tokens <= 0:
            return ""
        return text[: max(max_tokens, 16)]

    def _estimate_message_tokens(self, role, content):
        return max(1, len(str(content or "")))

    def _estimate_text_tokens(self, text):
        return max(1, len(str(text or "")))

    def _is_excluded_user_message_for_model_context(self, msg):
        return False

    def _is_builtin_slash_user_message(self, role, content):
        return False

    def _summarize_history_excerpt(self, rows, budget):
        return ""


if __name__ == "__main__":
    unittest.main()
