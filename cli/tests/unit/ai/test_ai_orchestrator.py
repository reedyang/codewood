import unittest
from unittest.mock import patch

from cli.ai.ai_provider_clients import AIResult
from cli.ai.ai_orchestrator import AgentAIContext, AIOrchestrator
from cli.ai.ai_provider_clients import AICallContext, ModelCallError


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


if __name__ == "__main__":
    unittest.main()
