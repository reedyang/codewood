import unittest

from cli.managers.chat_state_manager import ChatStateManager, _normalize_reply_records
from cli.services.session_memory_service import _assistant_model_view, _assistant_reply_raw_node


def _manager():
    return ChatStateManager.__new__(ChatStateManager)


class ReplyBlockSerializationTests(unittest.TestCase):
    def test_normalize_reply_records_whitelists_fields(self):
        records = [
            {"kind": "reasoning", "data": "r", "from": "native", "junk": "drop-me"},
            {"kind": "tool_call", "tool_call_id": "c1",
             "data": {"id": "c1", "function": {"name": "ls", "arguments": "{}"}},
             "from": "content_split"},
            {"kind": "raw", "_split_source": True, "content": "<raw>", "junk": "drop"},
        ]
        normalized = _normalize_reply_records(records)
        self.assertEqual(
            [(r.get("kind"), sorted(r.keys())) for r in normalized],
            [
                ("reasoning", ["data", "from", "kind"]),
                ("tool_call", ["data", "from", "kind", "tool_call_id"]),
                ("raw", ["_split_source", "content", "kind"]),
            ],
        )
        self.assertNotIn("junk", normalized[0])
        self.assertNotIn("junk", normalized[2])

    def test_normalize_message_round_trips_block(self):
        records = [
            {"kind": "reasoning", "data": "think", "from": "native"},
            {"kind": "content", "data": "hello", "from": "native"},
            {"kind": "tool_call", "tool_call_id": "c1",
             "data": {"id": "c1", "function": {"name": "read", "arguments": "{}"}}, "from": "native"},
            {"kind": "raw", "_split_source": True, "content": "raw text"},
        ]
        raw_msg = {
            "role": "assistant",
            "content": "",
            "created_at": "2026-01-01 00:00:00",
            "_reply_records": records,
            "_cache_stats": {"input_tokens": 10},
            "_output_tokens": 50,
            "_reasoning_tokens": 5,
            "_token_count_includes_reasoning": True,
            "_model": "p/m",
        }
        out = _manager()._normalize_message(raw_msg)
        self.assertEqual(out["role"], "assistant")
        self.assertIsNotNone(out["_reply_records"])
        self.assertEqual(out["_output_tokens"], 50)
        self.assertEqual(out["_cache_stats"]["input_tokens"], 10)
        raw = _assistant_reply_raw_node(out)
        self.assertEqual(raw["content"], "raw text")
        self.assertTrue(_assistant_reply_raw_node(out)["_split_source"])

    def test_normalize_message_preserves_unsplit_block(self):
        records = [{"kind": "content", "data": "hello", "from": "native"}]
        raw_msg = {"role": "assistant", "content": "", "created_at": "2026-01-01 00:00:00",
                   "_reply_records": records}
        out = _manager()._normalize_message(raw_msg)
        self.assertIsNone(_assistant_reply_raw_node(out))
        view = _assistant_model_view(out)
        self.assertEqual(view["content"], "hello")

    def test_normalize_message_orders_reply_records_before_tool_rounds_raw(self):
        raw_msg = {
            "role": "assistant",
            "content": "",
            "created_at": "2026-01-01 00:00:00",
            "_tool_rounds_raw": [{"tool": "shell", "args": {}, "output": "x"}],
            "_reply_records": [{"kind": "tool_call", "tool_call_id": "c1",
                                "data": {"id": "c1", "function": {"name": "shell", "arguments": "{}"}},
                                "from": "native"}],
        }
        out = _manager()._normalize_message(raw_msg)
        keys = list(out.keys())
        self.assertLess(keys.index("_reply_records"), keys.index("_tool_rounds_raw"))


if __name__ == "__main__":
    unittest.main()
