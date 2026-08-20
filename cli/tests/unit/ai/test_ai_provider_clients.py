import json
import os
import unittest
from unittest.mock import patch

from cli.ai.ai_provider_clients import (
    ModelCallError,
    OpenAIRequestError,
    _build_openai_payload,
    _call_openai_once,
    _call_openai_with_suffix_strategy,
    _stream_openai_like_response,
)
from cli.ai.ai_special_mode_prompts import InternalCallMode
from cli.core.config.config_env import resolve_env_placeholder, resolve_string_values_in_data


def _payload(reasoning_effort):
    return _build_openai_payload(
        api_kind="chat",
        model_name="m",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        image_data=None,
        image_user_idx=None,
        image_user_text="",
        internal_mode=InternalCallMode.REGULAR,
        tool_schemas=None,
        tool_choice=None,
        force_disable_thinking=False,
        reasoning_effort=reasoning_effort,
    )


class ReasoningEffortPayloadTests(unittest.TestCase):
    def test_single_level_string_is_sent_lowercased(self):
        payload = _payload("High")
        self.assertEqual(payload.get("reasoning_effort"), "high")

    def test_catalog_list_is_omitted_not_serialized(self):
        # The catalog stores supported levels as a list; it must never be sent
        # as its repr (``"['low', 'high']"``) which the server rejects (400).
        payload = _payload(["low", "high"])
        self.assertNotIn("reasoning_effort", payload)

    def test_empty_value_is_omitted(self):
        self.assertNotIn("reasoning_effort", _payload(""))


class ResolveEnvPlaceholderTests(unittest.TestCase):
    def test_plain_string_keeps_original_value(self):
        self.assertEqual(resolve_env_placeholder("abc123"), "abc123")

    def test_resolve_env_placeholder_string(self):
        old = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "env-secret"
        try:
            self.assertEqual(resolve_env_placeholder("${OPENAI_API_KEY}"), "env-secret")
        finally:
            if old is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = old

    def test_missing_env_name_returns_empty_string(self):
        self.assertEqual(resolve_env_placeholder("${NOT_EXISTS_FOR_TEST}"), "")


class ResolveStringValuesInDataTests(unittest.TestCase):
    def test_resolve_all_string_params_recursively(self):
        old_key = os.environ.get("OPENAI_API_KEY")
        old_base = os.environ.get("OPENAI_BASE_URL")
        os.environ["OPENAI_API_KEY"] = "token-1"
        os.environ["OPENAI_BASE_URL"] = "https://example.com/v1"
        try:
            raw = {
                "api_key": "${OPENAI_API_KEY}",
                "base_url": "${OPENAI_BASE_URL}",
                "model": "gpt-4o-mini",
                "nested": {
                    "headers": ["Bearer ${OPENAI_API_KEY}", "${OPENAI_API_KEY}"],
                    "timeout": 120,
                },
            }
            got = resolve_string_values_in_data(raw)
            self.assertEqual(got["api_key"], "token-1")
            self.assertEqual(got["base_url"], "https://example.com/v1")
            self.assertEqual(got["model"], "gpt-4o-mini")
            self.assertEqual(got["nested"]["headers"][0], "Bearer ${OPENAI_API_KEY}")
            self.assertEqual(got["nested"]["headers"][1], "token-1")
            self.assertEqual(got["nested"]["timeout"], 120)
        finally:
            if old_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = old_key
            if old_base is None:
                os.environ.pop("OPENAI_BASE_URL", None)
            else:
                os.environ["OPENAI_BASE_URL"] = old_base

    def test_resolve_full_config_style_string_fields(self):
        old_provider = os.environ.get("MODEL_PROVIDER")
        old_policy = os.environ.get("EXEC_POLICY")
        old_model = os.environ.get("MODEL_NAME")
        os.environ["MODEL_PROVIDER"] = "openai"
        os.environ["EXEC_POLICY"] = "moderate"
        os.environ["MODEL_NAME"] = "gpt-4o-mini"
        try:
            raw = {
                "execution_policy": "${EXEC_POLICY}",
                "model_providers": [
                    {
                        "provider": "${MODEL_PROVIDER}",
                        "params": {
                            "models": ["${MODEL_NAME}"],
                        },
                    }
                ],
            }
            got = resolve_string_values_in_data(raw)
            self.assertEqual(got["execution_policy"], "moderate")
            self.assertEqual(got["model_providers"][0]["provider"], "openai")
            self.assertEqual(got["model_providers"][0]["params"]["models"][0], "gpt-4o-mini")
        finally:
            if old_provider is None:
                os.environ.pop("MODEL_PROVIDER", None)
            else:
                os.environ["MODEL_PROVIDER"] = old_provider
            if old_policy is None:
                os.environ.pop("EXEC_POLICY", None)
            else:
                os.environ["EXEC_POLICY"] = old_policy
            if old_model is None:
                os.environ.pop("MODEL_NAME", None)
            else:
                os.environ["MODEL_NAME"] = old_model

    def test_resolve_non_string_types_from_env(self):
        old_int = os.environ.get("CFG_INT")
        old_float = os.environ.get("CFG_FLOAT")
        old_bool = os.environ.get("CFG_BOOL")
        old_null = os.environ.get("CFG_NULL")
        old_list = os.environ.get("CFG_LIST")
        old_obj = os.environ.get("CFG_OBJ")
        old_yes = os.environ.get("CFG_YES")
        os.environ["CFG_INT"] = "42"
        os.environ["CFG_FLOAT"] = "3.5"
        os.environ["CFG_BOOL"] = "true"
        os.environ["CFG_NULL"] = "null"
        os.environ["CFG_LIST"] = "[1,2,3]"
        os.environ["CFG_OBJ"] = "{\"a\":1}"
        os.environ["CFG_YES"] = "yes"
        try:
            raw = {
                "int_v": "${CFG_INT}",
                "float_v": "${CFG_FLOAT}",
                "bool_v": "${CFG_BOOL}",
                "null_v": "${CFG_NULL}",
                "list_v": "${CFG_LIST}",
                "obj_v": "${CFG_OBJ}",
                "yes_v": "${CFG_YES}",
            }
            got = resolve_string_values_in_data(raw)
            self.assertEqual(got["int_v"], 42)
            self.assertEqual(got["float_v"], 3.5)
            self.assertEqual(got["bool_v"], True)
            self.assertEqual(got["null_v"], None)
            self.assertEqual(got["list_v"], [1, 2, 3])
            self.assertEqual(got["obj_v"], {"a": 1})
            self.assertEqual(got["yes_v"], True)
        finally:
            if old_int is None:
                os.environ.pop("CFG_INT", None)
            else:
                os.environ["CFG_INT"] = old_int
            if old_float is None:
                os.environ.pop("CFG_FLOAT", None)
            else:
                os.environ["CFG_FLOAT"] = old_float
            if old_bool is None:
                os.environ.pop("CFG_BOOL", None)
            else:
                os.environ["CFG_BOOL"] = old_bool
            if old_null is None:
                os.environ.pop("CFG_NULL", None)
            else:
                os.environ["CFG_NULL"] = old_null
            if old_list is None:
                os.environ.pop("CFG_LIST", None)
            else:
                os.environ["CFG_LIST"] = old_list
            if old_obj is None:
                os.environ.pop("CFG_OBJ", None)
            else:
                os.environ["CFG_OBJ"] = old_obj
            if old_yes is None:
                os.environ.pop("CFG_YES", None)
            else:
                os.environ["CFG_YES"] = old_yes


class OpenAIRouteFallbackTests(unittest.TestCase):
    def test_semantic_400_does_not_probe_alternate_suffix_url(self):
        calls = []

        def _fake_call_once(**kwargs):
            calls.append(str(kwargs.get("url") or ""))
            raise OpenAIRequestError(
                "400 Bad Request; response_body={\"error\":{\"code\":\"data_inspection_failed\"}}",
                status_code=400,
                response_body='{"error":{"code":"data_inspection_failed"}}',
                url=str(kwargs.get("url") or ""),
            )

        with patch("cli.ai.ai_provider_clients._openai_get_prefer_no_suffix", return_value=False):
            with patch("cli.ai.ai_provider_clients._call_openai_once", _fake_call_once):
                with self.assertRaises(ModelCallError) as ctx:
                    _call_openai_with_suffix_strategy(
                        model_name="m",
                        api_kind="chat",
                        base_url="https://token.sensenova.cn/v1",
                        headers={},
                        messages=[{"role": "user", "content": "hi"}],
                        stream=False,
                        return_message=False,
                        image_data=None,
                        image_user_idx=None,
                        image_user_text="",
                        internal_mode=InternalCallMode.REGULAR,
                        tool_schemas=None,
                        tool_choice=None,
                        append_history=lambda *_args, **_kwargs: None,
                    )

        self.assertEqual(calls, ["https://token.sensenova.cn/v1/chat/completions"])
        self.assertEqual(len(ctx.exception.attempt_errors), 1)
        self.assertEqual(
            ctx.exception.attempt_errors[0]["label"],
            "chat with-suffix",
        )

    def test_route_chat_never_probes_bare_base(self):
        calls = []

        def _fake_call_once(**kwargs):
            url = str(kwargs.get("url") or "")
            calls.append(url)
            raise OpenAIRequestError(
                "404 Not Found",
                status_code=404,
                response_body='{"error":"not found"}',
                url=url,
            )

        with patch("cli.ai.ai_provider_clients._openai_get_prefer_no_suffix", return_value=False):
            with patch("cli.ai.ai_provider_clients._call_openai_once", _fake_call_once):
                with self.assertRaises(ModelCallError):
                    _call_openai_with_suffix_strategy(
                        model_name="m",
                        api_kind="chat",
                        base_url="https://token.sensenova.cn/v1",
                        headers={},
                        messages=[{"role": "user", "content": "hi"}],
                        stream=False,
                        return_message=False,
                        image_data=None,
                        image_user_idx=None,
                        image_user_text="",
                        internal_mode=InternalCallMode.REGULAR,
                        tool_schemas=None,
                        tool_choice=None,
                        append_history=lambda *_args, **_kwargs: None,
                    )

        # chat/completions is always appended, so the bare base is never probed
        # (it would 404). The secondary fallback collapses to the same URL and is
        # skipped, leaving a single attempted endpoint.
        self.assertEqual(calls, ["https://token.sensenova.cn/v1/chat/completions"])

    def test_explicit_mode_does_not_probe_alternate_url(self):
        calls = []

        def _fake_call_once(**kwargs):
            calls.append(str(kwargs.get("url") or ""))
            raise ValueError(
                "Unsupported OpenAI response format: expected 'choices' or Responses API 'output'. "
                "Top-level keys: [completed_at, created_at, id, model, object, output, status, usage]"
            )

        with patch("cli.ai.ai_provider_clients._openai_get_prefer_no_suffix", return_value=False):
            with patch("cli.ai.ai_provider_clients._call_openai_once", _fake_call_once):
                with self.assertRaises(ModelCallError) as ctx:
                    _call_openai_with_suffix_strategy(
                        model_name="m",
                        api_kind="responses",
                        base_url="http://127.0.0.1:8080/v1",
                        headers={},
                        messages=[{"role": "user", "content": "hi"}],
                        stream=False,
                        return_message=False,
                        image_data=None,
                        image_user_idx=None,
                        image_user_text="",
                        internal_mode=InternalCallMode.REGULAR,
                        tool_schemas=None,
                        tool_choice=None,
                        allow_probe=False,
                        append_history=lambda *_args, **_kwargs: None,
                    )

        self.assertEqual(calls, ["http://127.0.0.1:8080/v1/responses"])
        self.assertEqual(len(ctx.exception.attempt_errors), 1)
        self.assertIn("Unsupported OpenAI response format", str(ctx.exception))

    def test_auto_mode_probes_alternate_url_and_marks_it_as_fallback(self):
        calls = []

        def _fake_call_once(**kwargs):
            url = str(kwargs.get("url") or "")
            calls.append(url)
            if len(calls) == 1:
                raise ValueError(
                    "Unsupported OpenAI response format: expected 'choices' or Responses API 'output'. "
                    "Top-level keys: [completed_at, created_at, id, model, object, output, status, usage]"
                )
            raise OpenAIRequestError(
                "404 Client Error: Not Found for url: http://127.0.0.1:8080/v1; "
                "response_body={\"error\":{\"message\":\"File Not Found\","
                "\"type\":\"not_found_error\",\"code\":404}}",
                status_code=404,
                response_body='{"error":{"message":"File Not Found",'
                '"type":"not_found_error","code":404}}',
                url=url,
            )

        with patch("cli.ai.ai_provider_clients._openai_get_prefer_no_suffix", return_value=False):
            with patch("cli.ai.ai_provider_clients._call_openai_once", _fake_call_once):
                with self.assertRaises(ModelCallError) as ctx:
                    _call_openai_with_suffix_strategy(
                        model_name="m",
                        api_kind="responses",
                        base_url="http://127.0.0.1:8080/v1",
                        headers={},
                        messages=[{"role": "user", "content": "hi"}],
                        stream=False,
                        return_message=False,
                        image_data=None,
                        image_user_idx=None,
                        image_user_text="",
                        internal_mode=InternalCallMode.REGULAR,
                        tool_schemas=None,
                        tool_choice=None,
                        append_history=lambda *_args, **_kwargs: None,
                    )

        self.assertEqual(
            calls,
            [
                "http://127.0.0.1:8080/v1/responses",
                "http://127.0.0.1:8080/v1",
            ],
        )
        # The user-facing message is the primary error; the fallback's 404
        # "File Not Found" stays in the attempt trail (logs) only.
        self.assertIn("Unsupported OpenAI response format", str(ctx.exception))
        self.assertNotIn("File Not Found", str(ctx.exception))
        attempts = ctx.exception.attempt_errors
        self.assertEqual(len(attempts), 2)
        self.assertNotIn("fallback", attempts[0])
        self.assertEqual(attempts[1].get("fallback"), "1")
        self.assertIn("File Not Found", attempts[1]["error"])


_RAW_CHANNEL_CONTENT = "<|channel>thought\ntest message<channel|>visible"


class _FakeJsonResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self):
        return iter(self._lines)


class ResponsesApiContentContractTests(unittest.TestCase):
    """Responses API must follow the same storage contract as chat/completions:
    ``content`` keeps the raw text (the cleaned form lives in the reply block's
    content nodes), and thinking extracted from content sets
    ``_thinking_from_content``.
    """

    def test_nonstream_responses_keeps_raw_content_and_extracts_thinking(self):
        data = {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": _RAW_CHANNEL_CONTENT}],
                }
            ]
        }
        recorded = []

        with patch(
            "cli.ai.ai_provider_clients._post_openai_request",
            return_value=_FakeJsonResponse(data),
        ):
            message = _call_openai_once(
                model_name="m",
                api_kind="responses",
                url="https://example.com/v1/responses",
                headers={},
                messages=[{"role": "user", "content": "hi"}],
                stream=False,
                return_message=True,
                image_data=None,
                image_user_idx=None,
                image_user_text="",
                internal_mode=InternalCallMode.REGULAR,
                tool_schemas=None,
                tool_choice=None,
                force_disable_thinking=False,
                append_history=lambda text, msg: recorded.append((text, msg)),
            )

        self.assertEqual(len(recorded), 1)
        raw_text, history_msg = recorded[0]
        self.assertEqual(raw_text, _RAW_CHANNEL_CONTENT)
        self.assertEqual(history_msg["content"], _RAW_CHANNEL_CONTENT)
        self.assertNotIn("_clean_content", history_msg)
        self.assertEqual(history_msg["_thinking"], "test message")
        self.assertTrue(history_msg["_thinking_from_content"])
        self.assertEqual(message["content"], "visible")
        self.assertEqual(message["_thinking"], "test message")
        self.assertTrue(message["_thinking_from_content"])

    def _run_stream(self, events):
        lines = [b"data: " + json.dumps(evt).encode("utf-8") for evt in events]
        lines.append(b"data: [DONE]")
        recorded = []
        result = _stream_openai_like_response(
            resp=_FakeStreamResponse(lines),
            append_history=lambda text, msg: recorded.append((text, msg)),
            url="https://example.com/v1/responses",
        )
        visible = "".join(list(result))
        return result, visible, recorded

    def test_stream_responses_keeps_raw_content_and_flags_thinking(self):
        snapshot = {
            "type": "response.completed",
            "response": {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": _RAW_CHANNEL_CONTENT}
                        ],
                    }
                ]
            },
        }
        events = [
            {"type": "response.output_text.delta", "delta": "<|channel>"},
            {"type": "response.output_text.delta", "delta": "thought\ntest message"},
            {"type": "response.output_text.delta", "delta": "<channel|>visible"},
            snapshot,
        ]
        result, visible, recorded = self._run_stream(events)
        self.assertEqual(visible, "visible")
        self.assertEqual(len(recorded), 1)
        raw_text, history_msg = recorded[0]
        self.assertEqual(raw_text, _RAW_CHANNEL_CONTENT)
        self.assertEqual(history_msg["content"], _RAW_CHANNEL_CONTENT)
        self.assertNotIn("_clean_content", history_msg)
        self.assertEqual(history_msg["_thinking"], "test message")
        self.assertTrue(history_msg["_thinking_from_content"])

    def test_stream_snapshot_only_extracts_thinking_from_raw_content(self):
        # No text deltas at all: the raw content (markers included) arrives
        # only in the final snapshot. Thinking must still be extracted and
        # flagged as coming from content.
        events = [
            {
                "type": "response.completed",
                "response": {
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": _RAW_CHANNEL_CONTENT}
                            ],
                        }
                    ]
                },
            }
        ]
        result, visible, recorded = self._run_stream(events)
        self.assertEqual(visible, "visible")
        raw_text, history_msg = recorded[0]
        self.assertEqual(raw_text, _RAW_CHANNEL_CONTENT)
        self.assertEqual(history_msg["content"], _RAW_CHANNEL_CONTENT)
        self.assertNotIn("_clean_content", history_msg)
        self.assertEqual(history_msg["_thinking"], "test message")
        self.assertTrue(history_msg["_thinking_from_content"])


if __name__ == "__main__":
    unittest.main()
