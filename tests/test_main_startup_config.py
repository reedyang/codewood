import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import src.main as main_module
from src.config.app_info import get_app_config_dirname


class MainStartupConfigTests(unittest.TestCase):
    @staticmethod
    def _template_data():
        return {
            "model_providers": [
                {
                    "provider": "openai",
                    "params": {
                        "api_key": "<YOUR API KEY>",
                        "base_url": "https://api.openai.com/v1",
                        "models": [
                            {
                                "name": "<YOUR MODEL NAME>",
                                "context_window": 131072,
                            }
                        ],
                    },
                }
            ],
            "execution_policy": "moderate",
            "project_context_first_round_evidence": True,
            "max_tool_rounds": None,
            "memory_enabled": False,
            "mcp_tools_enabled": False,
        }

    @classmethod
    def _write_template_file(cls, project_dir: Path) -> Path:
        template_path = project_dir / "src/config" / "config.template.jsonc"
        template_path.parent.mkdir(parents=True, exist_ok=True)
        template_path.write_text(
            json.dumps(cls._template_data(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return template_path

    def test_creates_user_config_template_when_no_config_exists(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td_home, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td_project:
            self._write_template_file(Path(td_project))
            with patch.object(main_module, "project_root", Path(td_project)), patch(
                "src.main.Path.home", return_value=Path(td_home)
            ), patch("src.core.logging.app_logging.setup_app_logging"), patch(
                "src.core.logging.app_logging.get_logger", return_value=MagicMock()
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = main_module.main()

                self.assertEqual(code, 1)
                created = Path(td_home) / get_app_config_dirname() / "config.jsonc"
                self.assertTrue(created.exists())
                got = json.loads(created.read_text(encoding="utf-8").strip())
                self.assertEqual(got, self._template_data())
                out = buf.getvalue()
                self.assertLess(out.find("╭"), out.find("Config file not found. Created template successfully."))
                self.assertIn("Config file not found. Created template successfully.", out)
                self.assertIn("Please update the model settings in:", out)
                self.assertEqual(out.count(str(created)), 1)

    def test_invalid_model_config_prints_english_reminder(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td_home, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td_project:
            self._write_template_file(Path(td_project))
            user_cfg_dir = Path(td_home) / get_app_config_dirname()
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
            self._write_template_file(Path(td_project))
            user_cfg_dir = Path(td_home) / get_app_config_dirname()
            user_cfg_dir.mkdir(parents=True, exist_ok=True)
            cfg_path = user_cfg_dir / "config.jsonc"
            cfg_path.write_text(
                json.dumps(self._template_data()) + "\n",
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

    def test_help_exits_early_with_version_and_usage_only(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main_module.main(["--help"])

        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("Version:", out)
        self.assertIn("Usage:", out)
        self.assertIn("Commands:", out)
        self.assertIn("Arguments:", out)
        self.assertIn("Options:", out)
        self.assertIn("-m, --model <MODEL>", out)
        self.assertNotIn("Config file not found", out)
        self.assertNotIn("Please update the model settings in:", out)

    def test_help_uses_hidden_executable_name(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main_module.main(["--help", "--executable-name", "start.bat"])

        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("start.bat", out)
        self.assertNotIn("python src/main.py", out)
        self.assertNotIn("--executable-name", out)

    def test_explicit_model_arg_reapplies_active_chat_model(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td_home, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td_project:
            self._write_template_file(Path(td_project))
            user_cfg_dir = Path(td_home) / get_app_config_dirname()
            user_cfg_dir.mkdir(parents=True, exist_ok=True)
            cfg_path = user_cfg_dir / "config.jsonc"
            cfg_path.write_text(
                json.dumps(
                    {
                        "model_providers": [
                            {
                                "provider": "openai",
                                "params": {
                                    "api_key": "real-key",
                                    "base_url": "https://api.openai.com/v1",
                                    "models": [
                                        {"name": "gpt-4o-mini", "context_window": 128000},
                                        {"name": "Gemma-4-31B", "context_window": 128000},
                                    ],
                                },
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            class FakeAgent:
                last_instance = None

                def __init__(self, model_name=None, work_directory=None, provider=None, params=None, model_config=None, config_dir=None, builtin_skills_dir=None):
                    self.model_name = str(model_name or "")
                    self.provider = str(provider or "")
                    self.params = dict(params or {})
                    self.model_config = dict(model_config or {})
                    self._workspaces_state = {"workspaces": {}}
                    self.work_directory = Path.cwd()
                    self.workspace_root = str(self.work_directory)
                    self.workspace_name = "Default"
                    self.switch_calls = []
                    FakeAgent.last_instance = self

                def _save_current_workspace_position(self):
                    return None

                def _apply_workspace_entry(self, entry, fallback_dir):
                    return None

                def _refresh_workspace_runtime(self):
                    return None

                def _switch_model_by_selector(self, selector):
                    self.switch_calls.append(str(selector or ""))
                    return f"✅ Switched model: {selector}"

                def run(self):
                    return None

                def shutdown(self, wait=False):
                    return None

            with patch.object(main_module, "project_root", Path(td_project)), patch(
                "src.main.Path.home", return_value=Path(td_home)
            ), patch("src.agent.Agent", FakeAgent):
                code = main_module.main(["-m", "openai:Gemma-4-31B"])

            self.assertEqual(code, 0)
            self.assertIsNotNone(FakeAgent.last_instance)
            self.assertEqual(
                FakeAgent.last_instance.switch_calls,
                ["openai:Gemma-4-31B"],
            )


if __name__ == "__main__":
    unittest.main()
