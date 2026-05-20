import unittest
from unittest.mock import patch

from src.tooling.execution_engine import _print_with_auto_hide_tracking


class _DummyAgent:
    def __init__(self):
        self.calls = []

    def _register_shell_output_for_auto_hide(self, stdout_text, stderr_text=""):
        self.calls.append((str(stdout_text), str(stderr_text)))


class ExecutionEngineOutputTrackingTests(unittest.TestCase):
    def test_print_with_auto_hide_tracking_does_not_register_auto_hide(self):
        agent = _DummyAgent()
        with patch("builtins.print"):
            _print_with_auto_hide_tracking(agent, "ok")
        self.assertEqual(agent.calls, [])


if __name__ == "__main__":
    unittest.main()
