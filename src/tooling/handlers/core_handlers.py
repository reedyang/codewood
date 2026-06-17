from __future__ import annotations

from typing import Any, Dict, Optional

from ...tools.plan import UpdatePlanTool


def dispatch_core_tool(agent: Any, action: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if action == "update_plan":
        return UpdatePlanTool.apply(agent, params if isinstance(params, dict) else {})

    if action == "ask_more_info":
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
                    "ask_more_info requires at least two `options` (e.g. "
                    "[\"A\", \"B\"]). The host always appends an extra "
                    "'Other' option for freeform input, so do not include it "
                    "yourself."
                ),
            }
        return {
            "success": True,
            "needs_user_input": True,
            "input_type": "supplement",
            "question": question,
            "options": options,
            "retryable": False,
            "message": "Waiting for user selection.",
        }

    return None
