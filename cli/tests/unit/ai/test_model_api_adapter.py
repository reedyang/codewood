"""Tests for cli.ai.model_api_adapter — provider-specific cache hit statistics extraction."""

import unittest

from cli.ai.model_api_adapter import ModelApiAdapterManager
from cli.ai.adapters.deepseek_model_api_adapter import DeepSeekModelApiAdapter
from cli.ai.adapters.openai_model_api_adapter import OpenAIModelApiAdapter
from cli.ai.adapters.fallback_model_api_adapter import FallbackModelApiAdapter


class TestDeepSeekModelApiAdapter(unittest.TestCase):
    def setUp(self):
        self.adapter = DeepSeekModelApiAdapter()

    def test_supports_cache_stats(self):
        self.assertTrue(self.adapter.supports_cache_stats())

    def test_matches_deepseek_url(self):
        self.assertTrue(DeepSeekModelApiAdapter.matches("https://api.deepseek.com/v1"))
        self.assertTrue(DeepSeekModelApiAdapter.matches("https://api.deepseek.com"))
        self.assertTrue(DeepSeekModelApiAdapter.matches("http://api.deepseek.com/"))

    def test_does_not_match_other_urls(self):
        self.assertFalse(DeepSeekModelApiAdapter.matches("https://api.openai.com/v1"))
        self.assertFalse(DeepSeekModelApiAdapter.matches("https://api.anthropic.com"))
        self.assertFalse(DeepSeekModelApiAdapter.matches(""))

    def test_extract_cache_stats_with_valid_usage(self):
        data = {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "prompt_cache_hit_tokens": 80,
                "prompt_cache_miss_tokens": 20,
                "total_tokens": 150,
            }
        }
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["prompt_cache_hit_tokens"], 80)
        self.assertEqual(stats["prompt_cache_miss_tokens"], 20)

    def test_extract_cache_stats_partial(self):
        data = {
            "usage": {
                "prompt_cache_hit_tokens": 80,
            }
        }
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["prompt_cache_hit_tokens"], 80)
        self.assertEqual(stats["prompt_cache_miss_tokens"], 0)

    def test_extract_cache_stats_missing_usage(self):
        data = {"choices": [{"message": {"content": "hi"}}]}
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNone(stats)

    def test_extract_cache_stats_usage_not_dict(self):
        data = {"usage": "invalid"}
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNone(stats)

    def test_extract_cache_stats_empty_usage(self):
        data = {"usage": {}}
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNone(stats)

    def test_extract_cache_stats_none_values_treated_as_zero(self):
        data = {
            "usage": {
                "prompt_cache_hit_tokens": None,
                "prompt_cache_miss_tokens": None,
            }
        }
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["prompt_cache_hit_tokens"], 0)
        self.assertEqual(stats["prompt_cache_miss_tokens"], 0)


class TestOpenAIModelApiAdapter(unittest.TestCase):
    def setUp(self):
        self.adapter = OpenAIModelApiAdapter()

    def test_supports_cache_stats(self):
        self.assertTrue(self.adapter.supports_cache_stats())

    def test_matches_openai_url(self):
        self.assertTrue(OpenAIModelApiAdapter.matches("https://api.openai.com/v1"))
        self.assertTrue(OpenAIModelApiAdapter.matches("https://api.openai.com"))
        self.assertTrue(OpenAIModelApiAdapter.matches("http://api.openai.com/"))

    def test_does_not_match_other_urls(self):
        self.assertFalse(OpenAIModelApiAdapter.matches("https://api.deepseek.com/v1"))
        self.assertFalse(OpenAIModelApiAdapter.matches("https://api.anthropic.com"))
        self.assertFalse(OpenAIModelApiAdapter.matches(""))

    # -- Chat Completions API usage --

    def test_extract_chat_completions_cache_stats(self):
        data = {
            "usage": {
                "prompt_tokens": 2006,
                "completion_tokens": 300,
                "total_tokens": 2306,
                "prompt_tokens_details": {
                    "cached_tokens": 1920,
                },
            }
        }
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["prompt_cache_hit_tokens"], 1920)
        self.assertEqual(stats["prompt_cache_miss_tokens"], 86)

    def test_extract_chat_completions_cached_zero(self):
        data = {
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 100,
                "total_tokens": 600,
                "prompt_tokens_details": {
                    "cached_tokens": 0,
                },
            }
        }
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["prompt_cache_hit_tokens"], 0)
        self.assertEqual(stats["prompt_cache_miss_tokens"], 500)

    def test_extract_chat_completions_no_details_falls_back_to_root_tokens(self):
        data = {
            "usage": {
                "prompt_tokens": 2006,
                "completion_tokens": 300,
                "total_tokens": 2306,
                "prompt_tokens_details": {},
            }
        }
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["input_tokens"], 2006)

    def test_extract_chat_completions_cached_none(self):
        data = {
            "usage": {
                "prompt_tokens": 2006,
                "completion_tokens": 300,
                "total_tokens": 2306,
                "prompt_tokens_details": {
                    "cached_tokens": None,
                },
            }
        }
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["prompt_cache_hit_tokens"], 0)
        self.assertEqual(stats["prompt_cache_miss_tokens"], 2006)

    # -- Responses API usage --

    def test_extract_responses_cache_stats(self):
        data = {
            "usage": {
                "input_tokens": 2006,
                "output_tokens": 300,
                "total_tokens": 2306,
                "input_tokens_details": {
                    "cached_tokens": 1920,
                },
                "output_tokens_details": {
                    "reasoning_tokens": 100,
                },
            }
        }
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["prompt_cache_hit_tokens"], 1920)
        self.assertEqual(stats["prompt_cache_miss_tokens"], 86)

    def test_extract_responses_cached_zero(self):
        data = {
            "usage": {
                "input_tokens": 2006,
                "output_tokens": 300,
                "total_tokens": 2306,
                "input_tokens_details": {
                    "cached_tokens": 0,
                },
                "output_tokens_details": {
                    "reasoning_tokens": 0,
                },
            }
        }
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["prompt_cache_hit_tokens"], 0)
        self.assertEqual(stats["prompt_cache_miss_tokens"], 2006)

    # -- Edge cases --

    def test_extract_cache_stats_missing_usage(self):
        data = {"output": [{"type": "message", "content": []}]}
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNone(stats)

    def test_extract_cache_stats_usage_not_dict(self):
        data = {"usage": "invalid"}
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNone(stats)

    def test_extract_cache_stats_no_cache_fields_falls_back_to_root_tokens(self):
        data = {"usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}}
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["input_tokens"], 100)


class TestFallbackModelApiAdapter(unittest.TestCase):
    def setUp(self):
        self.adapter = FallbackModelApiAdapter()

    def test_matches_everything(self):
        self.assertTrue(FallbackModelApiAdapter.matches("https://api.openai.com/v1"))
        self.assertTrue(FallbackModelApiAdapter.matches("https://api.deepseek.com/v1"))
        self.assertTrue(FallbackModelApiAdapter.matches("https://api.anthropic.com"))
        self.assertTrue(FallbackModelApiAdapter.matches(""))

    def test_supports_cache_stats_initial(self):
        self.assertTrue(self.adapter.supports_cache_stats())

    def test_supports_cache_stats_after_success(self):
        data = {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "prompt_tokens_details": {"cached_tokens": 60},
            }
        }
        self.adapter.extract_cache_stats(data)
        self.assertTrue(self.adapter.supports_cache_stats())

    def test_supports_cache_stats_after_failure(self):
        data = {"usage": {"completion_tokens": 50}}
        self.adapter.extract_cache_stats(data)
        self.assertFalse(self.adapter.supports_cache_stats())

    def test_supports_cache_stats_flips_back_on_success(self):
        data_fail = {"usage": {"completion_tokens": 50}}
        data_ok = {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "prompt_tokens_details": {"cached_tokens": 40},
            }
        }
        self.adapter.extract_cache_stats(data_fail)
        self.assertFalse(self.adapter.supports_cache_stats())
        self.adapter.extract_cache_stats(data_ok)
        self.assertTrue(self.adapter.supports_cache_stats())

    def test_extract_chat_completions_format(self):
        data = {
            "usage": {
                "prompt_tokens": 200,
                "completion_tokens": 50,
                "total_tokens": 250,
                "prompt_tokens_details": {"cached_tokens": 150},
            }
        }
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["prompt_cache_hit_tokens"], 150)
        self.assertEqual(stats["prompt_cache_miss_tokens"], 50)

    def test_extract_responses_format(self):
        data = {
            "usage": {
                "input_tokens": 300,
                "output_tokens": 80,
                "total_tokens": 380,
                "input_tokens_details": {"cached_tokens": 200},
                "output_tokens_details": {"reasoning_tokens": 10},
            }
        }
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["prompt_cache_hit_tokens"], 200)
        self.assertEqual(stats["prompt_cache_miss_tokens"], 100)

    def test_extract_no_cache_info_falls_back_to_root_tokens(self):
        data = {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["input_tokens"], 100)

    def test_extract_root_input_tokens(self):
        data = {"usage": {"input_tokens": 200, "output_tokens": 80, "total_tokens": 280}}
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["input_tokens"], 200)

    def test_extract_root_tokens_prefers_input_tokens_over_prompt_tokens(self):
        data = {
            "usage": {
                "input_tokens": 200,
                "prompt_tokens": 100,
                "output_tokens": 80,
                "total_tokens": 280,
            }
        }
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["input_tokens"], 200)

    def test_extract_root_tokens_supports_cache_stats(self):
        data = {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
        self.adapter.extract_cache_stats(data)
        self.assertTrue(self.adapter.supports_cache_stats())

    def test_extract_missing_usage(self):
        data = {"output": [{"type": "message"}]}
        stats = self.adapter.extract_cache_stats(data)
        self.assertIsNone(stats)


class TestModelApiAdapterManager(unittest.TestCase):
    def setUp(self):
        self.manager = ModelApiAdapterManager()

    def test_resolve_deepseek(self):
        adapter = self.manager.resolve("https://api.deepseek.com/v1")
        self.assertIsInstance(adapter, DeepSeekModelApiAdapter)

    def test_resolve_openai(self):
        adapter = self.manager.resolve("https://api.openai.com/v1")
        self.assertIsInstance(adapter, OpenAIModelApiAdapter)

    def test_resolve_fallback_for_unknown(self):
        adapter = self.manager.resolve("https://api.anthropic.com")
        self.assertIsInstance(adapter, FallbackModelApiAdapter)

    def test_register_adapter(self):
        manager = ModelApiAdapterManager()
        manager.register(DeepSeekModelApiAdapter())
        adapter = manager.resolve("https://api.deepseek.com/v1")
        self.assertIsInstance(adapter, DeepSeekModelApiAdapter)
