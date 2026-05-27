import importlib.util
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _load_installer_module():
    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / "skills" / "clawhub-skill-installer" / "scripts" / "clawhub_installer.py"
    spec = importlib.util.spec_from_file_location("clawhub_installer_test_module", str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load clawhub_installer module")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class ClawHubInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_installer_module()

    def test_cards_from_search_payload(self):
        payload = {
            "results": [
                {
                    "slug": "api",
                    "displayName": "Publish Api",
                    "summary": "REST API reference for many services.",
                },
                {
                    "slug": "json",
                    "displayName": "JSON",
                    "summary": "JSON handling helper.",
                },
            ]
        }
        cards = self.mod._cards_from_search_payload(payload, max_results=5)
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0].name, "Publish Api")
        self.assertEqual(cards[0].detail_url, "https://clawhub.ai/skills/api")

    def test_extract_skill_md_from_detail_page(self):
        detail_text = """
## SKILL.md
---
name: demo-skill
description: demo
---

# Demo
content

### Files
2 total
"""
        skill_md = self.mod._extract_skill_md(detail_text)
        self.assertTrue(skill_md.startswith("---"))
        self.assertIn("name: demo-skill", skill_md)
        self.assertIn("description: demo", skill_md)

    def test_extract_download_zip_url(self):
        detail_text = """
[Download zip](https://foo.convex.site/api/v1/download?slug=api)
"""
        url = self.mod._extract_download_zip_url(detail_text)
        self.assertEqual(url, "https://foo.convex.site/api/v1/download?slug=api")

    def test_extract_skill_md_from_zip(self):
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "gmail/SKILL.md",
                "---\nname: gmail\ndescription: gmail integration\n---\n\n# Gmail\n",
            )
            zf.writestr("gmail/LICENSE.txt", "MIT-0")
        txt = self.mod._extract_skill_md_from_zip(mem.getvalue())
        self.assertTrue(txt.startswith("---"))
        self.assertIn("name: gmail", txt)

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
## SKILL.md
---
name: gmail
description: gmail integration
---

# Gmail
content

### Files
"""
        with tempfile.TemporaryDirectory() as td:
            args = SimpleNamespace(
                confirm="YES",
                detail_url="https://clawhub.ai/skills/gmail",
                insecure=False,
                no_verify=False,
                config_dir=td,
                builtin_skills_root="",
                workspace_skills_root="",
                on_conflict="abort",
            )
            captured = io.StringIO()
            with patch.object(self.mod, "_search", side_effect=AssertionError("must not search")):
                with patch.object(self.mod, "_fetch_text", return_value=detail_html):
                    with patch("builtins.input", side_effect=AssertionError("must not prompt")):
                        with patch("sys.stdout", new=captured):
                            rc = self.mod.cmd_install(args)
            self.assertEqual(rc, 0)
            self.assertTrue((Path(td) / "skills" / "gmail" / "SKILL.md").is_file())
            self.assertIn("detail_url: https://clawhub.ai/skills/gmail", captured.getvalue())

    def test_install_nonstandard_skill_md_normalizes_frontmatter(self):
        detail_html = """
## SKILL.md
# Demo Raw

Installs a raw skill without frontmatter.

- Keep this body line.

### Files
"""
        with tempfile.TemporaryDirectory() as td:
            args = SimpleNamespace(
                confirm="YES",
                detail_url="https://clawhub.ai/skills/demo-raw",
                insecure=False,
                no_verify=False,
                config_dir=td,
                builtin_skills_root="",
                workspace_skills_root="",
                on_conflict="abort",
            )
            captured = io.StringIO()
            with patch.object(self.mod, "_search", side_effect=AssertionError("must not search")):
                with patch.object(self.mod, "_fetch_text", return_value=detail_html):
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
## SKILL.md
---
name: demo-conflict
description: demo conflict
---

# Demo Conflict
content

### Files
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
                detail_url="https://clawhub.ai/skills/demo-conflict",
                insecure=False,
                no_verify=False,
                config_dir=td,
                builtin_skills_root="",
                workspace_skills_root="",
                on_conflict="abort",
            )
            captured = io.StringIO()
            with patch.object(self.mod, "_fetch_text", return_value=detail_html):
                with patch("builtins.input", side_effect=AssertionError("must not prompt")):
                    with patch("sys.stdout", new=captured):
                        rc = self.mod.cmd_install(args)
            self.assertEqual(rc, 3)
            self.assertIn("Install aborted due to config conflict.", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
