import unittest

from cli.server.serve_app import _compute_chat_cache_stats, _compute_chat_token_stats


def _msg(
    model,
    cache_stats=None,
    output_tokens=None,
    reasoning_tokens=None,
    includes_reasoning=False,
):
    m = {"_model": model}
    if cache_stats is not None:
        m["_cache_stats"] = cache_stats
    if output_tokens is not None:
        m["_output_tokens"] = output_tokens
    if reasoning_tokens is not None:
        m["_reasoning_tokens"] = reasoning_tokens
    if includes_reasoning:
        m["_token_count_includes_reasoning"] = True
    return m


class _FakeAgent:
    """Minimal agent standing in for the dashboard stats functions.

    ``provider``/``model_name`` are deliberately set to a DIFFERENT model than
    the focused chat's record so a test passing proves the stats follow the
    focused chat (its record + history), not the shared agent globals.
    """

    def __init__(self, active_chat_id="chat-1", chats=None, sess_key="", live_history=None):
        self._chat_state = {"active": active_chat_id}
        self._chats = chats or {}
        self._sess_key = sess_key
        self.conversation_history = list(live_history or [])
        self.provider = "openai"
        self.model_name = "gpt-4o"

    def _find_chat_by_id(self, cid):
        return self._chats.get(cid)

    def _current_session_chat_key(self):
        return self._sess_key

    def _session_registry_key(self, cid):
        return f"ws-1::{cid}"


def _claude_chat(messages=None):
    return {
        "model_provider": "anthropic",
        "model_name": "claude-3.5",
        "messages": messages or [],
    }


class ChatCacheStatsTests(unittest.TestCase):
    def test_stats_follow_active_chat_record_not_agent_globals(self):
        """When the thread is NOT bound to the active chat (busy-switch case),
        stats come from the focused chat's persisted record + recorded model,
        never the ambient provider/model_name globals."""
        chat_1 = _claude_chat([
            _msg(
                "anthropic/claude-3.5",
                {"prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 50},
                output_tokens=200,
                reasoning_tokens=10,
            ),
            _msg("anthropic/claude-3.5", {"input_tokens": 80}),
            # A different model's message must be ignored.
            _msg("openai/gpt-4o", {"prompt_cache_hit_tokens": 999}),
        ])
        agent = _FakeAgent(
            active_chat_id="chat-1",
            chats={"chat-1": chat_1},
            sess_key="",  # not bound -> record path
        )

        stats = _compute_chat_cache_stats(agent)
        self.assertEqual(stats["model"], "claude-3.5")
        self.assertEqual(stats["supported"], True)
        self.assertEqual(stats["hasBreakdown"], False)
        self.assertEqual(stats["hitTokens"], 100)
        self.assertEqual(stats["missTokens"], 130)
        self.assertEqual(stats["totalTokens"], 230)

        token_stats = _compute_chat_token_stats(agent)
        self.assertEqual(token_stats["outputTokens"], 200)
        self.assertEqual(token_stats["reasoningTokens"], 10)
        self.assertEqual(token_stats["hasOutputTokens"], True)
        self.assertEqual(token_stats["hasReasoningTokens"], True)

    def test_live_session_history_preferred_when_bound_to_active_chat(self):
        """A loop thread bound to the focused chat keeps live stats."""
        chat_1 = _claude_chat([_msg("anthropic/claude-3.5", {"prompt_cache_hit_tokens": 1})])
        agent = _FakeAgent(
            active_chat_id="chat-1",
            chats={"chat-1": chat_1},
            sess_key="ws-1::chat-1",  # bound -> conversation_history path
            live_history=[_msg("anthropic/claude-3.5", {"prompt_cache_hit_tokens": 42})],
        )

        stats = _compute_chat_cache_stats(agent)
        self.assertEqual(stats["supported"], True)
        self.assertEqual(stats["hitTokens"], 42)
        self.assertEqual(stats["totalTokens"], 42)

    def test_unused_model_clears_stats_and_switch_back_restores(self):
        """Switching a chat to a model that has no messages clears the dashboard
        (supported=False); switching back to the recorded model restores it."""
        chat_1 = _claude_chat([
            _msg("anthropic/claude-3.5", {"prompt_cache_hit_tokens": 77}),
        ])

        # Switch the chat's recorded model to one never used in the chat.
        chat_1["model_name"] = "deepseek-v3"
        agent = _FakeAgent(active_chat_id="chat-1", chats={"chat-1": chat_1}, sess_key="")
        stats = _compute_chat_cache_stats(agent)
        self.assertEqual(stats["model"], "deepseek-v3")
        self.assertEqual(stats["supported"], False)
        self.assertEqual(stats["totalTokens"], 0)

        # Switch back to the original model -> its stats come back.
        chat_1["model_name"] = "claude-3.5"
        agent = _FakeAgent(active_chat_id="chat-1", chats={"chat-1": chat_1}, sess_key="")
        stats = _compute_chat_cache_stats(agent)
        self.assertEqual(stats["supported"], True)
        self.assertEqual(stats["hitTokens"], 77)
        self.assertEqual(stats["totalTokens"], 77)

    def test_empty_active_chat_returns_empty_stats(self):
        agent = _FakeAgent(active_chat_id="", chats={}, sess_key="")
        self.assertEqual(_compute_chat_cache_stats(agent)["supported"], False)
        self.assertEqual(_compute_chat_token_stats(agent)["hasOutputTokens"], False)


if __name__ == "__main__":
    unittest.main()
