import sys
import types
import unittest

if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.agent import Agent


class _FakeOrchestrator:
    def __init__(self):
        self.context = types.SimpleNamespace(
            provider="",
            model_name="",
            model_params=None,
            openai_conf=None,
            work_directory="",
        )
        self.last_call_ctx = None

    def call(self, *, call_ctx):
        self.last_call_ctx = call_ctx
        return "ok"


class AgentCallAiStreamingTests(unittest.TestCase):
    def setUp(self):
        self.agent = Agent.__new__(Agent)
        self.agent.provider = "openai"
        self.agent.model_name = "gpt-4o-mini"
        self.agent.params = {}
        self.agent.openai_conf = {"api_key": "k"}
        self.agent.work_directory = "."
        self.agent.ai_orchestrator = _FakeOrchestrator()

    def test_call_ai_defaults_to_model_streaming_true(self):
        self.agent.params = {"streaming": True}
        out = self.agent.call_ai("hello")
        self.assertEqual(out, "ok")
        self.assertTrue(self.agent.ai_orchestrator.last_call_ctx.stream)

    def test_call_ai_defaults_to_model_streaming_false(self):
        self.agent.params = {"streaming": "false"}
        self.agent.call_ai("hello")
        self.assertFalse(self.agent.ai_orchestrator.last_call_ctx.stream)

    def test_call_ai_explicit_stream_overrides_model_setting(self):
        self.agent.params = {"streaming": False}
        self.agent.call_ai("hello", stream=True)
        self.assertTrue(self.agent.ai_orchestrator.last_call_ctx.stream)

    def test_standard_tools_mode_is_enabled_for_ollama(self):
        self.agent.provider = "ollama"
        self.agent.params = {}
        self.assertTrue(self.agent._use_standard_openai_tools_call())

    def test_standard_tools_mode_is_disabled_for_ollama_when_simulated_tools_enabled(self):
        self.agent.provider = "ollama"
        self.agent.params = {"use_simulated_tools": True}
        self.assertFalse(self.agent._use_standard_openai_tools_call())

    def test_model_switch_warns_when_context_window_is_below_64k(self):
        agent = Agent.__new__(Agent)
        agent.provider = "openai"
        agent.model_name = "large"
        agent.params = {"context_window": 128000}
        agent._find_configured_model_choice = lambda selector: {
            "provider": "openai",
            "name": "tiny",
            "selector": "openai:tiny",
            "params": {
                "model": "tiny",
                "context_window": 32000,
            },
        }
        agent._current_model_selector = lambda: "openai:large"
        agent._set_active_chat_model = lambda *_args, **_kwargs: None
        agent._refresh_status_context_usage_snapshot = lambda: None

        def _apply_choice(choice, validate=False):
            _ = validate
            agent.provider = str(choice.get("provider") or "")
            agent.model_name = str(choice.get("name") or "")
            agent.params = dict(choice.get("params") or {})

        agent._apply_runtime_model_choice = _apply_choice

        out = agent._switch_model_by_selector("openai:tiny")

        self.assertIn("✅ Switched model: openai:tiny", out)
        self.assertIn(
            "\n\n⚠️ Model context window is too small; only basic chat is supported.\n",
            out,
        )


if __name__ == "__main__":
    unittest.main()
