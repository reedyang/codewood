"""Tests for cli.ai.cache_adapter — provider-specific cache hit statistics extraction."""

import unittest

from cli.ai.cache_adapter import (
    CacheAdapterManager,
    DeepSeekCacheAdapter,
)


class TestDeepSeekCacheAdapter(unittest.TestCase):
    def setUp(self):
        self.adapter = DeepSeekCacheAdapter()

    def test_supports_cache_stats(self):
        self.assertTrue(self.adapter.supports_cache_stats())

    def test_matches_deepseek_url(self):
        self.assertTrue(DeepSeekCacheAdapter.matches("https://api.deepseek.com/v1"))
        self.assertTrue(DeepSeekCacheAdapter.matches("https://api.deepseek.com"))
        self.assertTrue(DeepSeekCacheAdapter.matches("http://api.deepseek.com/"))

    def test_does_not_match_other_urls(self):
        self.assertFalse(DeepSeekCacheAdapter.matches("https://api.openai.com/v1"))
        self.assertFalse(DeepSeekCacheAdapter.matches("https://api.anthropic.com"))
        self.assertFalse(DeepSeekCacheAdapter.matches(""))

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


class TestCacheAdapterManager(unittest.TestCase):
    def setUp(self):
        self.manager = CacheAdapterManager()

    def test_resolve_deepseek(self):
        adapter = self.manager.resolve("https://api.deepseek.com/v1")
        self.assertIsInstance(adapter, DeepSeekCacheAdapter)

    def test_resolve_unknown_returns_none(self):
        adapter = self.manager.resolve("https://api.openai.com/v1")
        self.assertIsNone(adapter)

    def test_register_adapter(self):
        manager = CacheAdapterManager()
        manager.register(DeepSeekCacheAdapter())
        adapter = manager.resolve("https://api.deepseek.com/v1")
        self.assertIsInstance(adapter, DeepSeekCacheAdapter)
