import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _load_installer_module():
    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / "skills" / "skillhub-skill-installer" / "scripts" / "skillhub_installer.py"
    spec = importlib.util.spec_from_file_location("skillhub_installer_test_module", str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load skillhub_installer module")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class SkillHubInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_installer_module()

    def test_install_index_cancel_is_user_abort_terminal(self):
        args = SimpleNamespace(
            confirm="YES",
            detail_url="",
            query="gmail",
            max_results=8,
            insecure=False,
            no_verify=False,
            config_dir=tempfile.gettempdir(),
            builtin_skills_root="",
            workspace_skills_root="",
        )
        cards = [
            self.mod.SkillCard(name="Gmail", detail_url="https://www.skillhub.club/skills/gmail", snippet="")
        ]
        captured = io.StringIO()
        with patch.object(self.mod, "_search", return_value=cards):
            with patch.object(self.mod, "_prompt_inline", return_value="c"):
                with patch("sys.stdout", new=captured):
                    rc = self.mod.cmd_install(args)
        self.assertEqual(rc, 2)
        self.assertIn("Installation aborted by user.", captured.getvalue())


if __name__ == "__main__":
    unittest.main()

