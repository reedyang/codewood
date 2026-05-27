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

    def test_install_requires_detail_url(self):
        args = SimpleNamespace(
            confirm="YES",
            detail_url="",
            insecure=False,
            no_verify=False,
            config_dir=tempfile.gettempdir(),
            builtin_skills_root="",
            workspace_skills_root="",
            on_conflict="abort",
        )
        captured = io.StringIO()
        with patch.object(self.mod, "_search", side_effect=AssertionError("must not search")):
            with patch("builtins.input", side_effect=AssertionError("must not prompt")):
                with patch("sys.stdout", new=captured):
                    rc = self.mod.cmd_install(args)
        self.assertEqual(rc, 2)
        self.assertIn("Invalid install arguments: provide --detail-url.", captured.getvalue())

    def test_install_detail_url_installs_without_prompt(self):
        detail_html = """
        <h2>SKILL.md</h2>
        ```markdown
        ---
        name: demo-two
        description: demo two
        ---

        # Demo Two
        ```
        """
        with tempfile.TemporaryDirectory() as td:
            args = SimpleNamespace(
                confirm="YES",
                detail_url="https://www.skillhub.club/skills/demo-two",
                insecure=False,
                no_verify=False,
                config_dir=td,
                builtin_skills_root="",
                workspace_skills_root="",
                on_conflict="abort",
            )
            captured = io.StringIO()
            with patch.object(self.mod, "_search", side_effect=AssertionError("must not search")):
                with patch.object(self.mod, "_fetch", return_value=detail_html):
                    with patch("builtins.input", side_effect=AssertionError("must not prompt")):
                        with patch("sys.stdout", new=captured):
                            rc = self.mod.cmd_install(args)
            self.assertEqual(rc, 0)
            self.assertTrue((Path(td) / "skills" / "demo-two" / "SKILL.md").is_file())
            self.assertIn("detail_url: https://www.skillhub.club/skills/demo-two", captured.getvalue())

    def test_install_nonstandard_skill_md_normalizes_frontmatter(self):
        detail_html = """
        <div class="prose-skill min-w-0 max-w-full overflow-x-auto p-4 sm:p-6">
          <h1>Demo Raw</h1>
          <p>Installs a raw skill without frontmatter.</p>
          <ul><li>Keep this body line.</li></ul>
        </div>
        """
        with tempfile.TemporaryDirectory() as td:
            args = SimpleNamespace(
                confirm="YES",
                detail_url="https://www.skillhub.club/skills/demo-raw",
                insecure=False,
                no_verify=False,
                config_dir=td,
                builtin_skills_root="",
                workspace_skills_root="",
                on_conflict="abort",
            )
            captured = io.StringIO()
            with patch.object(self.mod, "_search", side_effect=AssertionError("must not search")):
                with patch.object(self.mod, "_fetch", return_value=detail_html):
                    with patch("builtins.input", side_effect=AssertionError("must not prompt")):
                        with patch("sys.stdout", new=captured):
                            rc = self.mod.cmd_install(args)
            self.assertEqual(rc, 0)
            skill_md = (Path(td) / "skills" / "demo-raw" / "SKILL.md").read_text(encoding="utf-8")
            self.assertTrue(skill_md.startswith("---\n"))
            self.assertIn('name: "Demo Raw"', skill_md)
            self.assertIn('description: "Installs a raw skill without frontmatter."', skill_md)
            self.assertIn("# Demo Raw", skill_md)
            self.assertIn("- Keep this body line.", skill_md)
            self.assertIn("normalized_frontmatter: yes", captured.getvalue())

    def test_install_config_conflict_aborts_without_prompt(self):
        detail_html = """
        <h2>SKILL.md</h2>
        ```markdown
        ---
        name: demo-conflict
        description: demo conflict
        ---

        # Demo Conflict
        ```
        """
        with tempfile.TemporaryDirectory() as td:
            existing = Path(td) / "skills" / "demo-conflict"
            existing.mkdir(parents=True)
            (existing / "SKILL.md").write_text(
                "---\nname: demo-conflict\ndescription: existing\n---\n\n# Existing\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                confirm="YES",
                detail_url="https://www.skillhub.club/skills/demo-conflict",
                insecure=False,
                no_verify=False,
                config_dir=td,
                builtin_skills_root="",
                workspace_skills_root="",
                on_conflict="abort",
            )
            captured = io.StringIO()
            with patch.object(self.mod, "_fetch", return_value=detail_html):
                with patch("builtins.input", side_effect=AssertionError("must not prompt")):
                    with patch("sys.stdout", new=captured):
                        rc = self.mod.cmd_install(args)
            self.assertEqual(rc, 3)
            self.assertIn("Install aborted due to config conflict.", captured.getvalue())

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
