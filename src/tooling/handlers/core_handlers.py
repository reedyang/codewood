from __future__ import annotations

from typing import Any, Dict, Optional


def dispatch_core_tool(agent: Any, action: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if action == "ask_more_info":
        question = str(params.get("question") or "").strip() or "Please provide more details to continue."
        expected = params.get("expected_fields")
        if not isinstance(expected, list):
            expected = []
        expected_fields = [str(x).strip() for x in expected if str(x).strip()]
        return {
            "success": True,
            "needs_user_input": True,
            "input_type": "supplement",
            "question": question,
            "expected_fields": expected_fields,
            "retryable": False,
            "message": "Waiting for user supplement.",
        }

    return None
