import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cli.runtime.llm_context_manager import LLMContextManager


class _StubChatState:
    def __init__(self, data_dir: Path):
        self._data_dir = data_dir

    def chat_data_dir_for_chat(self, chat_id: str) -> Path:
        return self._data_dir


class _StubAgent:
    params: dict = {}
    active_chat_id = "chat_cache_test"

    def __init__(self, data_dir: Path):
        self._chat_state_manager = _StubChatState(data_dir)

    def _parse_internal_slash_result_history_content(self, content):
        return None

    def _parse_task_worked_summary_history_content(self, content):
        return None


class _StubMemory:
    def __init__(self, agent):
        self._agent = agent

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

    def _is_model_call_error_history_message(self, msg):
        return False

    def _is_conversation_interrupted_history_message(self, msg):
        return False

    def is_context_compaction_summary_message(self, msg):
        return False

    def _context_eligible_history(self):
        out = []
        for item in getattr(self._agent, "conversation_history", None) or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            if role not in ("user", "assistant", "tool"):
                continue
            if role == "user" and self._is_excluded_user_message_for_model_context(item):
                continue
            if self._is_builtin_slash_user_message(role, str(item.get("content") or "")):
                continue
            if self._is_model_call_error_history_message(item):
                continue
            if self._is_conversation_interrupted_history_message(item):
                continue
            out.append(item)
        return out

    def history_for_regular_context(self):
        rows = self._context_eligible_history()
        for idx in range(len(rows) - 1, -1, -1):
            if self.is_context_compaction_summary_message(rows[idx]):
                return rows[idx:]
        return rows

    def _context_token_budgets(self):
        return {"assistant_clip_tokens": 180}

    def _summarize_history_excerpt(self, rows, budget):
        return ""


def _history(messages):
    return [dict(m) for m in messages]


class ContextHistoryCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name) / "chats" / "data" / "chat_cache_test"
        self.agent = _StubAgent(self.data_dir)
        self.agent.conversation_history = []
        self.manager = LLMContextManager(self.agent, _StubMemory(self.agent), None)
        self.cache_path = self.data_dir / "context_history.json"

    def _build(self, history, **kwargs):
        return self.manager._build_history_messages_by_budget(
            kwargs.get("history_budget", 10000),
            kwargs.get("summary_budget", 80),
            kwargs.get("assistant_clip_tokens", 180),
            source_history=history,
        )

    def _cache_rows(self):
        with open(self.cache_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("rows") or []

    def test_first_build_writes_cache_and_second_reuses(self):
        history = _history(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ]
        )
        built1, _ = self._build(history)
        self.assertEqual(
            [m["content"] for m in built1], ["hello", "hi there"]
        )
        self.assertTrue(self.cache_path.is_file())
        self.assertEqual(len(self._cache_rows()), 2)

        # Second build with identical history must reuse the cache entirely:
        # no message re-assembly is allowed.
        with mock.patch.object(
            LLMContextManager,
            "_assemble_history_entry",
            side_effect=AssertionError("should be served from cache"),
        ):
            built2, _ = self._build(history)
        self.assertEqual(built1, built2)

    def test_append_only_reassembles_tail_keeps_prefix(self):
        history1 = _history(
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
            ]
        )
        built1, _ = self._build(history1)

        history2 = _history(
            history1
            + [
                {"role": "user", "content": "q2"},
                {"role": "assistant", "content": "a2"},
            ]
        )
        calls = []
        _orig_entry = LLMContextManager._assemble_history_entry

        def _counting_entry(*args, **kwargs):
            calls.append(args[1].get("content"))
            return _orig_entry(self.manager, *args, **kwargs)

        with mock.patch.object(
            LLMContextManager, "_assemble_history_entry", side_effect=_counting_entry
        ):
            built2, _ = self._build(history2)
        # Only the two new messages are re-assembled.
        self.assertEqual(calls, ["q2", "a2"])
        # The original prefix is byte-identical.
        self.assertEqual(built2[:2], built1)
        self.assertEqual([m["content"] for m in built2][2:], ["q2", "a2"])

    def test_edit_tail_invalidates_and_reassembles(self):
        history = _history(
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
                {"role": "assistant", "content": "a2"},
            ]
        )
        self._build(history)

        # Editing the tail message breaks the single boundary signature, so the
        # whole history is re-assembled and re-cached. (Edits always truncate +
        # clear the cache in the command path; here the change is exercised
        # directly against a still-present cache to prove tail_sig invalidation.)
        edited = _history(history)
        edited[3]["content"] = "a2-edited"
        calls = []
        _orig_entry = LLMContextManager._assemble_history_entry

        def _counting_entry(*args, **kwargs):
            calls.append(args[1].get("content"))
            return _orig_entry(self.manager, *args, **kwargs)

        with mock.patch.object(
            LLMContextManager, "_assemble_history_entry", side_effect=_counting_entry
        ):
            built, _ = self._build(edited)
        self.assertEqual(calls, ["q1", "a1", "q2", "a2-edited"])
        self.assertEqual(
            [m["content"] for m in built], ["q1", "a1", "q2", "a2-edited"]
        )

        # The rebuilt cache reflects the edited history.
        rows = self._cache_rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            rows[3]["msg"][0]["content"], "a2-edited"
        )

    def test_edit_mid_history_rebuilds_tail_in_file(self):
        history = _history(
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
            ]
        )
        self._build(history)

        # Rewind to the very first message (equivalent to an edit that drops
        # everything after q1) and clear the cache like the /chat edit command.
        from cli.runtime.context_history_cache import clear_history_cache

        self.assertTrue(self.cache_path.is_file())
        clear_history_cache(self.agent)
        self.assertFalse(self.cache_path.is_file())

        truncated = _history(history[:1])
        built, _ = self._build(truncated)
        self.assertEqual([m["content"] for m in built], ["q1"])
        self.assertTrue(self.cache_path.is_file())
        self.assertEqual(len(self._cache_rows()), 1)

    def test_clip_budget_change_invalidates_cache(self):
        history = _history(
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a" * 500},
            ]
        )
        self._build(history, assistant_clip_tokens=180)
        cached_rows = self._cache_rows()
        self.assertTrue(cached_rows)
        self.assertEqual(len(cached_rows[1]["msg"][0]["content"]), 180)

        # A different assembly parameter must not reuse stale entries.
        built2, _ = self._build(history, assistant_clip_tokens=90)
        self.assertEqual(built2[1]["role"], "assistant")
        self.assertEqual(len(built2[1]["content"]), 90)
        self.assertEqual(len(self._cache_rows()), 2)

    def test_cache_holds_clean_send_format_no_bookkeeping(self):
        history = _history(
            [
                {"role": "user", "content": "q1"},
                {
                    "role": "assistant",
                    "content": "a1",
                    "_model": "HappyCoding-responses/Gemma-4-31B-IT",
                    "_cache_stats": {"prompt_cache_hit_tokens": 13824, "prompt_cache_miss_tokens": 1166},
                    "_token_count": 19,
                },
            ]
        )
        built, _ = self._build(history)
        self.assertEqual(built[1]["content"], "a1")
        for banned in ("_model", "_cache_stats", "_token_count", "_thinking"):
            self.assertNotIn(banned, built[1])

        rows = self._cache_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["msg"], [{"role": "assistant", "content": "a1"}])

    def test_to_send_message_cleanup(self):
        from cli.runtime.context_history_cache import to_send_message

        native = {
            "role": "assistant",
            "content": "hi",
            "_thinking": "reasoning",
            "_thinking_from_content": False,
            "_model": "m/x",
            "_cache_stats": {},
            "_token_count": 5,
        }
        self.assertEqual(
            to_send_message(native),
            {"role": "assistant", "content": "hi", "reasoning_content": "reasoning"},
        )
        content_derived = {
            "role": "assistant",
            "content": "<think>h</think>hi",
            "_thinking": "h",
            "_thinking_from_content": True,
            "_model": "m/x",
        }
        self.assertEqual(
            to_send_message(content_derived),
            {"role": "assistant", "content": "<think>h</think>hi"},
        )
        tool = {"role": "tool", "content": "{}", "tool_call_id": "c1", "name": "read", "_token_count": 1}
        self.assertEqual(
            to_send_message(tool),
            {"role": "tool", "content": "{}", "tool_call_id": "c1", "name": "read"},
        )

    def test_api_kind_switch_invalidates_cache(self):
        history = _history(
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
            ]
        )
        self.agent.params = {"api_mode": "chat"}
        built1, _ = self._build(history)
        self.assertTrue(self._cache_rows())

        # Different API kind -> replay mode changes -> cache must regenerate.
        self.agent.params = {"api_mode": "responses"}
        calls = []
        _orig_entry = LLMContextManager._assemble_history_entry

        def _counting_entry(*args, **kwargs):
            calls.append(args[1].get("content"))
            return _orig_entry(self.manager, *args, **kwargs)

        with mock.patch.object(
            LLMContextManager, "_assemble_history_entry", side_effect=_counting_entry
        ):
            built2, _ = self._build(history)
        self.assertEqual(calls, ["q1", "a1"])
        self.assertEqual(built1, built2)


    def test_start_idx_shift_invalidates_fast_path(self):
        history = _history(
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
            ]
        )
        self._build(history)
        self.assertTrue(self.cache_path.is_file())

        # Simulate a cache whose covered prefix began at a different eligible
        # position than the current history (e.g. a compaction summary was
        # dropped by an edit, shifting the head while the tail is unchanged).
        # tail_sig still matches, so without the start_idx guard the fast path
        # would wrongly trust the misaligned rows.
        with open(self.cache_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data["start_idx"] = 1
        with open(self.cache_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        calls = []
        _orig_entry = LLMContextManager._assemble_history_entry

        def _counting_entry(*args, **kwargs):
            calls.append(args[1].get("content"))
            return _orig_entry(self.manager, *args, **kwargs)

        with mock.patch.object(
            LLMContextManager, "_assemble_history_entry", side_effect=_counting_entry
        ):
            built, _ = self._build(history)
        # The misaligned prefix must be fully re-assembled.
        self.assertEqual(calls, ["q1", "a1"])
        self.assertEqual([m["content"] for m in built], ["q1", "a1"])

        # The rebuilt cache carries the reconciled start_idx (0 here).
        with open(self.cache_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["start_idx"], 0)

    def test_edit_truncation_prunes_cache_keeps_prefix(self):
        history = _history(
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
                {"role": "assistant", "content": "a2"},
            ]
        )
        self.agent.conversation_history = list(history)
        self._build(history)
        self.assertTrue(self.cache_path.is_file())
        self.assertEqual(len(self._cache_rows()), 4)

        # Simulate an edit rewinding to the first user message (like the
        # /chat edit command path): truncate conversation_history in place,
        # then let the manager reconcile the cache.
        self.agent.conversation_history = list(history[:1])
        self.manager.prune_history_cache_after_truncation()
        self.assertTrue(self.cache_path.is_file())
        self.assertEqual(len(self._cache_rows()), 1)

        # Next build over the truncated history reuses the cached prefix: no
        # re-assembly of the surviving message is allowed.
        with mock.patch.object(
            LLMContextManager,
            "_assemble_history_entry",
            side_effect=AssertionError("should be served from cache"),
        ):
            built, _ = self._build(history[:1])
        self.assertEqual([m["content"] for m in built], ["q1"])


if __name__ == "__main__":
    unittest.main()