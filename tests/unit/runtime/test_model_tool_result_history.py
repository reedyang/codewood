import json
import sys
import types
import unittest


if "ollama" not in sys.modules:
    fake_ollama = types.SimpleNamespace(list=lambda: {"models": []})
    sys.modules["ollama"] = fake_ollama

from src.agent import Agent, MODEL_TOOL_RESULT_HISTORY_PREFIX


class ModelToolResultHistoryTests(unittest.TestCase):
    def setUp(self):
        # These helper methods are pure and do not require full Agent init.
        self.agent = Agent.__new__(Agent)

    def _decode_payload(self, content: str):
        self.assertTrue(content.startswith(MODEL_TOOL_RESULT_HISTORY_PREFIX))
        raw = content[len(MODEL_TOOL_RESULT_HISTORY_PREFIX):]
        return json.loads(raw)

    def test_failed_apply_patch_history_keeps_error_details(self):
        content = self.agent._build_model_tool_result_history_content(
            tool_name="apply_patch",
            args={"path": "bin/start.bat", "patch": "@@ -1,1 +1,1 @@\n-a\n+b\n"},
            result={
                "success": False,
                "error": "Patch context mismatch (line 10)",
            },
        )
        payload = self._decode_payload(content)
        self.assertFalse(payload.get("success"))
        self.assertEqual(payload.get("error"), "Patch context mismatch (line 10)")
        # Failed non-shell tools used to produce empty output in history.
        self.assertEqual(payload.get("output"), "Patch context mismatch (line 10)")

    def test_failed_tool_falls_back_to_message_when_error_missing(self):
        content = self.agent._build_model_tool_result_history_content(
            tool_name="apply_patch",
            args={"path": "README.md", "patch": "@@ -1,1 +1,1 @@\n-a\n+b\n"},
            result={
                "success": False,
                "message": "Operation cancelled by user",
            },
        )
        payload = self._decode_payload(content)
        self.assertFalse(payload.get("success"))
        self.assertEqual(payload.get("error"), "")
        self.assertEqual(payload.get("message"), "Operation cancelled by user")
        self.assertEqual(payload.get("output"), "Operation cancelled by user")


if __name__ == "__main__":
    unittest.main()
