import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from src.core.config.config_jsonc import CONFIG_JSONC_FILENAME
from src.runtime.bootstrap import setup_runtime_preferences


class _FakeAgent:
    def __init__(self, config_dir: Path):
        self.config_dir = config_dir


class RuntimePreferencesTests(unittest.TestCase):
    def test_invalid_auto_compact_trigger_percent_warns_and_uses_default(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            cfg_dir = Path(td)
            (cfg_dir / CONFIG_JSONC_FILENAME).write_text(
                json.dumps({"auto_compact_trigger_percent": "bad"}) + "\n",
                encoding="utf-8",
            )
            agent = _FakeAgent(cfg_dir)

            out = StringIO()
            with redirect_stdout(out):
                setup_runtime_preferences(agent)

            self.assertEqual(agent.auto_compact_trigger_percent, 60)
            self.assertIn("Invalid auto_compact_trigger_percent", out.getvalue())
            self.assertIn("using default 60%", out.getvalue())

    def test_valid_auto_compact_trigger_percent_is_loaded(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            cfg_dir = Path(td)
            (cfg_dir / CONFIG_JSONC_FILENAME).write_text(
                json.dumps({"auto_compact_trigger_percent": 75}) + "\n",
                encoding="utf-8",
            )
            agent = _FakeAgent(cfg_dir)

            setup_runtime_preferences(agent)

            self.assertEqual(agent.auto_compact_trigger_percent, 75)


if __name__ == "__main__":
    unittest.main()
