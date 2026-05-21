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

    def test_extract_github_link_prefers_repo_url_from_embedded_json(self):
        detail_html = (
            '<a href="https://github.com/someone/awesome">profile</a>'
            '...repo_url\\":\\"https://github.com/openclaw/skills#skills~owner~demo-skill\\"...'
            '<a href="https://github.com/another/repo">other</a>'
        )
        url = self.mod._extract_github_link(detail_html)
        self.assertEqual(url, "https://github.com/openclaw/skills#skills~owner~demo-skill")

    def test_extract_skill_md_from_prose_html_matches_new_class_layout(self):
        detail_html = """
        <div class="prose-skill min-w-0 max-w-full overflow-x-auto p-4 sm:p-6">
          <h1>Demo Skill</h1>
          <p>Line one.</p>
          <ul><li>Item A</li><li>Item B</li></ul>
        </div>
        """
        skill_md = self.mod._extract_skill_md_from_prose_html(detail_html)
        self.assertIn("# Demo Skill", skill_md)
        self.assertIn("Line one.", skill_md)
        self.assertIn("- Item A", skill_md)


if __name__ == "__main__":
    unittest.main()
