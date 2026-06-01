import unittest
from unittest.mock import patch

import requests

import src.ai.ai_provider_clients as ai_provider_clients
from src.ai.ai_provider_clients import ProviderCallContext, call_ai_with_provider


class _FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


class _FakeResponsesApiResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "object": "response",
            "output": [
                {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "..."}]},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hello from responses api"}],
                },
            ],
        }


class _FakeChatToolsResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": "{\"path\":\"README.md\"}",
                                },
                            }
                        ],
                    }
                }
            ]
        }


class _FakeResponsesToolsApiResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "object": "response",
            "output": [
                {
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": "{\"path\":\"README.md\"}",
                    "call_id": "call_resp_1",
                }
            ],
        }


class _FakeOllama:
    def __init__(self):
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return [{"message": {"content": "hello"}}, {"message": {"content": " world"}}]
        return {"message": {"content": "ok"}}


class _FakeHttpErrorResponse:
    def __init__(self, status_code: int = 400, body: str = '{"error":"bad request"}'):
        self.status_code = status_code
        self.text = body

    def raise_for_status(self):
        raise requests.HTTPError(
            f"{self.status_code} Client Error: Bad Request for url: https://example.com"
        )

    def json(self):
        return {"error": "bad request"}


class _FakeStreamResponse:
    def __init__(self, lines):
        self._lines = list(lines or [])

    def raise_for_status(self):
        return None

    def iter_lines(self):
        for line in self._lines:
            yield line


class ProviderContextWindowTests(unittest.TestCase):
    @staticmethod
    def _sample_tool_schema():
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read one file",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                        },
                        "required": ["path"],
                    },
                },
            }
        ]

    def test_openai_sends_context_window_header(self):
        with patch("requests.post", return_value=_FakeResponse()) as mock_post:
            out = call_ai_with_provider(
                context=ProviderCallContext(
                    provider="openai",
                    model_name="gpt-4o-mini",
                    model_params={"context_window": "64k"},
                    openai_conf={
                        "api_key": "k",
                        "base_url": "https://example.com/v1",
                        "context_window": "64k",
                    },
                    messages=[{"role": "user", "content": "hi"}],
                    stream=False,
                    return_message=False,
                    image_data=None,
                    image_user_idx=None,
                    image_user_text="",
                    session_summary_mode=False,
                    memory_query_expansion_mode=False,
                    domain_classifier_mode=False,
                ),
                append_history=lambda _s: None,
                ollama_importer=lambda: None,
            )
        self.assertEqual(out, "ok")
        headers = mock_post.call_args.kwargs.get("headers", {})
        self.assertEqual(headers.get("X-Context-Window"), "64000")

    def test_openai_invalid_context_window_uses_default_header(self):
        with patch("requests.post", return_value=_FakeResponse()) as mock_post:
            out = call_ai_with_provider(
                context=ProviderCallContext(
                    provider="openai",
                    model_name="gpt-oss-120b",
                    model_params={"context_window": "bad"},
                    openai_conf={
                        "api_key": "k",
                        "base_url": "https://example.com/v1",
                        "context_window": "bad",
                    },
                    messages=[{"role": "user", "content": "hi"}],
                    stream=False,
                    return_message=False,
                    image_data=None,
                    image_user_idx=None,
                    image_user_text="",
                    session_summary_mode=False,
                    memory_query_expansion_mode=False,
                    domain_classifier_mode=False,
                ),
                append_history=lambda _s: None,
                ollama_importer=lambda: None,
            )
        self.assertEqual(out, "ok")
        headers = mock_post.call_args.kwargs.get("headers", {})
        self.assertEqual(headers.get("X-Context-Window"), "128000")

    def test_openai_supports_responses_api_output_shape(self):
        with patch("requests.post", return_value=_FakeResponsesApiResponse()):
            out = call_ai_with_provider(
                context=ProviderCallContext(
                    provider="openai",
                    model_name="gpt-oss-120b",
                    model_params={"context_window": 128000},
                    openai_conf={
                        "api_key": "k",
                        "base_url": "https://example.com/v1",
                        "context_window": 128000,
                    },
                    messages=[{"role": "user", "content": "hi"}],
                    stream=False,
                    return_message=False,
                    image_data=None,
                    image_user_idx=None,
                    image_user_text="",
                    session_summary_mode=False,
                    memory_query_expansion_mode=False,
                    domain_classifier_mode=False,
                ),
                append_history=lambda _s: None,
                ollama_importer=lambda: None,
            )
        self.assertEqual(out, "hello from responses api")

    def test_openai_normalizes_typed_history_content_to_plain_text(self):
        with patch("requests.post", return_value=_FakeResponse()) as mock_post:
            out = call_ai_with_provider(
                context=ProviderCallContext(
                    provider="openai",
                    model_name="gpt-oss-120b",
                    model_params={},
                    openai_conf={
                        "api_key": "k",
                        "base_url": "https://example.com/v1",
                    },
                    messages=[
                        {"role": "user", "content": [{"type": "input_text", "text": "u1"}]},
                        {"role": "assistant", "content": [{"type": "output_text", "text": "a1"}]},
                        {"role": "user", "content": "u2"},
                    ],
                    stream=False,
                    return_message=False,
                    image_data=None,
                    image_user_idx=None,
                    image_user_text="",
                    session_summary_mode=False,
                    memory_query_expansion_mode=False,
                    domain_classifier_mode=False,
                ),
                append_history=lambda _s: None,
                ollama_importer=lambda: None,
            )
        self.assertEqual(out, "ok")
        payload = mock_post.call_args.kwargs.get("json", {})
        sent = payload.get("messages", [])
        self.assertEqual(sent[0]["content"], "u1")
        self.assertEqual(sent[1]["content"], "a1")
        self.assertEqual(sent[2]["content"], "u2")

    def test_openai_chat_mode_appends_chat_completions_suffix(self):
        with patch("requests.post", return_value=_FakeResponse()) as mock_post:
            out = call_ai_with_provider(
                context=ProviderCallContext(
                    provider="openai",
                    model_name="gpt-oss-120b",
                    model_params={},
                    openai_conf={
                        "api_key": "k",
                        "base_url": "https://example.com/v1",
                        "api_mode": "chat",
                    },
                    messages=[{"role": "user", "content": "ping"}],
                    stream=False,
                    return_message=False,
                    image_data=None,
                    image_user_idx=None,
                    image_user_text="",
                    session_summary_mode=False,
                    memory_query_expansion_mode=False,
                    domain_classifier_mode=False,
                ),
                append_history=lambda _s: None,
                ollama_importer=lambda: None,
            )
        self.assertEqual(out, "ok")
        self.assertEqual(
            mock_post.call_args.args[0], "https://example.com/v1/chat/completions"
        )
        payload = mock_post.call_args.kwargs.get("json", {})
        self.assertIn("messages", payload)
        self.assertNotIn("input", payload)

    def test_openai_responses_mode_appends_responses_suffix(self):
        with patch("requests.post", return_value=_FakeResponsesApiResponse()) as mock_post:
            out = call_ai_with_provider(
                context=ProviderCallContext(
                    provider="openai",
                    model_name="Gemma-4-31B",
                    model_params={},
                    openai_conf={
                        "api_key": "k",
                        "base_url": "https://example.com/v1",
                        "api_mode": "responses",
                    },
                    messages=[{"role": "user", "content": "ping"}],
                    stream=False,
                    return_message=False,
                    image_data=None,
                    image_user_idx=None,
                    image_user_text="",
                    session_summary_mode=False,
                    memory_query_expansion_mode=False,
                    domain_classifier_mode=False,
                ),
                append_history=lambda _s: None,
                ollama_importer=lambda: None,
            )
        self.assertEqual(out, "hello from responses api")
        self.assertEqual(mock_post.call_args.args[0], "https://example.com/v1/responses")
        payload = mock_post.call_args.kwargs.get("json", {})
        self.assertIn("input", payload)
        self.assertNotIn("messages", payload)

    def test_openai_responses_mode_defaults_additional_drop_params_tools(self):
        with patch("requests.post", return_value=_FakeResponsesApiResponse()) as mock_post:
            out = call_ai_with_provider(
                context=ProviderCallContext(
                    provider="openai",
                    model_name="Gemma-4-31B",
                    model_params={},
                    openai_conf={
                        "api_key": "k",
                        "base_url": "https://example.com/v1",
                        "api_mode": "responses",
                    },
                    messages=[{"role": "user", "content": "ping"}],
                    stream=False,
                    return_message=False,
                    image_data=None,
                    image_user_idx=None,
                    image_user_text="",
                    session_summary_mode=False,
                    memory_query_expansion_mode=False,
                    domain_classifier_mode=False,
                ),
                append_history=lambda _s: None,
                ollama_importer=lambda: None,
            )
        self.assertEqual(out, "hello from responses api")
        payload = mock_post.call_args.kwargs.get("json", {})
        self.assertEqual(payload.get("additional_drop_params"), ["tools"])
        self.assertNotIn("tools", payload)

    def test_openai_responses_mode_merges_configured_additional_drop_params(self):
        with patch("requests.post", return_value=_FakeResponsesApiResponse()) as mock_post:
            out = call_ai_with_provider(
                context=ProviderCallContext(
                    provider="openai",
                    model_name="Gemma-4-31B",
                    model_params={},
                    openai_conf={
                        "api_key": "k",
                        "base_url": "https://example.com/v1",
                        "api_mode": "responses",
                        "additional_drop_params": ["tools"],
                    },
                    messages=[{"role": "user", "content": "ping"}],
                    stream=False,
                    return_message=False,
                    image_data=None,
                    image_user_idx=None,
                    image_user_text="",
                    session_summary_mode=False,
                    memory_query_expansion_mode=False,
                    domain_classifier_mode=False,
                ),
                append_history=lambda _s: None,
                ollama_importer=lambda: None,
            )
        self.assertEqual(out, "hello from responses api")
        payload = mock_post.call_args.kwargs.get("json", {})
        self.assertEqual(payload.get("additional_drop_params"), ["tools"])
        self.assertNotIn("tools", payload)

    def test_openai_chat_mode_supports_standard_tools_call(self):
        with patch("requests.post", return_value=_FakeChatToolsResponse()) as mock_post:
            out = call_ai_with_provider(
                context=ProviderCallContext(
                    provider="openai",
                    model_name="Gemma-4-31B",
                    model_params={},
                    openai_conf={
                        "api_key": "k",
                        "base_url": "https://example.com/v1",
                        "api_mode": "chat",
                    },
                    messages=[{"role": "user", "content": "please read readme"}],
                    stream=False,
                    return_message=True,
                    image_data=None,
                    image_user_idx=None,
                    image_user_text="",
                    session_summary_mode=False,
                    memory_query_expansion_mode=False,
                    domain_classifier_mode=False,
                    tool_schemas=self._sample_tool_schema(),
                    tool_choice="required",
                ),
                append_history=lambda _s: None,
                ollama_importer=lambda: None,
            )
        self.assertIsInstance(out, dict)
        self.assertIn("tool_calls", out)
        payload = mock_post.call_args.kwargs.get("json", {})
        self.assertIn("tools", payload)
        self.assertEqual(payload.get("tool_choice"), "required")
        self.assertEqual(
            payload.get("tools", [{}])[0].get("function", {}).get("name"),
            "read_file",
        )
        self.assertNotIn(
            "tools",
            payload.get("additional_drop_params", []),
        )

    def test_openai_responses_mode_supports_standard_tools_call(self):
        with patch("requests.post", return_value=_FakeResponsesToolsApiResponse()) as mock_post:
            out = call_ai_with_provider(
                context=ProviderCallContext(
                    provider="openai",
                    model_name="Gemma-4-31B",
                    model_params={},
                    openai_conf={
                        "api_key": "k",
                        "base_url": "https://example.com/v1",
                        "api_mode": "responses",
                        "additional_drop_params": ["tools", "stop"],
                    },
                    messages=[{"role": "user", "content": "please read readme"}],
                    stream=False,
                    return_message=True,
                    image_data=None,
                    image_user_idx=None,
                    image_user_text="",
                    session_summary_mode=False,
                    memory_query_expansion_mode=False,
                    domain_classifier_mode=False,
                    tool_schemas=self._sample_tool_schema(),
                    tool_choice="required",
                ),
                append_history=lambda _s: None,
                ollama_importer=lambda: None,
            )
        self.assertIsInstance(out, dict)
        self.assertIn("tool_calls", out)
        tool_calls = out.get("tool_calls") or []
        self.assertTrue(isinstance(tool_calls, list) and tool_calls)
        self.assertEqual(tool_calls[0].get("function", {}).get("name"), "read_file")

        payload = mock_post.call_args.kwargs.get("json", {})
        self.assertIn("tools", payload)
        self.assertEqual(payload.get("tool_choice"), "required")
        self.assertEqual(payload.get("tools", [{}])[0].get("name"), "read_file")
        self.assertNotIn(
            "tools",
            payload.get("additional_drop_params", []),
        )
        self.assertIn("stop", payload.get("additional_drop_params", []))

    def test_openai_append_fail_then_no_suffix_success_records_override(self):
        responses = [_FakeHttpErrorResponse(), _FakeResponse()]

        def _fake_post(*args, **kwargs):
            return responses.pop(0)

        with patch("requests.post", side_effect=_fake_post) as mock_post, patch.object(
            ai_provider_clients,
            "_openai_get_prefer_no_suffix",
            return_value=False,
        ), patch.object(ai_provider_clients, "_openai_set_prefer_no_suffix") as set_mock:
            out = call_ai_with_provider(
                context=ProviderCallContext(
                    provider="openai",
                    model_name="gpt-oss-120b",
                    model_params={},
                    openai_conf={
                        "api_key": "k",
                        "base_url": "https://example.com/v1",
                        "api_mode": "chat",
                    },
                    messages=[{"role": "user", "content": "ping"}],
                    stream=False,
                    return_message=False,
                    image_data=None,
                    image_user_idx=None,
                    image_user_text="",
                    session_summary_mode=False,
                    memory_query_expansion_mode=False,
                    domain_classifier_mode=False,
                ),
                append_history=lambda _s: None,
                ollama_importer=lambda: None,
            )
        self.assertEqual(out, "ok")
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(
            mock_post.call_args_list[0].args[0],
            "https://example.com/v1/chat/completions",
        )
        self.assertEqual(mock_post.call_args_list[1].args[0], "https://example.com/v1")
        set_mock.assert_called_once_with(
            base_url="https://example.com/v1",
            model_name="gpt-oss-120b",
            api_kind="chat",
            prefer_no_suffix=True,
        )

    def test_openai_no_suffix_fail_then_suffix_success_clears_override(self):
        responses = [_FakeHttpErrorResponse(), _FakeResponse()]

        def _fake_post(*args, **kwargs):
            return responses.pop(0)

        with patch("requests.post", side_effect=_fake_post) as mock_post, patch.object(
            ai_provider_clients,
            "_openai_get_prefer_no_suffix",
            return_value=True,
        ), patch.object(ai_provider_clients, "_openai_set_prefer_no_suffix") as set_mock:
            out = call_ai_with_provider(
                context=ProviderCallContext(
                    provider="openai",
                    model_name="gpt-oss-120b",
                    model_params={},
                    openai_conf={
                        "api_key": "k",
                        "base_url": "https://example.com/v1",
                        "api_mode": "chat",
                    },
                    messages=[{"role": "user", "content": "ping"}],
                    stream=False,
                    return_message=False,
                    image_data=None,
                    image_user_idx=None,
                    image_user_text="",
                    session_summary_mode=False,
                    memory_query_expansion_mode=False,
                    domain_classifier_mode=False,
                ),
                append_history=lambda _s: None,
                ollama_importer=lambda: None,
            )
        self.assertEqual(out, "ok")
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(mock_post.call_args_list[0].args[0], "https://example.com/v1")
        self.assertEqual(
            mock_post.call_args_list[1].args[0],
            "https://example.com/v1/chat/completions",
        )
        set_mock.assert_called_once_with(
            base_url="https://example.com/v1",
            model_name="gpt-oss-120b",
            api_kind="chat",
            prefer_no_suffix=False,
        )

    def test_openai_auto_prefers_responses_when_base_url_has_responses_suffix(self):
        responses = [_FakeHttpErrorResponse(), _FakeResponse()]

        def _fake_post(*args, **kwargs):
            return responses.pop(0)

        with patch("requests.post", side_effect=_fake_post) as mock_post, patch.object(
            ai_provider_clients, "_openai_get_prefer_no_suffix", return_value=False
        ), patch.object(ai_provider_clients, "_openai_set_prefer_no_suffix"):
            out = call_ai_with_provider(
                context=ProviderCallContext(
                    provider="openai",
                    model_name="gpt-oss-120b",
                    model_params={},
                    openai_conf={
                        "api_key": "k",
                        "base_url": "https://example.com/v1/responses",
                        "api_mode": "auto",
                    },
                    messages=[{"role": "user", "content": "ping"}],
                    stream=False,
                    return_message=False,
                    image_data=None,
                    image_user_idx=None,
                    image_user_text="",
                    session_summary_mode=False,
                    memory_query_expansion_mode=False,
                    domain_classifier_mode=False,
                ),
                append_history=lambda _s: None,
                ollama_importer=lambda: None,
            )
        self.assertEqual(out, "ok")
        self.assertGreaterEqual(mock_post.call_count, 2)
        self.assertEqual(mock_post.call_args_list[0].args[0], "https://example.com/v1/responses")
        self.assertEqual(
            mock_post.call_args_list[1].args[0],
            "https://example.com/v1/responses/chat/completions",
        )

    def test_ollama_passes_num_ctx(self):
        fake_ollama = _FakeOllama()
        out = call_ai_with_provider(
            context=ProviderCallContext(
                provider="ollama",
                model_name="qwen2.5:14b",
                model_params={"context_window": "96K"},
                openai_conf=None,
                messages=[{"role": "user", "content": "hi"}],
                stream=False,
                return_message=False,
                image_data=None,
                image_user_idx=None,
                image_user_text="",
                session_summary_mode=False,
                memory_query_expansion_mode=False,
                domain_classifier_mode=False,
            ),
            append_history=lambda _s: None,
            ollama_importer=lambda: fake_ollama,
        )
        self.assertEqual(out, "ok")
        self.assertEqual(fake_ollama.calls[0]["options"]["num_ctx"], 96000)

    def test_ollama_stream_summary_keeps_num_ctx_and_summary_options(self):
        fake_ollama = _FakeOllama()
        chunks = call_ai_with_provider(
            context=ProviderCallContext(
                provider="ollama",
                model_name="qwen2.5:14b",
                model_params={"context_window": 128000},
                openai_conf=None,
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
                return_message=False,
                image_data=None,
                image_user_idx=None,
                image_user_text="",
                session_summary_mode=True,
                memory_query_expansion_mode=False,
                domain_classifier_mode=False,
            ),
            append_history=lambda _s: None,
            ollama_importer=lambda: fake_ollama,
        )
        self.assertEqual("".join(list(chunks)), "hello world")
        options = fake_ollama.calls[0]["options"]
        self.assertEqual(options["num_ctx"], 128000)
        self.assertEqual(options["num_predict"], 512)
        self.assertEqual(options["temperature"], 0.3)

    def test_openai_stream_ignores_reasoning_delta_and_only_records_output_text(self):
        stream_resp = _FakeStreamResponse(
            [
                b'data: {"type":"response.reasoning.delta","delta":"internal thinking"}',
                b'data: {"type":"response.output_text.delta","delta":" Hel"}',
                b'data: {"type":"response.output_text.delta","delta":"lo"}',
                b"data: [DONE]",
            ]
        )
        history = []
        with patch("requests.post", return_value=stream_resp):
            chunks = call_ai_with_provider(
                context=ProviderCallContext(
                    provider="openai",
                    model_name="gpt-oss-120b",
                    model_params={},
                    openai_conf={
                        "api_key": "k",
                        "base_url": "https://example.com/v1",
                        "api_mode": "responses",
                    },
                    messages=[{"role": "user", "content": "ping"}],
                    stream=True,
                    return_message=False,
                    image_data=None,
                    image_user_idx=None,
                    image_user_text="",
                    session_summary_mode=False,
                    memory_query_expansion_mode=False,
                    domain_classifier_mode=False,
                ),
                append_history=lambda s: history.append(s),
                ollama_importer=lambda: None,
            )
        self.assertEqual("".join(list(chunks)), "Hello")
        self.assertEqual(history, ["Hello"])


if __name__ == "__main__":
    unittest.main()
