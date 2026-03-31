import importlib.util
import io
import sys
import unittest
import zipfile
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
