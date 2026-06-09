"""Tests for ``resolve_api_mode`` — the single source of truth that
selects the API call method (OpenAI-compatible vs Ollama-native).

Background: the older code branched on ``provider`` strings,
hard-coding ``"openai"`` and ``"ollama"`` everywhere. The new design
makes ``api_mode`` the dispatch switch and reduces ``provider`` to a
free-form selector-prefix label (e.g. ``openai:gpt-4o``,
``nvidia:deepseek-v4``, ``local:my-model``). These tests pin down
the precedence rules so future refactors don't silently regress
either the new explicit semantics or the legacy ``provider:
"ollama"`` shortcut.
"""

import unittest

from src.ai.ai_provider_clients import (
    _normalize_openai_api_mode,
    resolve_api_mode,
)


class NormalizeOpenAIApiModeTests(unittest.TestCase):
    def test_empty_value_normalizes_to_auto(self):
        self.assertEqual(_normalize_openai_api_mode(""), "auto")
        self.assertEqual(_normalize_openai_api_mode(None), "auto")
        self.assertEqual(_normalize_openai_api_mode("   "), "auto")

    def test_auto_passthrough(self):
        self.assertEqual(_normalize_openai_api_mode("auto"), "auto")
        self.assertEqual(_normalize_openai_api_mode("AUTO"), "auto")

    def test_chat_aliases(self):
        for alias in ("chat", "chat_completions", "chat/completions", "completions"):
            self.assertEqual(_normalize_openai_api_mode(alias), "chat", alias)

    def test_responses_aliases(self):
        self.assertEqual(_normalize_openai_api_mode("responses"), "responses")
        self.assertEqual(_normalize_openai_api_mode("response"), "responses")

    def test_ollama_value_is_recognized(self):
        # The whole point of this refactor: the OpenAI client api_mode
        # vocabulary now includes ``ollama`` so dispatch can be
        # decided by api_mode alone.
        self.assertEqual(_normalize_openai_api_mode("ollama"), "ollama")
        self.assertEqual(_normalize_openai_api_mode("OLLAMA"), "ollama")

    def test_unknown_values_fall_back_to_auto(self):
        self.assertEqual(_normalize_openai_api_mode("unknown"), "auto")
        self.assertEqual(_normalize_openai_api_mode("grpc"), "auto")


class ResolveApiModeTests(unittest.TestCase):
    """Precedence:

    1. Explicit ``params.api_mode`` always wins (after normalization).
    2. Otherwise, ``provider == 'ollama'`` infers ``api_mode='ollama'``
       so legacy configs that omit the new field keep working.
    3. Otherwise, default to ``'auto'`` (OpenAI-compatible probe).
    """

    def test_explicit_api_mode_wins_over_provider(self):
        # A user can label an Ollama gateway as ``provider='openai'``
        # for selector grouping but still keep the Ollama backend by
        # setting api_mode explicitly.
        self.assertEqual(
            resolve_api_mode(
                params={"api_mode": "ollama"}, provider="openai"
            ),
            "ollama",
        )

    def test_explicit_chat_mode_overrides_ollama_provider(self):
        # Conversely, an OpenAI-compatible base_url can be exposed
        # under a ``provider='ollama'`` label and the dispatcher
        # must respect the explicit api_mode rather than falling
        # back to the legacy provider hint.
        self.assertEqual(
            resolve_api_mode(
                params={"api_mode": "chat"}, provider="ollama"
            ),
            "chat",
        )

    def test_legacy_provider_ollama_without_api_mode(self):
        # Backward compatibility: existing configs that say
        # ``provider: "ollama"`` and have no api_mode field still
        # dispatch to the Ollama-native backend.
        self.assertEqual(
            resolve_api_mode(params={}, provider="ollama"),
            "ollama",
        )
        self.assertEqual(
            resolve_api_mode(params=None, provider="ollama"),
            "ollama",
        )

    def test_legacy_provider_openai_without_api_mode_returns_auto(self):
        self.assertEqual(
            resolve_api_mode(params={}, provider="openai"),
            "auto",
        )

    def test_unknown_provider_without_api_mode_returns_auto(self):
        # ``provider`` is now free-form. Anything that isn't
        # ``ollama`` and that doesn't explicitly set ``api_mode``
        # falls through to the OpenAI-compatible probe.
        self.assertEqual(
            resolve_api_mode(params={}, provider="nvidia"),
            "auto",
        )
        self.assertEqual(
            resolve_api_mode(params={}, provider="my-corp-gateway"),
            "auto",
        )
        self.assertEqual(
            resolve_api_mode(params={}, provider=""),
            "auto",
        )

    def test_explicit_auto_is_preserved(self):
        self.assertEqual(
            resolve_api_mode(
                params={"api_mode": "auto"}, provider="ollama"
            ),
            "auto",
        )

    def test_blank_api_mode_field_falls_back_to_provider_inference(self):
        # An empty/whitespace api_mode value is treated as "not set"
        # so the legacy provider inference still kicks in.
        self.assertEqual(
            resolve_api_mode(
                params={"api_mode": ""}, provider="ollama"
            ),
            "ollama",
        )
        self.assertEqual(
            resolve_api_mode(
                params={"api_mode": "   "}, provider="ollama"
            ),
            "ollama",
        )


if __name__ == "__main__":
    unittest.main()
