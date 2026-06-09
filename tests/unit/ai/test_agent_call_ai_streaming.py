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
        self.agent.params = {"context_window": 64000}
        self.assertTrue(self.agent._use_standard_openai_tools_call())

    def test_standard_tools_mode_is_enabled_for_openai(self):
        self.agent.provider = "openai"
        self.agent.params = {"context_window": 64000}
        self.assertTrue(self.agent._use_standard_openai_tools_call())

    def test_standard_tools_mode_is_disabled_below_64k(self):
        self.agent.provider = "openai"
        self.agent.params = {"context_window": 63999}
        self.assertFalse(self.agent._use_standard_openai_tools_call())

    def test_standard_tools_mode_uses_default_context_window_when_missing(self):
        self.agent.provider = "ollama"
        self.agent.params = {}
        self.assertTrue(self.agent._use_standard_openai_tools_call())

    def test_standard_tools_mode_is_enabled_regardless_of_provider_label(self):
        # ``provider`` is now a free-form selector-prefix label, so
        # the standard-tool-calls capability check must NOT depend
        # on a hard-coded ``{"openai", "ollama"}`` allow-list. Any
        # provider name with a sufficient context window opts into
        # standard tool_calls.
        for label in ("nvidia", "my-corp-gateway", "azure", "", "deepseek"):
            self.agent.provider = label
            self.agent.params = {"context_window": 128000}
            self.assertTrue(
                self.agent._use_standard_openai_tools_call(),
                f"expected standard tools for provider label {label!r}",
            )

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

    def test_model_switch_applies_and_clears_model_level_extra_headers(self):
        agent = Agent.__new__(Agent)
        agent.provider = "openai"
        agent.model_name = "plain"
        agent.params = {
            "api_key": "k",
            "base_url": "https://example.test/v1",
            "model": "plain",
            "models": ["plain", "needs-header"],
            "extra_headers": {},
        }
        agent.openai_conf = agent.params
        agent.ai_orchestrator = _FakeOrchestrator()
        agent._set_active_chat_model = lambda *_args, **_kwargs: None
        agent._refresh_status_context_usage_snapshot = lambda: None
        agent._load_runtime_config_data = lambda: {
            "model_providers": [
                {
                    "provider": "openai",
                    "params": {
                        "api_key": "k",
                        "base_url": "https://example.test/v1",
                        "models": [
                            {"name": "plain"},
                            {
                                "name": "needs-header",
                                "extra_headers": {"X-Model": "needs-header"},
                            },
                        ],
                    },
                }
            ]
        }

        out = agent._switch_model_by_selector("openai:needs-header")

        self.assertIn("Switched model: openai:needs-header", out)
        self.assertEqual(agent.params.get("model"), "needs-header")
        self.assertEqual(agent.params.get("extra_headers"), {"X-Model": "needs-header"})
        self.assertIs(agent.openai_conf, agent.params)
        self.assertEqual(
            agent.ai_orchestrator.context.openai_conf.get("extra_headers"),
            {"X-Model": "needs-header"},
        )

        agent._switch_model_by_selector("openai:plain")

        self.assertEqual(agent.params.get("model"), "plain")
        self.assertEqual(agent.params.get("extra_headers"), {})
        self.assertEqual(agent.ai_orchestrator.context.openai_conf.get("extra_headers"), {})


if __name__ == "__main__":
    unittest.main()
