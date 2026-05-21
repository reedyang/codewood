import unittest

from src.completion.builtin_slash_commands import slash_builtin_completions
from src.completion.slash_dynamic_completions import build_model_switch_commands


class ModelCompletionOrderTests(unittest.TestCase):
    def test_build_model_switch_commands_preserves_input_order(self):
        selectors = ["openwebui:b", "openai:a", "ollama:c"]
        commands = build_model_switch_commands(selectors)
        self.assertEqual(
            commands,
            ["/model openwebui:b", "/model openai:a", "/model ollama:c"],
        )

    def test_windows_completion_keeps_model_candidates_order(self):
        delayed_groups = [
            (
                "/model ",
                ["/model openwebui:b", "/model openai:a", "/model ollama:c"],
            )
        ]
        out = slash_builtin_completions(
            "/model ",
            dynamic_commands=[],
            delayed_dynamic_groups=delayed_groups,
        )
        self.assertEqual(
            out,
            ["/model openwebui:b", "/model openai:a", "/model ollama:c"],
        )


if __name__ == "__main__":
    unittest.main()
