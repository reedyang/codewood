import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from src.controllers.language_command_controller import handle_language_builtin_command


class _FakeLanguageAgent:
    def __init__(self, config_dir: Path):
        self.config_dir = config_dir
        self.display_language = "en"
        self._resolved_config_data = {}
        self.reload_calls = 0

    def _reload_chat_history_from_anchor_on_resize(self):
        self.reload_calls += 1


class LanguageCommandControllerTests(unittest.TestCase):
    def test_language_usage_prints_supported_languages(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeLanguageAgent(Path(td))
            buf = io.StringIO()
            with redirect_stdout(buf):
                handled = handle_language_builtin_command(agent, "language")
            self.assertTrue(handled)
            out = buf.getvalue()
            self.assertIn("Usage:", out)
            self.assertIn("/language <language code>", out)
            self.assertIn("Current language:", out)
            self.assertIn("en - English", out)
            self.assertIn("zh-CN - 简体中文", out)

    def test_language_change_persists_and_reloads(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_dir = Path(td)
            (cfg_dir / "config.jsonc").write_text("{}", encoding="utf-8")
            agent = _FakeLanguageAgent(cfg_dir)
            handled = handle_language_builtin_command(agent, "language zh-CN")
            self.assertTrue(handled)
            self.assertEqual(agent.display_language, "zh-CN")
            self.assertEqual(agent.reload_calls, 1)
            with open(cfg_dir / "config.jsonc", "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data.get("language"), "zh-CN")
            self.assertNotIn("display_language", data)

    def test_invalid_language_shows_usage(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeLanguageAgent(Path(td))
            buf = io.StringIO()
            with redirect_stdout(buf):
                handled = handle_language_builtin_command(agent, "language fr")
            self.assertTrue(handled)
            out = buf.getvalue()
            self.assertIn("Unsupported language", out)
            self.assertIn("/language <language code>", out)
            self.assertNotIn("/language list", out)


if __name__ == "__main__":
    unittest.main()
