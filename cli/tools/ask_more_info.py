"""Tool: ask_more_info."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class AskMoreInfoTool(BaseTool):
    name = "ask_more_info"
    description = "When key input is missing, ask the user a single-choice or multiple-choice question and pause auto-continuation until the user responds. Call this only after trying memory_search and other tools/skills/MCP in order and still being unable to obtain the information reliably. The host renders the options as buttons (single-choice: pick one; multi-choice: tick any subset and click Submit). The host always appends an extra 'Other' choice so the user can type a freeform answer; that freeform text is concatenated with any ticked options. Execution resumes with the user's answer as the supplement. Important: each entry in `options` MUST be the full human-readable label the user needs to choose (not a bare index/code), and you must NOT also print the same option list in your natural-language reply \u2014 the buttons rendered from `options` are the only enumeration the user should see."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The clarifying question shown to the user. Keep it short and answerable from the option list below.",
            },
            "options": {
                "type": "array",
                "description": "Two or more discrete answer choices the user can pick from. Each item MUST be the full human-readable label (e.g. the package name, URL, file path, or short phrase the user needs to read) \u2014 NOT a bare index, code, or `1`/`2` placeholder. Ideal length is under ~10 words but use whatever is needed to make the choice meaningful. The host always appends an extra 'Other' option for freeform input, so do NOT include one here.",
                "items": {
                    "type": "string",
                },
                "minItems": 2,
            },
            "multi_select": {
                "type": "boolean",
                "description": "When true the user may tick multiple options and the supplement comes back as a `; `-separated list. When false (default) the user picks exactly one option.",
            },
        },
        "required": [
            "question",
            "options",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_core

        return delegate_core(agent, "ask_more_info", params if isinstance(params, dict) else {})
