import sys
import threading
import time
import types
import unittest

if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from cli.agent import Agent
from cli.ai.ai_provider_clients import (
    AICallContext,
    AIResult,
    _ThreadedInterruptibleStream,
)


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
        return AIResult(text="ok")


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
        # Streaming calls are returned as a threaded iterable (the network work
        # runs on a background thread so interrupt is immediate); draining it
        # runs the underlying call and records the resolved streaming flag.
        self.agent.params = {"streaming": True}
        out = self.agent.call_ai("hello")
        self.assertTrue(hasattr(out, "__iter__"))
        list(out)
        self.assertTrue(self.agent.ai_orchestrator.last_call_ctx.stream)

    def test_call_ai_defaults_to_model_streaming_false(self):
        self.agent.params = {"streaming": "false"}
        self.agent.call_ai("hello")
        self.assertFalse(self.agent.ai_orchestrator.last_call_ctx.stream)

    def test_call_ai_explicit_stream_overrides_model_setting(self):
        self.agent.params = {"streaming": False}
        out = self.agent.call_ai("hello", stream=True)
        self.assertTrue(hasattr(out, "__iter__"))
        list(out)
        self.assertTrue(self.agent.ai_orchestrator.last_call_ctx.stream)

    def test_internal_calls_get_zero_retry_budget(self):
        # Best-effort internal calls (memory query expansion, chat title, ...)
        # must never enter the endless 429/503 backoff: they run nested inside
        # user-facing turns and would otherwise stall the whole turn forever.
        from cli.ai.ai_special_mode_prompts import InternalCallMode

        self.agent.params = {"streaming": False}
        self.agent.call_ai("expand", internal_mode=InternalCallMode.MEMORY_QUERY_EXPANSION)
        self.assertEqual(self.agent.ai_orchestrator.last_call_ctx.max_retries, 0)

        self.agent.call_ai("title", internal_mode=InternalCallMode.CHAT_TITLE)
        self.assertEqual(self.agent.ai_orchestrator.last_call_ctx.max_retries, 0)

        self.agent.call_ai("regular user turn")
        self.assertIsNone(self.agent.ai_orchestrator.last_call_ctx.max_retries)

    def test_standard_tools_mode_is_enabled_for_ollama(self):
        self.agent.provider = "ollama"
        self.agent.params = {"context_window": 64000}
        self.assertTrue(self.agent._use_standard_openai_tools_call())

    def test_standard_tools_mode_is_enabled_for_openai(self):
        self.agent.provider = "openai"
        self.agent.params = {"context_window": 64000}
        self.assertTrue(self.agent._use_standard_openai_tools_call())

    def test_standard_tools_mode_uses_default_context_window_when_missing(self):
        self.agent.provider = "ollama"
        self.agent.params = {}
        self.assertTrue(self.agent._use_standard_openai_tools_call())

    def test_find_configured_model_choice_uses_slash_selector(self):
        choice = {
            "provider": "openai",
            "name": "gpt-4o",
            "selector": "openai/gpt-4o",
            "params": {"model": "gpt-4o"},
        }
        self.agent._get_configured_model_catalog = lambda: [choice]
        self.assertEqual(
            self.agent._find_configured_model_choice("openai/gpt-4o"),
            choice,
        )

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

    def test_model_switch_returns_success_message(self):
        agent = Agent.__new__(Agent)
        agent.provider = "openai"
        agent.model_name = "large"
        agent.params = {"context_window": 128000}
        agent._find_configured_model_choice = lambda selector: {
            "provider": "openai",
            "name": "tiny",
            "selector": "openai/tiny",
            "params": {
                "model": "tiny",
                "context_window": 32000,
            },
        }
        agent._current_model_selector = lambda: "openai/large"
        agent._set_active_chat_model = lambda *_args, **_kwargs: None
        agent._refresh_status_context_usage_snapshot = lambda: None

        def _apply_choice(choice, validate=False):
            _ = validate
            agent.provider = str(choice.get("provider") or "")
            agent.model_name = str(choice.get("name") or "")
            agent.params = dict(choice.get("params") or {})

        agent._apply_runtime_model_choice = _apply_choice

        out = agent._switch_model_by_selector("openai/tiny")

        self.assertIn("✅ Switched model: openai/tiny", out)

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

        out = agent._switch_model_by_selector("openai/needs-header")

        self.assertIn("Switched model: openai/needs-header", out)
        self.assertEqual(agent.params.get("model"), "needs-header")
        self.assertEqual(agent.params.get("extra_headers"), {"X-Model": "needs-header"})
        self.assertIs(agent.openai_conf, agent.params)
        self.assertEqual(
            agent.ai_orchestrator.context.openai_conf.get("extra_headers"),
            {"X-Model": "needs-header"},
        )

        agent._switch_model_by_selector("openai/plain")

        self.assertEqual(agent.params.get("model"), "plain")
        self.assertEqual(agent.params.get("extra_headers"), {})
        self.assertEqual(agent.ai_orchestrator.context.openai_conf.get("extra_headers"), {})

    def test_call_uses_session_snapshot_even_when_shared_context_is_mutated_by_another_chat(self):
        # Regression: a background chat's model call must not pick up a config
        # that a concurrent chat activation wrote into the shared orchestrator
        # context. The call carries its own resolved snapshot (provider, model,
        # base_url) so a chat switch can't pair chat A's model name with chat
        # B's server address.
        agent = Agent.__new__(Agent)
        agent.work_directory = "."
        agent._install_session_registry()
        agent._session_for_key("")
        agent.ai_orchestrator = _FakeOrchestrator()
        # "Loop thread" bound to chat-a, whose session is pinned to model A.
        agent._bind_session("chat-a", "ws-1")
        agent.provider = "provA"
        agent.model_name = "model-a"
        agent.params = {"api_key": "k", "base_url": "https://api-a.example.com"}
        agent.openai_conf = dict(agent.params)
        agent._pin_session_model()
        # Simulate a concurrent chat activation (switch to chat B) clobbering
        # the shared orchestrator context with chat B's server config.
        agent.ai_orchestrator.context.provider = "provB"
        agent.ai_orchestrator.context.model_name = "model-b"
        agent.ai_orchestrator.context.model_params = {"base_url": "https://api-b.example.com"}
        agent.ai_orchestrator.context.openai_conf = {"base_url": "https://api-b.example.com"}

        agent._call_orchestrator(AICallContext(user_input="hello", stream=False))

        ctx = agent.ai_orchestrator.last_call_ctx
        # The call used its OWN resolved snapshot (chat A), not the shared
        # context that a concurrent chat switch had clobbered with chat B's
        # server address.
        self.assertEqual(ctx.provider, "provA")
        self.assertEqual(ctx.model_name, "model-a")
        self.assertEqual((ctx.openai_conf or {}).get("base_url"), "https://api-a.example.com")


class ThreadedInterruptibleStreamTests(unittest.TestCase):
    def test_yields_items_produced_on_background_thread(self):
        bridge = _ThreadedInterruptibleStream(lambda: iter(["a", "b", "c"]))
        self.assertEqual(list(bridge), ["a", "b", "c"])

    def test_proxies_inner_stream_result_attributes(self):
        class _Inner:
            final_message = {"role": "assistant", "content": "hello"}
            thinking_text = "hidden"

            def __iter__(self):
                return iter(["h", "i"])

        bridge = _ThreadedInterruptibleStream(_Inner)
        self.assertEqual(list(bridge), ["h", "i"])
        self.assertEqual(bridge.final_message["content"], "hello")
        self.assertEqual(bridge.thinking_text, "hidden")

    def test_cancel_returns_immediately_even_while_network_call_is_stuck(self):
        started = threading.Event()
        release = threading.Event()
        cancel_flag = {"value": False}

        def stuck_producer():
            started.set()
            # Simulate a network connect/read that never returns.
            release.wait(10)
            return iter(["late"])

        bridge = _ThreadedInterruptibleStream(
            stuck_producer, should_cancel=lambda: cancel_flag["value"]
        )
        # The producer is running (blocked) on the background thread.
        self.assertTrue(started.wait(2))

        # User interrupts while the network call is stuck: __next__ must return
        # immediately (raise KeyboardInterrupt) WITHOUT waiting for the network
        # thread to finish.
        cancel_flag["value"] = True
        with self.assertRaises(KeyboardInterrupt):
            next(bridge)
        release.set()

    def test_should_cancel_consumed_flag_does_not_raise_when_not_cancelled(self):
        bridge = _ThreadedInterruptibleStream(lambda: iter(["ok"]), should_cancel=lambda: False)
        self.assertEqual(list(bridge), ["ok"])

    def test_producer_keyboard_interrupt_is_forwarded_without_crashing_thread(self):
        # The infinite retry loop raises KeyboardInterrupt on a user stop; the
        # daemon thread must not crash with an unhandled-exception traceback,
        # and the consumer must receive the interrupt from ``__next__``.
        def aborting_producer():
            raise KeyboardInterrupt

        bridge = _ThreadedInterruptibleStream(aborting_producer)
        with self.assertRaises(KeyboardInterrupt):
            list(bridge)

    def test_is_cancelled_stays_set_after_cancel(self):
        bridge = _ThreadedInterruptibleStream(lambda: iter(["a"]))
        self.assertFalse(bridge.is_cancelled())
        bridge.cancel()
        self.assertTrue(bridge.is_cancelled())
        # A consuming interrupt probe would go back to False after being read;
        # the persistent cancel event must remain set so a background retry
        # loop can observe the stop long after the consumer saw it.
        self.assertTrue(bridge.is_cancelled())

    def test_nested_call_on_producer_thread_inherits_cancel_event(self):
        # Regression: the memory query-expansion call runs nested on the
        # bridge's producer thread with no bridge of its own. When the user
        # stops, the main loop consumes the interrupt flag and cancels the
        # bridge — the nested call's only stop signal is the thread-local
        # cancel event inherited from the owning bridge.
        from cli.ai.ai_provider_clients import current_stream_cancel_event

        self.assertIsNone(current_stream_cancel_event())

        seen = {}

        def nested_probe_producer():
            seen["event"] = current_stream_cancel_event()
            return iter(["ok"])

        bridge = _ThreadedInterruptibleStream(nested_probe_producer)
        self.assertEqual(list(bridge), ["ok"])
        # The producer thread observed the owning bridge's cancel event.
        self.assertIs(seen["event"], bridge._cancel)
        self.assertIsNone(current_stream_cancel_event())

        bridge.cancel()
        self.assertTrue(seen["event"].is_set())

    def test_nested_retry_loop_aborts_when_bridge_cancelled(self):
        # End-to-end shape of the reported bug: a nested non-stream call with
        # its own infinite retry loop runs on the producer thread; the probe
        # (peek=False, no bridge of its own) must still observe the stop
        # through the inherited cancel event.
        from cli.ai.ai_provider_clients import current_stream_cancel_event

        state = {"peek": False, "stopped": False}

        def nested_should_cancel():
            inherited = current_stream_cancel_event()
            return state["peek"] or bool(inherited and inherited.is_set())

        def stuck_in_nested_retry():
            # The nested infinite retry loop: exits only via the probe.
            deadline = time.time() + 5
            while time.time() < deadline:
                if nested_should_cancel():
                    raise KeyboardInterrupt
                time.sleep(0.01)
            return iter(["never"])

        bridge = _ThreadedInterruptibleStream(
            stuck_in_nested_retry,
            should_cancel=lambda: state["stopped"],
        )
        # Simulate the user stop: the main loop consumes the interrupt flag
        # (state["peek"] stays False for the nested probe) and cancels the
        # bridge; the consumer polls its own should_cancel and unwinds.
        state["stopped"] = True
        bridge.cancel()
        with self.assertRaises(KeyboardInterrupt):
            list(bridge)


if __name__ == "__main__":
    unittest.main()
