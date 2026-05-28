import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import src.main as main_module


class MainStartupConfigTests(unittest.TestCase):
    def test_creates_user_config_template_when_no_config_exists(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td_home, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td_project:
            with patch.object(main_module, "project_root", Path(td_project)), patch(
                "src.main.Path.home", return_value=Path(td_home)
            ), patch("src.core.logging.app_logging.setup_app_logging"), patch(
                "src.core.logging.app_logging.get_logger", return_value=MagicMock()
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = main_module.main()

                self.assertEqual(code, 1)
                created = Path(td_home) / ".smartshell" / "config.jsonc"
                self.assertTrue(created.exists())
                got = json.loads(created.read_text(encoding="utf-8").strip())
                self.assertEqual(got, main_module.DEFAULT_USER_CONFIG_TEMPLATE)
                out = buf.getvalue()
                self.assertLess(out.find("╭"), out.find("Config file not found. Created template successfully."))
                self.assertIn("Config file not found. Created template successfully.", out)
                self.assertIn("Please update the model settings in:", out)
                self.assertEqual(out.count(str(created)), 1)

    def test_invalid_model_config_prints_english_reminder(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td_home, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td_project:
            user_cfg_dir = Path(td_home) / ".smartshell"
            user_cfg_dir.mkdir(parents=True, exist_ok=True)
            cfg_path = user_cfg_dir / "config.jsonc"
            cfg_path.write_text(json.dumps({"execution_policy": "moderate"}) + "\n", encoding="utf-8")

            with patch.object(main_module, "project_root", Path(td_project)), patch(
                "src.main.Path.home", return_value=Path(td_home)
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = main_module.main()

                self.assertEqual(code, 1)
                out = buf.getvalue()
                self.assertLess(out.find("╭"), out.find("Please update the model settings in:"))
                self.assertIn("Please update the model settings in:", out)
                self.assertIn(str(cfg_path), out)
                for line in out.splitlines():
                    if "Please update the model settings in:" in line:
                        path_part = line.split("Please update the model settings in:", 1)[1].strip()
                        self.assertFalse("/" in path_part and "\\" in path_part)

    def test_template_placeholder_values_are_rejected(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td_home, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td_project:
            user_cfg_dir = Path(td_home) / ".smartshell"
            user_cfg_dir.mkdir(parents=True, exist_ok=True)
            cfg_path = user_cfg_dir / "config.jsonc"
            cfg_path.write_text(
                json.dumps(main_module.DEFAULT_USER_CONFIG_TEMPLATE) + "\n",
                encoding="utf-8",
            )

            with patch.object(main_module, "project_root", Path(td_project)), patch(
                "src.main.Path.home", return_value=Path(td_home)
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = main_module.main()

                self.assertEqual(code, 1)
                out = buf.getvalue()
                self.assertIn("Please update the model settings in:", out)
                self.assertIn(str(cfg_path), out)
                for line in out.splitlines():
                    if "Please update the model settings in:" in line:
                        path_part = line.split("Please update the model settings in:", 1)[1].strip()
                        self.assertFalse("/" in path_part and "\\" in path_part)


if __name__ == "__main__":
    unittest.main()
