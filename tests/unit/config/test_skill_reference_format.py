import sys
import types
import unittest


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.agent import Agent
from src.completion.slash_dynamic_completions import build_slash_dynamic_rules


class SkillReferenceFormatTests(unittest.TestCase):
    def setUp(self):
        self.agent = Agent.__new__(Agent)
        self.agent.skills = [
            types.SimpleNamespace(skill_id="alpha-skill", name="Alpha"),
            types.SimpleNamespace(skill_id="beta-skill", name="Beta"),
        ]

    def test_slash_skill_commands_use_skills_root_only(self):
        commands = self.agent._get_slash_skill_commands()
        self.assertEqual(commands, ["/skills/"])

    def test_slash_skill_target_commands_are_dynamic(self):
        commands = self.agent._get_slash_skill_target_commands()
        self.assertIn("/skills/alpha-skill", commands)
        self.assertIn("/skills/beta-skill", commands)
        self.assertNotIn("/alpha-skill", commands)
        self.assertNotIn("/skills alpha-skill", commands)

    def test_extract_forced_skill_reference_supports_new_format(self):
        parsed = self.agent._extract_forced_skill_reference("/skills/alpha-skill help me do a code review")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["skills"][0]["skill_id"], "alpha-skill")
        self.assertEqual(parsed["rest"], "help me do a code review")

    def test_extract_forced_skill_reference_deduplicates_new_format(self):
        parsed = self.agent._extract_forced_skill_reference("/skills/alpha-skill /skills/alpha-skill run")
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed["skills"]), 1)
        self.assertEqual(parsed["skills"][0]["skill_id"], "alpha-skill")
        self.assertEqual(parsed["rest"], "run")

    def test_extract_forced_skill_reference_supports_bracket_pill_format(self):
        # The GUI composes skill references as ``[skill: name]`` inline
        # pills. The backend must recognise that form and inject the skill
        # prompt directly (the previous slash-only matcher silently ignored
        # GUI references, leaving the model to call request_skill_prompt).
        parsed = self.agent._extract_forced_skill_reference(
            "[skill: alpha-skill] help me do a code review"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["skills"][0]["skill_id"], "alpha-skill")
        self.assertEqual(parsed["rest"], "help me do a code review")

    def test_extract_forced_skill_reference_bracket_matches_by_display_name(self):
        parsed = self.agent._extract_forced_skill_reference("[skill: Alpha] go")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["skills"][0]["skill_id"], "alpha-skill")
        self.assertEqual(parsed["rest"], "go")

    def test_extract_forced_skill_reference_mixed_forms(self):
        parsed = self.agent._extract_forced_skill_reference(
            "[skill: alpha-skill] and /skills/beta-skill now"
        )
        self.assertIsNotNone(parsed)
        ids = {s["skill_id"] for s in parsed["skills"]}
        self.assertEqual(ids, {"alpha-skill", "beta-skill"})

    def test_display_normalizer_converts_slash_skill_to_bracket_pill(self):
        # TUI echo/edit should present the unified ``[skill: name]`` form
        # rather than the raw ``/skills/name`` slash token.
        out = self.agent._normalize_reference_pills_for_display(
            "/skills/alpha-skill install a gmail skill"
        )
        # ANSI color is wrapped around the pill; assert the pill text is
        # present and the slash form is gone.
        self.assertIn("[skill: alpha-skill]", out)
        self.assertNotIn("/skills/alpha-skill", out)
        self.assertIn("install a gmail skill", out)

    def test_display_normalizer_keeps_existing_bracket_pill_text(self):
        out = self.agent._normalize_reference_pills_for_display(
            "[skill: alpha-skill] go"
        )
        self.assertIn("[skill: alpha-skill]", out)

    def test_display_normalizer_converts_slash_mcp_reference(self):
        out = self.agent._normalize_reference_pills_for_display(
            "/mcp/playwright/browser_click do it"
        )
        self.assertIn("[mcp: playwright/browser_click]", out)
        self.assertNotIn("/mcp/playwright/browser_click", out)

    def test_dynamic_rules_include_skills_trigger_and_candidates(self):
        rules = build_slash_dynamic_rules(
            workspaces_state={},
            mcp_config={},
            mcp_scoped_groups_provider=lambda: [],
            skill_targets_provider=self.agent._get_slash_skill_target_commands,
        )
        skills_rules = [r for r in rules if r.get("trigger") == "/skills/"]
        self.assertEqual(len(skills_rules), 1)
        candidates = skills_rules[0].get("candidates", [])
        self.assertIn("/skills/alpha-skill", candidates)


if __name__ == "__main__":
    unittest.main()

