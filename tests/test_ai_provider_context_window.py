import unittest
from unittest.mock import patch

from src.ai.ai_provider_clients import ProviderCallContext, call_ai_with_provider


class _FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


class _FakeOllama:
    def __init__(self):
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return [{"message": {"content": "hello"}}, {"message": {"content": " world"}}]
        return {"message": {"content": "ok"}}


class ProviderContextWindowTests(unittest.TestCase):
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
                    openwebui_conf=None,
                    messages=[{"role": "user", "content": "hi"}],
                    stream=False,
                    return_message=False,
                    image_data=None,
                    image_user_idx=None,
                    image_user_text="",
                    session_summary_mode=False,
                    memory_query_expansion_mode=False,
                ),
                append_history=lambda _s: None,
                ollama_importer=lambda: None,
            )
        self.assertEqual(out, "ok")
        headers = mock_post.call_args.kwargs.get("headers", {})
        self.assertEqual(headers.get("X-Context-Window"), "64000")

    def test_openwebui_invalid_context_window_uses_default_header(self):
        with patch("requests.post", return_value=_FakeResponse()) as mock_post:
            out = call_ai_with_provider(
                context=ProviderCallContext(
                    provider="openwebui",
                    model_name="gpt-oss-120b",
                    model_params={"context_window": "bad"},
                    openai_conf=None,
                    openwebui_conf={
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
                ),
                append_history=lambda _s: None,
                ollama_importer=lambda: None,
            )
        self.assertEqual(out, "ok")
        headers = mock_post.call_args.kwargs.get("headers", {})
        self.assertEqual(headers.get("X-Context-Window"), "128000")

    def test_ollama_passes_num_ctx(self):
        fake_ollama = _FakeOllama()
        out = call_ai_with_provider(
            context=ProviderCallContext(
                provider="ollama",
                model_name="qwen2.5:14b",
                model_params={"context_window": "96K"},
                openai_conf=None,
                openwebui_conf=None,
                messages=[{"role": "user", "content": "hi"}],
                stream=False,
                return_message=False,
                image_data=None,
                image_user_idx=None,
                image_user_text="",
                session_summary_mode=False,
                memory_query_expansion_mode=False,
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
                openwebui_conf=None,
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
                return_message=False,
                image_data=None,
                image_user_idx=None,
                image_user_text="",
                session_summary_mode=True,
                memory_query_expansion_mode=False,
            ),
            append_history=lambda _s: None,
            ollama_importer=lambda: fake_ollama,
        )
        self.assertEqual("".join(list(chunks)), "hello world")
        options = fake_ollama.calls[0]["options"]
        self.assertEqual(options["num_ctx"], 128000)
        self.assertEqual(options["num_predict"], 512)
        self.assertEqual(options["temperature"], 0.3)


if __name__ == "__main__":
    unittest.main()
