import io
import unittest
from contextlib import redirect_stdout

from src.controllers.model_command_controller import handle_model_builtin_command


class _FakeModelAgent:
    def __init__(self):
        self.last_selector = None

    def _current_model_selector(self):
        return "openai:gpt-4.1"

    def _get_configured_model_selectors(self):
        return ["openai:gpt-4.1", "ollama:qwen2.5:7b"]

    def _switch_model_by_selector(self, selector: str):
        self.last_selector = selector
        return f"switched:{selector}"


class ModelCommandControllerTests(unittest.TestCase):
    def test_non_model_command_not_handled(self):
        agent = _FakeModelAgent()
        self.assertFalse(handle_model_builtin_command(agent, "chat list"))

    def test_model_without_args_prints_current_and_available(self):
        agent = _FakeModelAgent()
        buf = io.StringIO()
        with redirect_stdout(buf):
            handled = handle_model_builtin_command(agent, "model")
        self.assertTrue(handled)
        out = buf.getvalue()
        self.assertIn("当前模型: openai:gpt-4.1", out)
        self.assertIn("openai:gpt-4.1", out)
        self.assertIn("ollama:qwen2.5:7b", out)

    def test_model_switch_passes_selector_to_agent(self):
        agent = _FakeModelAgent()
        buf = io.StringIO()
        with redirect_stdout(buf):
            handled = handle_model_builtin_command(agent, "model openai:gpt-4.1")
        self.assertTrue(handled)
        self.assertEqual(agent.last_selector, "openai:gpt-4.1")
        self.assertIn("switched:openai:gpt-4.1", buf.getvalue())

    def test_model_switch_with_invalid_format_prints_usage_error(self):
        agent = _FakeModelAgent()
        buf = io.StringIO()
        with redirect_stdout(buf):
            handled = handle_model_builtin_command(agent, "model invalid_format")
        self.assertTrue(handled)
        self.assertIsNone(agent.last_selector)
        self.assertIn("模型格式错误", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
