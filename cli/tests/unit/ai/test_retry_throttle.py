"""Unit tests for the 429/503/connection infinite retry with countdown hook."""

import unittest
from unittest.mock import patch

from cli.ai.ai_provider_clients import (
    ModelCallError,
    OpenAIRequestError,
    _CONNECTION_RETRY_MESSAGE,
    _RETRY_CODE_CONNECTION,
    _call_with_openai_compatible,
    _is_transient_connection_error,
    _is_throttle_error,
    _model_call_error_throttle_code,
    _retry_wait_seconds,
    _sleep_with_retry_countdown,
    _throttle_error_message,
    set_retry_countdown_callback,
)
from cli.ai.ai_special_mode_prompts import InternalCallMode


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


def _connection_reset_attempt() -> dict:
    error = "('Connection aborted.', ConnectionResetError(10054, 'An existing connection was forcibly closed by the remote host', None, 10054, None))"
    return {"label": "chat with-suffix", "url": "http://x", "error": error}


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


class ConnectionErrorDetectionTests(unittest.TestCase):
    def test_transient_connection_error_types(self):
        self.assertTrue(_is_transient_connection_error(ConnectionResetError(10054, "reset")))
        self.assertTrue(_is_transient_connection_error(TimeoutError("timed out")))

        import requests

        reset = requests.exceptions.ConnectionError(
            "('Connection aborted.', ConnectionResetError(10054, 'reset', None, 10054, None))"
        )
        self.assertTrue(_is_transient_connection_error(reset))
        self.assertTrue(_is_transient_connection_error(requests.exceptions.Timeout("timed out")))
        self.assertFalse(_is_transient_connection_error(RuntimeError("boom")))
        self.assertFalse(_is_transient_connection_error(OSError(2, "no such file")))
        self.assertFalse(_is_transient_connection_error(ValueError("bad request")))

    def test_transient_connection_error_matches_text(self):
        self.assertTrue(_is_transient_connection_error(
            RuntimeError("('Connection aborted.', ConnectionResetError(10054, 'reset'))")
        ))
        self.assertTrue(_is_transient_connection_error(
            RuntimeError("Max retries exceeded with url: /v1/chat/completions")
        ))
        self.assertFalse(_is_transient_connection_error(RuntimeError("HTTP 500 boom")))

    def test_throttle_code_parses_connection_reset_attempt_text(self):
        err = ModelCallError("failed", attempt_errors=[_connection_reset_attempt()])
        self.assertEqual(_model_call_error_throttle_code(err), _RETRY_CODE_CONNECTION)

    def test_throttle_code_prefers_http_status_over_connection_text(self):
        attempts = [_connection_reset_attempt(), _throttle_attempt(429)]
        err = ModelCallError("failed", attempt_errors=attempts)
        self.assertEqual(_model_call_error_throttle_code(err), 429)


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

    def test_sleep_aborts_with_keyboard_interrupt_when_cancelled(self):
        state = {"cancelled": False}

        def should_cancel():
            return state["cancelled"]

        with patch("cli.ai.ai_provider_clients.time.sleep"):
            state["cancelled"] = True
            with self.assertRaises(KeyboardInterrupt):
                _sleep_with_retry_countdown(
                    3.0,
                    retry_number=1,
                    code=429,
                    model_name="m",
                    should_cancel=should_cancel,
                )

    def test_sleep_abort_emits_done_tick_so_gui_clears_countdown(self):
        # Regression: aborting the wait on a user stop must publish the final
        # ``done`` countdown tick, otherwise the GUI keeps showing a frozen
        # "retry #n in Xs" line after the user stopped.
        ticks = []
        set_retry_countdown_callback(lambda **kw: ticks.append(dict(kw)))
        with patch("cli.ai.ai_provider_clients.time.sleep"):
            with self.assertRaises(KeyboardInterrupt):
                _sleep_with_retry_countdown(
                    3.0,
                    retry_number=2,
                    code=429,
                    model_name="m",
                    message="rpm exhausted",
                    should_cancel=lambda: True,
                )
        self.assertTrue(ticks, "expected at least one countdown tick")
        self.assertTrue(ticks[-1].get("done"))
        self.assertEqual(ticks[-1]["remaining_seconds"], 0.0)
        self.assertEqual(ticks[-1]["code"], 429)
        self.assertEqual(ticks[-1]["retry_number"], 2)

    def test_sleep_aborts_immediately_even_when_not_yet_ticking(self):
        def should_cancel():
            return True

        with patch("cli.ai.ai_provider_clients.time.sleep") as mock_sleep:
            with self.assertRaises(KeyboardInterrupt):
                _sleep_with_retry_countdown(
                    3.0,
                    retry_number=1,
                    code=429,
                    model_name="m",
                    should_cancel=should_cancel,
                )
        mock_sleep.assert_not_called()


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
            internal_mode=InternalCallMode.REGULAR,
            tool_schemas=None,
            tool_choice=None,
            append_history=lambda *a, **k: None,
            api_key_error_msg="no key",
            default_base_url="http://x",
        )
        kwargs.update(overrides)
        return _call_with_openai_compatible(**kwargs)

    def test_retry_stops_with_keyboard_interrupt_when_user_cancels(self):
        state = {"cancelled": False}
        calls = {"n": 0}

        def fake_call(**kwargs):
            calls["n"] += 1
            raise ModelCallError(
                "429 Client Error: Too Many Requests for url: http://x",
                attempt_errors=[_throttle_attempt(429)],
            )

        waits = []

        def fake_sleep(wait, **kw):
            waits.append(wait)
            state["cancelled"] = True
            raise KeyboardInterrupt

        with patch(
            "cli.ai.ai_provider_clients._call_openai_with_suffix_strategy",
            side_effect=fake_call,
        ), patch(
            "cli.ai.ai_provider_clients._sleep_with_retry_countdown",
            side_effect=fake_sleep,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self._call(
                    {"api_key": "k", "base_url": "http://x", "api_mode": "chat"},
                    should_cancel=lambda: state["cancelled"],
                )
        # The user stop (reported through the cancel probe during the backoff
        # wait) aborts the loop before any further retry attempt.
        self.assertEqual(calls["n"], 1)
        self.assertEqual(waits, [3])

    def test_retry_stops_when_cancelled_between_sleep_and_next_attempt(self):
        calls = {"n": 0}

        def fake_call(**kwargs):
            calls["n"] += 1
            raise ModelCallError(
                "429 Client Error: Too Many Requests for url: http://x",
                attempt_errors=[_throttle_attempt(429)],
            )

        with patch(
            "cli.ai.ai_provider_clients._call_openai_with_suffix_strategy",
            side_effect=fake_call,
        ), patch(
            "cli.ai.ai_provider_clients._sleep_with_retry_countdown",
            side_effect=lambda wait, **kw: None,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self._call(
                    {"api_key": "k", "base_url": "http://x", "api_mode": "chat"},
                    should_cancel=lambda: True,
                )
        # The cancel probe is checked before the very first attempt, so no
        # network call is ever made when the user already stopped.
        self.assertEqual(calls["n"], 0)

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

    def test_internal_call_max_retries_zero_never_retries(self):
        # Regression: best-effort internal calls (memory query expansion,
        # chat title, ...) run nested inside user-facing turns — the endless
        # 429 backoff kept them (and therefore the whole turn) stuck forever.
        # With ``max_retries=0`` a throttle error fails immediately.
        calls = {"n": 0}

        def fake_call(**kwargs):
            calls["n"] += 1
            raise ModelCallError(
                "429 Client Error: Too Many Requests for url: http://x",
                attempt_errors=[_throttle_attempt(429)],
            )

        with patch(
            "cli.ai.ai_provider_clients._call_openai_with_suffix_strategy",
            side_effect=fake_call,
        ), patch(
            "cli.ai.ai_provider_clients._sleep_with_retry_countdown",
            side_effect=lambda wait, **kw: self.fail("internal call must not sleep for a retry"),
        ) as fake_sleep:
            with self.assertRaises(ModelCallError):
                self._call(
                    {"api_key": "k", "base_url": "http://x", "api_mode": "chat"},
                    max_retries=0,
                )
        self.assertEqual(calls["n"], 1)
        fake_sleep.assert_not_called()

    def test_max_retries_bounds_retry_attempts(self):
        calls = {"n": 0}

        def fake_call(**kwargs):
            calls["n"] += 1
            raise ModelCallError(
                "429 Client Error: Too Many Requests for url: http://x",
                attempt_errors=[_throttle_attempt(429)],
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
                self._call(
                    {"api_key": "k", "base_url": "http://x", "api_mode": "chat"},
                    max_retries=2,
                )
        # One initial attempt + exactly 2 retries, then the error propagates.
        self.assertEqual(calls["n"], 3)
        self.assertEqual(waits, [3, 4])

    def test_max_retries_none_keeps_endless_retry(self):
        calls = {"n": 0}

        def fake_call(**kwargs):
            calls["n"] += 1
            raise ModelCallError(
                "429 Client Error: Too Many Requests for url: http://x",
                attempt_errors=[_throttle_attempt(429)],
            )

        with patch(
            "cli.ai.ai_provider_clients._call_openai_with_suffix_strategy",
            side_effect=fake_call,
        ), patch(
            "cli.ai.ai_provider_clients._sleep_with_retry_countdown",
            side_effect=lambda wait, **kw: None,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self._call(
                    {"api_key": "k", "base_url": "http://x", "api_mode": "chat"},
                    max_retries=None,
                    should_cancel=lambda: calls["n"] >= 3,
                )
        # No budget → retries continue until the cancel probe fires.
        self.assertGreaterEqual(calls["n"], 3)

    def test_transient_connection_error_respects_max_retries(self):
        calls = {"n": 0}

        def fake_call(**kwargs):
            calls["n"] += 1
            raise ConnectionResetError("connection reset by peer")

        with patch(
            "cli.ai.ai_provider_clients._call_openai_with_suffix_strategy",
            side_effect=fake_call,
        ), patch(
            "cli.ai.ai_provider_clients._sleep_with_retry_countdown",
            side_effect=lambda wait, **kw: None,
        ):
            with self.assertRaises(ModelCallError):
                self._call(
                    {"api_key": "k", "base_url": "http://x", "api_mode": "chat"},
                    max_retries=1,
                )
        self.assertEqual(calls["n"], 2)

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

    def test_connection_reset_retries_with_backoff_then_succeeds(self):
        import requests

        calls = {"n": 0}
        sleeps = []

        def fake_call(**kwargs):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise requests.exceptions.ConnectionError(
                    "('Connection aborted.', ConnectionResetError(10054, 'An existing connection was forcibly closed by the remote host', None, 10054, None))"
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
            self.assertEqual(kw["code"], _RETRY_CODE_CONNECTION)
            self.assertEqual(kw["message"], _CONNECTION_RETRY_MESSAGE)
        self.assertEqual(sleeps[0]["retry_number"], 1)
        self.assertEqual(sleeps[2]["retry_number"], 3)

    def test_connection_reset_inside_model_call_error_retries(self):
        calls = {"n": 0}

        def fake_call(**kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise ModelCallError(
                    "('Connection aborted.', ConnectionResetError(10054, 'reset', None, 10054, None))",
                    attempt_errors=[_connection_reset_attempt()],
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

    def test_plain_exception_not_connection_is_not_retried(self):
        calls = {"n": 0}

        def fake_call(**kwargs):
            calls["n"] += 1
            raise RuntimeError("boom")

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