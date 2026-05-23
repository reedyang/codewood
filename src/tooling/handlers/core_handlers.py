from __future__ import annotations

from typing import Any, Dict, Optional


def _handle_batch(agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    commands = params.get("commands", [])
    results = []
    all_success = True

    for subcmd in commands:
        sub_action = (subcmd.get("tool") or subcmd.get("action") or "").strip()
        sub_args = subcmd.get("args")
        if not isinstance(sub_args, dict):
            sub_args = subcmd.get("params")
        if not isinstance(sub_args, dict):
            sub_args = {}

        sub_result = agent.execute_tool_call(sub_action, sub_args)
        results.append({"action": sub_action, "result": sub_result})

        if (not sub_result.get("success", True)) and (
            "user cancelled" in str(sub_result.get("error", "")).lower()
        ):
            return {"success": False, "error": "User cancelled operation", "results": results}

        if not sub_result.get("success", True):
            all_success = False

    return {"success": all_success, "results": results}


def dispatch_core_tool(agent: Any, action: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if action == "done":
        return {"success": True, "message": "Task completed", "finished": True}

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

    if action == "cls":
        import os

        os.system("cls" if os.name == "nt" else "clear")
        return {"success": True, "message": "Screen cleared"}

    if action == "batch":
        return _handle_batch(agent, params)

    return None
