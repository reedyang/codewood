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

