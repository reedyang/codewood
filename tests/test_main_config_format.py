import unittest

from src.main import _extract_model_runtime_config


class MainConfigFormatTests(unittest.TestCase):
    def test_selects_first_provider_first_model(self):
        provider, model_name, model_config, error = _extract_model_runtime_config(
            {
                "model_providers": [
                    {
                        "provider": "openwebui",
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
        self.assertEqual(provider, "openwebui")
        self.assertEqual(model_name, "gpt-oss-120b")
        self.assertEqual(model_config["provider"], "openwebui")
        self.assertEqual(model_config["params"]["models"][0], "gpt-oss-120b")
        self.assertEqual(model_config["params"]["model"], "gpt-oss-120b")

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
                        "provider": "openwebui",
                        "params": {"models": []},
                    }
                ]
            }
        )
        self.assertIn("models", error or "")


if __name__ == "__main__":
    unittest.main()
