"""Unit tests for McpGetPromptTool injection deduplication."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from cli.tools.mcp_get_prompt import McpGetPromptTool


class McpGetPromptDedupTests(unittest.TestCase):
    def _make_agent(self, already_injected=None, get_prompt_result=None):
        calls = []

        def fake_get_prompt(server, name, arguments, timeout_s=20.0):
            calls.append((server, name, dict(arguments or {})))
            return get_prompt_result

        agent = SimpleNamespace(
            mcp_manager=SimpleNamespace(get_prompt=fake_get_prompt),
            _session_injected_mcp_prompts=set(already_injected or []),
        )
        return agent, calls

    def test_already_injected_returns_lightweight_without_fetching(self):
        agent, calls = self._make_agent(
            already_injected=["csp-ai-agent/csp-ai-agent-setup"],
            get_prompt_result={"description": "d", "messages": []},
        )
        result = McpGetPromptTool().execute(
            agent,
            {"server": "csp-ai-agent", "prompt": "csp-ai-agent-setup"},
        )
        self.assertTrue(result.get("success"))
        self.assertTrue(result.get("already_injected"))
        self.assertNotIn("result", result)
        # get_prompt must NOT be called again for an already-injected prompt.
        self.assertEqual(calls, [])

    def test_fresh_prompt_is_fetched_and_tracked(self):
        agent, calls = self._make_agent(
            get_prompt_result={"description": "d", "messages": []},
        )
        result = McpGetPromptTool().execute(
            agent,
            {"server": "csp-ai-agent", "prompt": "csp-ai-agent-setup"},
        )
        self.assertTrue(result.get("success"))
        self.assertNotIn("already_injected", result)
        self.assertIn("result", result)
        self.assertEqual(calls, [("csp-ai-agent", "csp-ai-agent-setup", {})])
        self.assertIn(
            "csp-ai-agent/csp-ai-agent-setup", agent._session_injected_mcp_prompts
        )


if __name__ == "__main__":
    unittest.main()
