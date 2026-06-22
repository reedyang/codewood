import unittest

from cli.completion.builtin_slash_commands import (
    SLASH_BUILTIN_COMMANDS,
    SLASH_BUILTIN_DISPLAY_OVERRIDES,
    slash_builtin_completions,
)


class PlanAgentCompletionTests(unittest.TestCase):
    """Plan-mode / Agent-mode toggles must be discoverable in the TUI slash
    completion menu (they are handled by the builtin command router but were
    previously missing from the completion list)."""

    def test_plan_and_agent_are_registered(self):
        for cmd in ("/plan", "/plan off", "/plan status", "/agent"):
            self.assertIn(cmd, SLASH_BUILTIN_COMMANDS)

    def test_typing_slash_p_offers_plan(self):
        out = slash_builtin_completions("/p")
        self.assertIn("/plan", out)

    def test_typing_slash_a_offers_agent(self):
        out = slash_builtin_completions("/a")
        self.assertIn("/agent", out)

    def test_plan_and_agent_have_descriptive_labels(self):
        for cmd in ("/plan", "/plan off", "/plan status", "/agent"):
            self.assertIn(cmd, SLASH_BUILTIN_DISPLAY_OVERRIDES)


if __name__ == "__main__":
    unittest.main()
