"""Unit tests for the 429/503 infinite retry with countdown hook."""

import unittest
from unittest.mock import patch

from cli.ai.ai_provider_clients import (
    ModelCallError,
    OpenAIRequestError,
    _call_with_openai_compatible,
    _is_throttle_error,
    _model_call_error_throttle_code,
    _retry_wait_seconds,
    _sleep_with_retry_countdown,
    _throttle_error_message,
    set_retry_countdown_callback,
)


def _throttle_error(code: int) -> OpenAIRequestError:
    return OpenAIRequestError(
        f"{code} Client Error: boom",
        status_code=code,
        response_body=f'{{"error":{{"message":"{code}"}}}}',
        url="http://x/v1/chat/completions",
    )


def _throttle_attempt(code: int) -> dict:
    return {
        "label": "chat with-suffix",
        "error": f"{code} Client Error: boom for url: http://x",
        "response_body": f'{{"error":{{"message":"rpm exhausted ({code})"}}}}',
    }


class RetryWaitSequenceTests(unittest.TestCase):
    def test_wait_sequence_matches_spec(self):
        # 3s first, then 2^n (4, 8, 16, 32), capped at 60s forever.
        self.assertEqual(_retry_wait_seconds(1), 3)
        self.assertEqual(_retry_wait_seconds(2), 4)
        self.assertEqual(_retry_wait_seconds(3), 8)
        self.assertEqual(_retry_wait_seconds(4), 16)
        self.assertEqual(_retry_wait_seconds(5), 32)
        self.assertEqual(_retry_wait_seconds(6), 60)
        self.assertEqual(_retry_wait_seconds(7), 60)
        self.assertEqual(_retry_wait_seconds(20), 60)


class ThrottleDetectionTests(unittest.TestCase):
    def test_is_throttle_error_429_and_503(self):
        self.assertTrue(_is_throttle_error(_throttle_error(429)))
        self.assertTrue(_is_throttle_error(_throttle_error(503)))
        self.assertFalse(_is_throttle_error(_throttle_error(400)))
        self.assertFalse(_is_throttle_error(RuntimeError("boom")))

    def test_throttle_code_parses_429_attempt_text(self):
        err = ModelCallError("failed", attempt_errors=[_throttle_attempt(429)])
        self.assertEqual(_model_call_error_throttle_code(err), 429)

    def test_throttle_code_parses_503_server_error_text(self):
        err = ModelCallError("failed", attempt_errors=[_throttle_attempt(503)])
        self.assertEqual(_model_call_error_throttle_code(err), 503)

    def test_non_throttle_text_returns_none(self):
        err = ModelCallError(
            "failed",
            attempt_errors=[
                {"label": "chat", "error": "400 Client Error: Bad Request for url: http://x"}
            ],
        )
        self.assertIsNone(_model_call_error_throttle_code(err))

    def test_throttle_error_message_extracted_from_response_body(self):
        err = ModelCallError("failed", attempt_errors=[_throttle_attempt(429)])
        self.assertEqual(_throttle_error_message(err, 429), "rpm exhausted (429)")

    def test_throttle_error_message_falls_back_to_embedded_body_text(self):
        err = ModelCallError(
            "failed",
            attempt_errors=[
                {
                    "label": "chat",
                    "error": (
                        "429 Client Error: Too Many Requests for url: http://x; "
                        'response_body={"error":{"message":"quota exceeded"}}'
                    ),
                }
            ],
        )
        self.assertEqual(_throttle_error_message(err, 429), "quota exceeded")

    def test_throttle_error_message_empty_when_unparseable(self):
        err = ModelCallError(
            "failed",
            attempt_errors=[
                {"label": "chat", "error": "429 Client Error: Too Many Requests for url: http://x"}
            ],
        )
        self.assertEqual(_throttle_error_message(err, 429), "")


class CountdownSleepTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_retry_countdown_callback(None)

    def test_callback_ticks_each_second_then_done(self):
        ticks = []
        set_retry_countdown_callback(lambda **kw: ticks.append(dict(kw)))
        with patch("cli.ai.ai_provider_clients.time.sleep"):
            _sleep_with_retry_countdown(
                3.0,
                retry_number=1,
                code=429,
                model_name="m",
                message="rpm exhausted",
            )
        remaining = [t["remaining_seconds"] for t in ticks]
        self.assertEqual(remaining, [3.0, 2.0, 1.0, 0.0])
        self.assertTrue(ticks[-1].get("done"))
        self.assertEqual(ticks[0]["code"], 429)
        self.assertEqual(ticks[0]["retry_number"], 1)
        self.assertEqual(ticks[0]["wait_seconds"], 3.0)
        self.assertEqual(ticks[0]["message"], "rpm exhausted")
        self.assertEqual(ticks[-1]["message"], "rpm exhausted")


class InfiniteRetryLoopTests(unittest.TestCase):
    def _call(self, conf: dict, **overrides):
        kwargs = dict(
            model_name="m",
            conf=conf,
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
            return_message=False,
            image_data=None,
            image_user_idx=None,
            image_user_text="",
            session_summary_mode=False,
            memory_query_expansion_mode=False,
            tool_schemas=None,
            tool_choice=None,
            append_history=lambda *a, **k: None,
            api_key_error_msg="no key",
            default_base_url="http://x",
        )
        kwargs.update(overrides)
        return _call_with_openai_compatible(**kwargs)

    def test_throttle_retries_with_backoff_then_succeeds(self):
        calls = {"n": 0}
        sleeps = []

        def fake_call(**kwargs):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise ModelCallError(
                    "429 Client Error: Too Many Requests for url: http://x",
                    attempt_errors=[_throttle_attempt(429)],
                )
            return "ok"

        waits = []
        with patch(
            "cli.ai.ai_provider_clients._call_openai_with_suffix_strategy",
            side_effect=fake_call,
        ), patch(
            "cli.ai.ai_provider_clients._sleep_with_retry_countdown",
            side_effect=lambda wait, **kw: (waits.append(wait), sleeps.append(kw)),
        ):
            result = self._call({"api_key": "k", "base_url": "http://x", "api_mode": "chat"})
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 4)
        self.assertEqual(waits, [3, 4, 8])
        for kw in sleeps:
            self.assertEqual(kw["code"], 429)
            self.assertEqual(kw["message"], "rpm exhausted (429)")
        self.assertEqual(sleeps[0]["retry_number"], 1)
        self.assertEqual(sleeps[2]["retry_number"], 3)

    def test_503_also_retries_forever(self):
        calls = {"n": 0}

        def fake_call(**kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise ModelCallError(
                    "503 Server Error: Service Unavailable for url: http://x",
                    attempt_errors=[_throttle_attempt(503)],
                )
            return "ok"

        waits = []
        with patch(
            "cli.ai.ai_provider_clients._call_openai_with_suffix_strategy",
            side_effect=fake_call,
        ), patch(
            "cli.ai.ai_provider_clients._sleep_with_retry_countdown",
            side_effect=lambda wait, **kw: waits.append(wait),
        ):
            result = self._call({"api_key": "k", "base_url": "http://x", "api_mode": "chat"})
        self.assertEqual(result, "ok")
        self.assertEqual(waits, [3, 4])

    def test_non_throttle_error_is_not_retried(self):
        calls = {"n": 0}

        def fake_call(**kwargs):
            calls["n"] += 1
            raise ModelCallError(
                "400 Client Error: Bad Request for url: http://x",
                attempt_errors=[
                    {"label": "chat", "error": "400 Client Error: Bad Request for url: http://x"}
                ],
            )

        waits = []
        with patch(
            "cli.ai.ai_provider_clients._call_openai_with_suffix_strategy",
            side_effect=fake_call,
        ), patch(
            "cli.ai.ai_provider_clients._sleep_with_retry_countdown",
            side_effect=lambda wait, **kw: waits.append(wait),
        ):
            with self.assertRaises(ModelCallError):
                self._call({"api_key": "k", "base_url": "http://x", "api_mode": "chat"})
        self.assertEqual(calls["n"], 1)
        self.assertEqual(waits, [])


if __name__ == "__main__":
    unittest.main()