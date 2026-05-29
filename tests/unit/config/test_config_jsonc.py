import json
import tempfile
import unittest
from pathlib import Path

from src.core.config.config_jsonc import load_config_jsonc, save_config_jsonc


class ConfigJsoncTests(unittest.TestCase):
    def test_load_supports_jsonc_comments(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.jsonc"
            p.write_text(
                "{\n"
                "  // provider section\n"
                "  \"model_providers\": [\n"
                "    {\n"
                "      \"provider\": \"openai\", /* inline comment */\n"
                "      \"params\": {\"api_key\": \"k\", \"models\": [\"gpt-oss-120b\"]}\n"
                "    }\n"
                "  ]\n"
                "}\n",
                encoding="utf-8",
            )
            data = load_config_jsonc(p)
            self.assertEqual(data["model_providers"][0]["provider"], "openai")

    def test_load_requires_json_object_root(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.jsonc"
            p.write_text("[1,2,3]\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config_jsonc(p)

    def test_save_writes_pretty_json_text(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.jsonc"
            save_config_jsonc(p, {"a": 1, "b": {"c": 2}})
            text = p.read_text(encoding="utf-8")
            self.assertIn("\n  \"a\": 1,", text)
            self.assertTrue(text.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
