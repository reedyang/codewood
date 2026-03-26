import sys
import types
import unittest


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from agent.smart_shell_agent import SmartShellAgent


class AgentJsonCommandExtractTests(unittest.TestCase):
    def setUp(self):
        # Bypass heavy __init__; extractor methods only rely on self helpers.
        self.agent = SmartShellAgent.__new__(SmartShellAgent)

    def test_extract_skips_non_executable_action_and_selects_done(self):
        text = """
已完成调用，返回如下：
```json
{
  "action": "accept",
  "title": "User Profile Collection",
  "values": {"name": "name-value", "age": 0}
}
```

```json
{"action":"done","last_action":true}
```
"""
        cmd = self.agent.extract_json_command(text)
        self.assertIsInstance(cmd, dict)
        self.assertEqual(cmd.get("action"), "done")


if __name__ == "__main__":
    unittest.main()
