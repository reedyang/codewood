import unittest

from src.ai.ai_provider_clients import _make_stream_sanitizer, _sanitize_assistant_text


def _stream(deltas):
    """Drive a fresh sanitizer with the given list of deltas and return the
    concatenation of everything it emitted, including the final flush."""
    san = _make_stream_sanitizer()
    out_parts = []
    for d in deltas:
        out_parts.append(san.feed(d))
    out_parts.append(san.flush())
    return "".join(out_parts)


class StreamingSanitizerTests(unittest.TestCase):
    def test_passthrough_text_with_no_sentinels(self):
        self.assertEqual(_stream(["hello ", "world"]), "hello world")

    def test_strips_complete_channel_thought_block_in_one_chunk(self):
        self.assertEqual(
            _stream(["pre ", "<|channel>thought\nhidden body\n<channel|>", " post"]),
            "pre  post",
        )

    def test_strips_channel_thought_block_split_across_chunks(self):
        # Reproduces the original bug: opener and closer arrive in separate
        # chunks. The naive per-chunk regex sanitizer let the markers leak.
        deltas = [
            "  我将立即执行写入和分析操作。\n\n  ",
            "<|channel>thought\n  ",
            "<channel|>",
            "\nnext line",
        ]
        self.assertEqual(
            _stream(deltas),
            "  我将立即执行写入和分析操作。\n\n  \nnext line",
        )

    def test_channel_thought_split_after_just_lt_pipe(self):
        # Worst case: opener prefix is broken right after the very first
        # characters, then completed in the next chunk.
        deltas = ["abc<", "|channel>thought ", "secret stuff", "<channel|>def"]
        self.assertEqual(_stream(deltas), "abcdef")

    def test_strips_think_block_split_across_chunks(self):
        deltas = ["before ", "<thi", "nk>hidden", " more</thi", "nk> after"]
        self.assertEqual(_stream(deltas), "before  after")

    def test_two_consecutive_hidden_blocks(self):
        deltas = [
            "A",
            "<think>x</think>",
            "B",
            "<|channel>thought y<channel|>",
            "C",
        ]
        self.assertEqual(_stream(deltas), "ABC")

    def test_unterminated_hidden_block_is_dropped_at_flush(self):
        # If the model never closes the hidden block, the sanitizer must not
        # leak the opener or any of the in-progress content.
        deltas = ["visible ", "<|channel>thought hidden tail without closer"]
        self.assertEqual(_stream(deltas), "visible ")

    def test_lonely_lt_at_end_of_stream_is_emitted_on_flush(self):
        # A trailing `<` that turns out NOT to be the start of a sentinel must
        # not be permanently swallowed; flush() releases it.
        self.assertEqual(_stream(["plain text <"]), "plain text <")

    def test_partial_lt_then_unrelated_content_is_emitted(self):
        # Trailing `<` is initially withheld, but when the next chunk reveals
        # it is just a real angle bracket (e.g., `<3` heart), the sanitizer
        # must emit it once it can no longer become a sentinel.
        self.assertEqual(_stream(["heart ", "<", "3 end"]), "heart <3 end")

    def test_partial_channel_prefix_then_unrelated_content_is_emitted(self):
        # A trailing `<|c` that does not continue into a sentinel must be
        # released once an incompatible character arrives.
        self.assertEqual(_stream(["x ", "<|c", "ode!"]), "x <|code!")

    def test_each_chunk_is_byte_for_byte_safe(self):
        # Drive char-by-char to mimic the worst-case streaming granularity:
        # the sanitizer must still produce the exact same output as the
        # whole-text regex sanitizer.
        full = (
            "alpha "
            "<think>secret one</think>"
            " mid "
            "<|channel>thought\nsecret two\n<channel|>"
            " omega"
        )
        chars = list(full)
        self.assertEqual(_stream(chars), _sanitize_assistant_text(full))

    def test_empty_inputs_are_safe(self):
        self.assertEqual(_stream([]), "")
        self.assertEqual(_stream([""]), "")
        self.assertEqual(_stream(["", ""]), "")

    def test_existing_stateless_sanitizer_still_strips_full_text(self):
        # The whole-text regex path is still used for snapshots and history.
        self.assertEqual(
            _sanitize_assistant_text("a<think>hide</think>b<|channel>thought x<channel|>c"),
            "abc",
        )

    def test_stateless_sanitizer_strips_orphan_closer_literal(self):
        # Provider-side reasoning suppression can leave a stray closer with no
        # matching opener; the sanitizer must not let it leak.
        self.assertEqual(
            _sanitize_assistant_text("hello <channel|> world"),
            "hello  world",
        )

    def test_stateless_sanitizer_strips_orphan_opener_literal(self):
        self.assertEqual(
            _sanitize_assistant_text("a <|channel>thought never closes b"),
            "a  never closes b",
        )

    def test_stateless_sanitizer_strips_orphan_think_tags(self):
        self.assertEqual(_sanitize_assistant_text("a <think> never closes b"), "a  never closes b")
        self.assertEqual(_sanitize_assistant_text("a never opens </think> b"), "a never opens  b")

    def test_streaming_strips_orphan_closer_in_single_chunk(self):
        # Reproduces terminal line 42: only ``<channel|>`` arrives in this
        # turn, with no opener anywhere in this assistant message.
        self.assertEqual(
            _stream(["plan completed.\n\n  ", "<channel|>", "\nnext line"]),
            "plan completed.\n\n  \nnext line",
        )

    def test_streaming_strips_orphan_closer_split_across_chunks(self):
        self.assertEqual(
            _stream(["before ", "<chan", "nel|>", " after"]),
            "before  after",
        )

    def test_streaming_drops_partial_closer_at_flush(self):
        # The model aborted mid-closer (no further bytes ever arrive). The
        # 2+ char fragment ``<channel|`` is unmistakably a sentinel artifact,
        # so flush must drop it instead of leaking it as plain text.
        self.assertEqual(_stream(["body text ", "<channel|"]), "body text ")

    def test_streaming_preserves_lone_lt_at_flush(self):
        # A bare ``<`` at end-of-stream is preserved: it is far more likely a
        # real character than an aborted sentinel of length 1.
        self.assertEqual(_stream(["plain text <"]), "plain text <")


if __name__ == "__main__":
    unittest.main()
