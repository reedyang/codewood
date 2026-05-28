from __future__ import annotations

from typing import Any, Dict, Optional


def _parse_reviewed_files(params: Dict[str, Any]) -> list[str]:
    raw = params.get("reviewed_files")
    if isinstance(raw, str):
        s = raw.strip()
        return [s] if s else []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        s = str(item or "").strip()
        if s:
            out.append(s)
    return out


def dispatch_core_tool(agent: Any, action: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if action == "done":
        reviewed_files = _parse_reviewed_files(params if isinstance(params, dict) else {})
        return {
            "success": True,
            "message": "Task completed",
            "finished": True,
            "reviewed_files": reviewed_files,
        }

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

    if action == "task_changed":
        new_task = str(params.get("new_task") or "").strip()
        reason = str(params.get("reason") or "").strip()
        if not new_task:
            return {"success": False, "error": "task_changed missing new_task"}
        return {
            "success": True,
            "task_changed": True,
            "new_task": new_task,
            "reason": reason or "User input changed the task focus.",
            "message": "Task switched.",
        }

    return None
