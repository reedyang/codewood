"""Tool: request_user_input."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class RequestUserInputTool(BaseTool):
    name = "request_user_input"
    description = "When key input is missing, ask the user a single-choice or multiple-choice question and pause auto-continuation until the user responds. Call this only after trying memory_search and other tools/skills/MCP in order and still being unable to obtain the information reliably. The host renders the options as buttons (single-choice: pick one; multi-choice: tick any subset and click Submit). The host always appends an extra 'Other' choice so the user can type a freeform answer; that freeform text is concatenated with any ticked options. Execution resumes with the user's answer as the supplement. Important: each entry in `options` MUST be the full human-readable label the user needs to choose (not a bare index/code), and you must NOT also print the same option list in your natural-language reply — the buttons rendered from `options` are the only enumeration the user should see."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The clarifying question shown to the user. Keep it short and answerable from the option list below.",
            },
            "options": {
                "type": "array",
                "description": "Two or more discrete answer choices the user can pick from. Each item MUST be the full human-readable label (e.g. the package name, URL, file path, or short phrase the user needs to read) — NOT a bare index, code, or `1`/`2` placeholder. Ideal length is under ~10 words but use whatever is needed to make the choice meaningful. The host always appends an extra 'Other' option for freeform input, so do NOT include one here.",
                "items": {"type": "string"},
                "minItems": 2,
            },
            "multi_select": {
                "type": "boolean",
                "description": "When true the user may tick multiple options and the supplement comes back as a `; `-separated list. When false (default) the user picks exactly one option.",
            },
        },
        "required": ["question", "options"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        question = str(params.get("question") or "").strip() or "Please provide more details to continue."
        raw_options = params.get("options")
        options: list[str] = []
        if isinstance(raw_options, list):
            # Cap label length so a runaway model can't fill the screen and
            # cap the count so the option strip stays scannable; dedupe in
            # original order to keep the host's chips deterministic.
            seen: set[str] = set()
            for item in raw_options:
                label = str(item or "").strip()[:120]
                if not label or label in seen:
                    continue
                seen.add(label)
                options.append(label)
                if len(options) >= 16:
                    break
        if len(options) < 2:
            # Reject with a retryable error so the model can re-issue the
            # call with a valid options array; the loop relays the error
            # back as the next-round input.
            return {
                "success": False,
                "needs_user_input": False,
                "retryable": True,
                "error": (
                    "request_user_input requires at least two `options` (e.g. "
                    "[\"A\", \"B\"]). The host always appends an extra "
                    "'Other' option for freeform input, so do not include it "
                    "yourself."
                ),
            }
        # ``multi_select`` is optional and defaults to single-choice; we
        # coerce explicitly so a stringy "true"/"false" from a loose JSON
        # implementation doesn't get treated as truthy-by-accident.
        raw_multi = params.get("multi_select", False)
        if isinstance(raw_multi, bool):
            multi_select = raw_multi
        elif isinstance(raw_multi, str):
            multi_select = raw_multi.strip().lower() in ("1", "true", "yes", "y")
        else:
            multi_select = bool(raw_multi)
        return {
            "success": True,
            "needs_user_input": True,
            "input_type": "supplement",
            "question": question,
            "options": options,
            "multi_select": multi_select,
            "retryable": False,
            "message": "Waiting for user selection.",
        }
