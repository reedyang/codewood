import unittest

from src.main import _extract_model_runtime_config


class MainConfigFormatTests(unittest.TestCase):
    def test_selects_first_provider_first_model(self):
        provider, model_name, model_config, error = _extract_model_runtime_config(
            {
                "model_providers": [
                    {
                        "provider": "openai",
                        "params": {
                            "api_key": "k1",
                            "base_url": "https://example.com/v1",
                            "models": ["gpt-oss-120b", "gpt-4o-mini"],
                        },
                    },
                    {
                        "provider": "ollama",
                        "params": {"models": ["qwen2.5vl:3b"]},
                    },
                ]
            }
        )
        self.assertIsNone(error)
        self.assertEqual(provider, "openai")
        self.assertEqual(model_name, "gpt-oss-120b")
        self.assertEqual(model_config["provider"], "openai")
        self.assertEqual(model_config["params"]["models"][0], "gpt-oss-120b")
        self.assertEqual(model_config["params"]["model"], "gpt-oss-120b")
        self.assertEqual(model_config["params"]["context_window"], 128000)
        self.assertTrue(model_config["params"]["streaming"])
        self.assertFalse(model_config["params"]["use_simulated_tools"])

    def test_supports_object_model_with_numeric_context_window(self):
        provider, model_name, model_config, error = _extract_model_runtime_config(
            {
                "model_providers": [
                    {
                        "provider": "openai",
                        "params": {
                            "models": [
                                {"name": "gpt-oss-120b", "context_window": 64000},
                                {"name": "gpt-4o-mini", "context_window": "96k"},
                            ]
                        },
                    }
                ]
            }
        )
        self.assertIsNone(error)
        self.assertEqual(provider, "openai")
        self.assertEqual(model_name, "gpt-oss-120b")
        self.assertEqual(model_config["params"]["models"], ["gpt-oss-120b", "gpt-4o-mini"])
        self.assertEqual(model_config["params"]["context_window"], 64000)
        self.assertTrue(model_config["params"]["streaming"])
        self.assertFalse(model_config["params"]["use_simulated_tools"])

    def test_supports_k_suffix_context_window(self):
        _, _, model_config, error = _extract_model_runtime_config(
            {
                "model_providers": [
                    {
                        "provider": "openai",
                        "params": {
                            "models": [
                                {"name": "gpt-oss-120b", "context_window": "128K"},
                            ]
                        },
                    }
                ]
            }
        )
        self.assertIsNone(error)
        self.assertEqual(model_config["params"]["context_window"], 128000)

    def test_invalid_context_window_falls_back_to_default(self):
        _, _, model_config, error = _extract_model_runtime_config(
            {
                "model_providers": [
                    {
                        "provider": "openai",
                        "params": {
                            "models": [
                                {"name": "gpt-oss-120b", "context_window": "bad-value"},
                            ]
                        },
                    }
                ]
            }
        )
        self.assertIsNone(error)
        self.assertEqual(model_config["params"]["context_window"], 128000)

    def test_supports_use_simulated_tools_flag(self):
        _, _, model_config, error = _extract_model_runtime_config(
            {
                "model_providers": [
                    {
                        "provider": "openai",
                        "params": {
                            "models": [
                                {
                                    "name": "gpt-oss-120b",
                                    "context_window": 128000,
                                    "use_simulated_tools": "true",
                                },
                            ]
                        },
                    }
                ]
            }
        )
        self.assertIsNone(error)
        self.assertTrue(model_config["params"]["use_simulated_tools"])

    def test_supports_streaming_flag(self):
        _, _, model_config, error = _extract_model_runtime_config(
            {
                "model_providers": [
                    {
                        "provider": "openai",
                        "params": {
                            "models": [
                                {
                                    "name": "gpt-oss-120b",
                                    "context_window": 128000,
                                    "streaming": "false",
                                },
                            ]
                        },
                    }
                ]
            }
        )
        self.assertIsNone(error)
        self.assertFalse(model_config["params"]["streaming"])

    def test_supports_model_level_extra_headers(self):
        _, _, model_config, error = _extract_model_runtime_config(
            {
                "model_providers": [
                    {
                        "provider": "openai",
                        "params": {
                            "models": [
                                {
                                    "name": "gpt-oss-120b",
                                    "context_window": 128000,
                                    "extra_headers": {
                                        "X-Model": "gpt-oss-120b",
                                        "X-Test-Route": "enabled",
                                    },
                                },
                            ]
                        },
                    }
                ]
            }
        )
        self.assertIsNone(error)
        self.assertEqual(
            model_config["params"]["extra_headers"],
            {
                "X-Model": "gpt-oss-120b",
                "X-Test-Route": "enabled",
            },
        )

    def test_ollama_port_defaults_and_can_be_configured(self):
        _, _, default_config, default_error = _extract_model_runtime_config(
            {
                "model_providers": [
                    {
                        "provider": "ollama",
                        "params": {"models": ["qwen2.5:14b"]},
                    }
                ]
            }
        )
        self.assertIsNone(default_error)
        self.assertEqual(default_config["params"]["port"], 11434)

        _, _, custom_config, custom_error = _extract_model_runtime_config(
            {
                "model_providers": [
                    {
                        "provider": "ollama",
                        "params": {"port": "11555", "models": ["qwen2.5:14b"]},
                    }
                ]
            }
        )
        self.assertIsNone(custom_error)
        self.assertEqual(custom_config["params"]["port"], 11555)

    def test_requires_model_providers(self):
        provider, model_name, model_config, error = _extract_model_runtime_config({})
        self.assertIsNone(provider)
        self.assertIsNone(model_name)
        self.assertIsNone(model_config)
        self.assertIn("model_providers", error or "")

    def test_requires_non_empty_models(self):
        _, _, _, error = _extract_model_runtime_config(
            {
                "model_providers": [
                    {
                        "provider": "openai",
                        "params": {"models": []},
                    }
                ]
            }
        )
        self.assertIn("models", error or "")

    def test_startup_model_override_by_name(self):
        provider, model_name, model_config, error = _extract_model_runtime_config(
            {
                "model_providers": [
                    {
                        "provider": "openai",
                        "params": {
                            "models": ["gpt-oss-120b", "gpt-4o-mini"],
                        },
                    },
                    {
                        "provider": "ollama",
                        "params": {
                            "models": ["qwen2.5-coder:7b"],
                        },
                    },
                ]
            },
            requested_model="qwen2.5-coder:7b",
        )
        self.assertIsNone(error)
        self.assertEqual(provider, "ollama")
        self.assertEqual(model_name, "qwen2.5-coder:7b")
        self.assertEqual(model_config["params"]["model"], "qwen2.5-coder:7b")

    def test_startup_model_override_by_provider_model_selector(self):
        provider, model_name, model_config, error = _extract_model_runtime_config(
            {
                "model_providers": [
                    {
                        "provider": "openai",
                        "params": {"models": ["gpt-4o-mini"]},
                    },
                    {
                        "provider": "ollama",
                        "params": {"models": ["qwen2.5-coder:7b"]},
                    },
                ]
            },
            requested_model="openai:gpt-4o-mini",
        )
        self.assertIsNone(error)
        self.assertEqual(provider, "openai")
        self.assertEqual(model_name, "gpt-4o-mini")
        self.assertEqual(model_config["params"]["models"], ["gpt-4o-mini"])

    def test_startup_model_override_ambiguous_without_provider(self):
        provider, model_name, model_config, error = _extract_model_runtime_config(
            {
                "model_providers": [
                    {
                        "provider": "openai",
                        "params": {"models": ["shared-model"]},
                    },
                    {
                        "provider": "ollama",
                        "params": {"models": ["shared-model"]},
                    },
                ]
            },
            requested_model="shared-model",
        )
        self.assertIsNone(provider)
        self.assertIsNone(model_name)
        self.assertIsNone(model_config)
        self.assertIn("ambiguous", error or "")


if __name__ == "__main__":
    unittest.main()
