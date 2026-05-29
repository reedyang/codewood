import unittest
from pathlib import Path
from unittest.mock import patch

from src.runtime import prompt_composer


class PromptComposerTests(unittest.TestCase):
    def test_has_usable_workspace_rg_bin_true_when_candidate_is_usable(self):
        candidate = Path("D:/repo/bin/rg.exe")
        with patch("src.runtime.prompt_composer._rg_bin_candidates", return_value=[candidate]):
            with patch(
                "src.runtime.prompt_composer._is_usable_rg_executable",
                side_effect=lambda p: p == candidate,
            ):
                self.assertTrue(prompt_composer.has_usable_workspace_rg_bin())

    def test_has_usable_workspace_rg_bin_false_when_all_candidates_unusable(self):
        candidates = [Path("D:/repo/bin/rg.exe"), Path("D:/repo/bin/rg.cmd")]
        with patch("src.runtime.prompt_composer._rg_bin_candidates", return_value=candidates):
            with patch("src.runtime.prompt_composer._is_usable_rg_executable", return_value=False):
                self.assertFalse(prompt_composer.has_usable_workspace_rg_bin())

    def test_render_workspace_prompt_variables_uses_rg_rules_when_rg_available(self):
        original = (
            "A\n"
            "{{SYSTEM_FILE_SEARCH_NEARBY_RULE}}\n"
            "{{TOOLS_FILE_SEARCH_NEARBY_RULE}}\n"
            "B"
        )
        with patch("src.runtime.prompt_composer.has_usable_workspace_rg_bin", return_value=True):
            updated = prompt_composer.render_workspace_prompt_variables(original)
        self.assertIn("必须先执行 `rg` 检索", updated)
        self.assertIn("必须先用 `rg` 检索命中位置", updated)
        self.assertNotIn("{{SYSTEM_FILE_SEARCH_NEARBY_RULE}}", updated)
        self.assertNotIn("{{TOOLS_FILE_SEARCH_NEARBY_RULE}}", updated)

    def test_render_workspace_prompt_variables_uses_fallback_rules_when_rg_unavailable(self):
        original = (
            "{{SYSTEM_FILE_SEARCH_NEARBY_RULE}}\n{{TOOLS_FILE_SEARCH_NEARBY_RULE}}"
        )
        with patch("src.runtime.prompt_composer.has_usable_workspace_rg_bin", return_value=False):
            updated = prompt_composer.render_workspace_prompt_variables(original)
        self.assertIn("必须先执行检索（如 `Select-String`/`rg`）", updated)
        self.assertIn("必须先检索命中位置", updated)

    def test_build_os_file_ops_prompt_append_uses_rg_search_line_when_available(self):
        with patch("src.runtime.prompt_composer.has_usable_workspace_rg_bin", return_value=True):
            text = prompt_composer.build_os_file_ops_prompt_append()
        self.assertIn("必须先用 `rg` 检索命中位置", text)

    def test_build_os_file_ops_prompt_append_uses_generic_search_line_when_rg_unavailable(self):
        with patch("src.runtime.prompt_composer.has_usable_workspace_rg_bin", return_value=False):
            text = prompt_composer.build_os_file_ops_prompt_append()
        self.assertIn("必须先检索命中位置", text)
        self.assertNotIn("必须先用 `rg` 检索命中位置", text)


if __name__ == "__main__":
    unittest.main()
