import unittest
from unittest.mock import patch

from src.ai.ai_orchestrator import AgentAIContext, AIOrchestrator
from src.ai.ai_provider_clients import AICallContext


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
            work_directory=".",
            history_writer=lambda role, content: history.append((role, content)),
            regular_message_builder=_regular_builder,
            ollama_importer=lambda: None,
        )
        orchestrator = AIOrchestrator(ctx)

        def _fake_provider_call(*, context, append_history, ollama_importer):
            self.assertEqual(context.messages, override_messages)
            append_history("compact summary")
            return "compact summary"

        with patch("src.ai.ai_orchestrator.call_ai_with_provider", _fake_provider_call):
            result = orchestrator.call(
                call_ctx=AICallContext(
                    user_input="ignored",
                    messages_override=override_messages,
                    record_history_override=False,
                )
            )

        self.assertEqual(result, "compact summary")
        self.assertEqual(history, [])

    def test_minimal_classifier_uses_real_work_directory(self):
        captured_messages = []

        def _regular_builder(_user_input, _context):
            raise AssertionError("regular builder should not be called")

        ctx = AgentAIContext(
            provider="openai",
            model_name="test-model",
            model_params={},
            openai_conf={"api_key": "x"},
            work_directory="D:/SourceCode/opensource/smart-shell",
            history_writer=lambda _role, _content: None,
            regular_message_builder=_regular_builder,
            ollama_importer=lambda: None,
        )
        orchestrator = AIOrchestrator(ctx)

        def _fake_provider_call(*, context, append_history, ollama_importer):
            _ = append_history, ollama_importer
            captured_messages.extend(context.messages)
            return "ok"

        with patch("src.ai.ai_orchestrator.call_ai_with_provider", _fake_provider_call):
            result = orchestrator.call(
                call_ctx=AICallContext(
                    user_input='{"command":"python -c \\"print(1)\\""}',
                    minimal_classifier=True,
                    stream=False,
                )
            )

        self.assertEqual(result, "ok")
        joined = "\n".join(str(msg.get("content") or "") for msg in captured_messages)
        self.assertIn("Current working directory: D:/SourceCode/opensource/smart-shell", joined)

    def test_empty_assistant_response_is_not_written_to_history(self):
        history = []

        def _regular_builder(user_input, _context):
            return [{"role": "user", "content": user_input}], True

        ctx = AgentAIContext(
            provider="openai",
            model_name="test-model",
            model_params={},
            openai_conf={"api_key": "x"},
            work_directory=".",
            history_writer=lambda role, content: history.append((role, content)),
            regular_message_builder=_regular_builder,
            ollama_importer=lambda: None,
        )
        orchestrator = AIOrchestrator(ctx)

        def _fake_provider_call(*, context, append_history, ollama_importer):
            _ = context, ollama_importer
            append_history("")
            return ""

        with patch("src.ai.ai_orchestrator.call_ai_with_provider", _fake_provider_call):
            result = orchestrator.call(
                call_ctx=AICallContext(user_input="hello", stream=True)
            )

        self.assertEqual(result, "")
        self.assertEqual(history, [("user", "hello")])


if __name__ == "__main__":
    unittest.main()
