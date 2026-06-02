import unittest
from pathlib import Path
from types import SimpleNamespace
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
        self.assertIn("first run `rg`", updated)
        self.assertIn("first use `rg` to locate matches", updated)
        self.assertNotIn("{{SYSTEM_FILE_SEARCH_NEARBY_RULE}}", updated)
        self.assertNotIn("{{TOOLS_FILE_SEARCH_NEARBY_RULE}}", updated)

    def test_render_workspace_prompt_variables_uses_fallback_rules_when_rg_unavailable(self):
        original = (
            "{{SYSTEM_FILE_SEARCH_NEARBY_RULE}}\n{{TOOLS_FILE_SEARCH_NEARBY_RULE}}"
        )
        with patch("src.runtime.prompt_composer.has_usable_workspace_rg_bin", return_value=False):
            updated = prompt_composer.render_workspace_prompt_variables(original)
        self.assertIn("first run a search such as `Select-String` or `rg`", updated)
        self.assertIn("first locate matches", updated)

    def test_build_os_file_ops_prompt_append_uses_rg_search_line_when_available(self):
        with patch("src.runtime.prompt_composer.has_usable_workspace_rg_bin", return_value=True):
            text = prompt_composer.build_os_file_ops_prompt_append()
        self.assertIn("first use `rg` to locate matches", text)

    def test_build_os_file_ops_prompt_append_uses_generic_search_line_when_rg_unavailable(self):
        with patch("src.runtime.prompt_composer.has_usable_workspace_rg_bin", return_value=False):
            text = prompt_composer.build_os_file_ops_prompt_append()
        self.assertIn("first locate matches", text)
        self.assertNotIn("first use `rg` to locate matches", text)

    def test_standard_tools_prompt_strips_simulated_json_examples(self):
        agent = SimpleNamespace(
            tools_prompt_template=prompt_composer.load_tools_prompt_template(),
            tool_specs=[
                {
                    "function": {
                        "name": "shell",
                        "description": "Run command",
                        "parameters": {"properties": {"command": {}}},
                    }
                }
            ],
            _project_context_tool_allowed=lambda: True,
            _use_standard_openai_tools_call=lambda: True,
        )

        text = prompt_composer.build_tools_prompt_append(agent)

        self.assertIn("Tool Call Mode: Standard API tool_calls", text)
        self.assertIn("HARD REQUIREMENT", text)
        self.assertIn("content-only assistant message is invalid", text)
        self.assertIn("call `done` through standard API `tool_calls`", text)
        self.assertIn("Visible text may contain only user-visible", text)
        self.assertIn("Never print any tool-call representation", text)
        self.assertIn("Available tools:", text)
        self.assertNotIn('{"tool"', text)
        self.assertNotIn("```json", text)

    def test_standard_skill_section_hint_uses_standard_tools_not_json(self):
        text, meta = prompt_composer.render_skill_section_payload(
            sections=["part 1", "part 2"],
            requested_section=1,
            full=False,
            initial_sections=1,
        )

        self.assertFalse(meta.get("full"))
        self.assertIn("standard tools", text)
        self.assertIn("section=2", text)
        self.assertNotIn('{"tool"', text)
        self.assertNotIn("```json", text)


if __name__ == "__main__":
    unittest.main()
