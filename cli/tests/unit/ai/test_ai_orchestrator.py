import unittest
from unittest.mock import patch

from cli.ai.ai_provider_clients import AIResult
from cli.ai.ai_orchestrator import AgentAIContext, AIOrchestrator, _extract_clean_api_error
from cli.ai.ai_provider_clients import (
    AICallContext,
    ModelCallError,
    _is_internal_response_format_error,
)


class AIOrchestratorTests(unittest.TestCase):
    def test_messages_override_skips_regular_builder_and_history(self):
        history = []
        override_messages = [
            {"role": "system", "content": "compact system"},
            {"role": "user", "content": "compact input"},
        ]

        def _regular_builder(_user_input, _context):
            raise AssertionError("regular builder should not be called")

        ctx = AgentAIContext(
            provider="openai",
            model_name="test-model",
            model_params={},
            openai_conf={"api_key": "x"},
            history_writer=lambda role, content: history.append((role, content)),
            regular_message_builder=_regular_builder,
            ollama_importer=lambda: None,
        )
        orchestrator = AIOrchestrator(ctx)

        def _fake_provider_call(*, context, append_history, ollama_importer):
            self.assertEqual(context.messages, override_messages)
            append_history("compact summary")
            return "compact summary"

        with patch("cli.ai.ai_orchestrator.call_ai_with_provider", _fake_provider_call):
            result = orchestrator.call(
                call_ctx=AICallContext(
                    user_input="ignored",
                    messages_override=override_messages,
                    record_history_override=False,
                )
            )

        self.assertEqual(result.text, "compact summary")
        self.assertEqual(history, [])

    def test_empty_assistant_response_is_not_written_to_history(self):
        history = []

        def _regular_builder(user_input, _context):
            return [{"role": "user", "content": user_input}], True

        ctx = AgentAIContext(
            provider="openai",
            model_name="test-model",
            model_params={},
            openai_conf={"api_key": "x"},
            history_writer=lambda role, content: history.append((role, content)),
            regular_message_builder=_regular_builder,
            ollama_importer=lambda: None,
        )
        orchestrator = AIOrchestrator(ctx)

        def _fake_provider_call(*, context, append_history, ollama_importer):
            _ = context, ollama_importer
            append_history("")
            return ""

        with patch("cli.ai.ai_orchestrator.call_ai_with_provider", _fake_provider_call):
            result = orchestrator.call(
                call_ctx=AICallContext(user_input="hello", stream=True)
            )

        self.assertEqual(result.text, "")
        self.assertEqual(history, [("user", "hello")])


    def test_model_call_error_emits_full_trail_via_ephemeral_writer_only(self):
        """When every retry strategy fails, the orchestrator surfaces
        a clean API error message through the ephemeral on-screen channel
        without persisting anything to chat history."""
        history = []
        notices = []

        def _regular_builder(user_input, _context):
            return [{"role": "user", "content": user_input}], True

        ctx = AgentAIContext(
            provider="openai",
            model_name="test-model",
            model_params={},
            openai_conf={"api_key": "x"},
            history_writer=lambda role, content: history.append((role, content)),
            regular_message_builder=_regular_builder,
            ollama_importer=lambda: None,
            ephemeral_notice_writer=notices.append,
        )
        orchestrator = AIOrchestrator(ctx)

        attempts = [
            {"label": "responses with-suffix", "url": "https://x/v1/responses", "error": "404 Not Found"},
            {"label": "responses no-suffix", "url": "https://x/v1", "error": "405 Method Not Allowed"},
            {"label": "chat with-suffix", "url": "https://x/v1/chat/completions", "error": "404 Not Found"},
        ]

        def _raise_full_trail(*, context, append_history, ollama_importer):
            _ = context, append_history, ollama_importer
            raise ModelCallError("405 Method Not Allowed", attempt_errors=attempts)

        with patch("cli.ai.ai_orchestrator.call_ai_with_provider", _raise_full_trail):
            result = orchestrator.call(call_ctx=AICallContext(user_input="hello", stream=False))

        self.assertIsInstance(result, AIResult)
        self.assertEqual(result.error_code, "API_ERROR")
        self.assertEqual(history, [], "model-call errors must not be persisted to chat history")
        self.assertEqual(len(notices), 1, "ephemeral notice must be emitted exactly once")
        self.assertIn("Not Found", notices[0])

    def test_internal_call_api_error_is_silent(self):
        # Best-effort internal calls (memory query expansion, chat title, ...)
        # must NOT surface their failures as user-visible API-error messages:
        # no ephemeral notice and no persisted model-call-error history entry
        # (which the GUI renders as the red API error after a stop).
        from cli.ai.ai_special_mode_prompts import InternalCallMode

        history = []
        notices = []
        recorded = []

        def _regular_builder(user_input, _context):
            return [{"role": "user", "content": user_input}], True

        ctx = AgentAIContext(
            provider="openai",
            model_name="test-model",
            model_params={},
            openai_conf={"api_key": "x"},
            history_writer=lambda role, content: history.append((role, content)),
            regular_message_builder=_regular_builder,
            ollama_importer=lambda: None,
            ephemeral_notice_writer=notices.append,
            model_error_history_writer=recorded.append,
        )
        orchestrator = AIOrchestrator(ctx)

        def _raise_throttled(*, context, append_history, ollama_importer):
            _ = context, append_history, ollama_importer
            raise ModelCallError(
                "429 Client Error: Too Many Requests for url: http://x; "
                'response_body={"error":{"message":"余额不足","code":"1113"}}',
                attempt_errors=[{"label": "chat", "url": "http://x", "error": "429"}],
            )

        with patch("cli.ai.ai_orchestrator.call_ai_with_provider", _raise_throttled):
            result = orchestrator.call(
                call_ctx=AICallContext(
                    user_input="hello",
                    stream=False,
                    internal_mode=InternalCallMode.MEMORY_QUERY_EXPANSION,
                )
            )

        self.assertIsInstance(result, AIResult)
        self.assertEqual(result.error_code, "API_ERROR")
        self.assertEqual(notices, [], "internal calls must not emit ephemeral notices")
        self.assertEqual(recorded, [], "internal calls must not record error history")
        self.assertEqual(history, [])

    def test_clean_api_error_skips_fallback_attempt(self):
        attempts = [
            {
                "label": "responses with-suffix",
                "url": "https://x/v1/responses",
                "error": (
                    "Unsupported OpenAI response format: expected 'choices' or "
                    "Responses API 'output'. Top-level keys: [output, status, usage]"
                ),
            },
            {
                "label": "responses no-suffix",
                "url": "https://x/v1",
                "error": (
                    "404 Client Error: Not Found for url: https://x/v1; "
                    'response_body={"error":{"message":"File Not Found",'
                    '"type":"not_found_error","code":404}}'
                ),
                "fallback": "1",
            },
        ]
        error = ModelCallError(str(attempts[0]["error"]), attempt_errors=attempts)
        clean = _extract_clean_api_error(error)
        self.assertEqual(clean, "")
        self.assertTrue(_is_internal_response_format_error(str(error)))

    def test_response_format_error_is_not_shown_on_ui(self):
        history = []
        notices = []
        recorded = []

        def _regular_builder(user_input, _context):
            return [{"role": "user", "content": user_input}], True

        ctx = AgentAIContext(
            provider="openai",
            model_name="test-model",
            model_params={},
            openai_conf={"api_key": "x"},
            history_writer=lambda role, content: history.append((role, content)),
            regular_message_builder=_regular_builder,
            ollama_importer=lambda: None,
            ephemeral_notice_writer=notices.append,
            model_error_history_writer=recorded.append,
        )
        orchestrator = AIOrchestrator(ctx)
        format_error = (
            "Unsupported OpenAI response format: expected 'choices' or "
            "Responses API 'output'. Top-level keys: [completed_at, created_at, "
            "id, model, object, output, status, usage]"
        )

        def _raise_format_error(*, context, append_history, ollama_importer):
            _ = context, append_history, ollama_importer
            raise ModelCallError(
                format_error,
                attempt_errors=[{"label": "responses", "url": "http://x/v1/responses", "error": format_error}],
            )

        with patch("cli.ai.ai_orchestrator.call_ai_with_provider", _raise_format_error):
            result = orchestrator.call(call_ctx=AICallContext(user_input="hello", stream=False))

        self.assertEqual(result.error_code, "API_ERROR")
        self.assertEqual(notices, [])
        self.assertEqual(recorded, [])
        self.assertEqual(history, [])


if __name__ == "__main__":
    unittest.main()
