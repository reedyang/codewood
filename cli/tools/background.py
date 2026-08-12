"""Tools to manage background shell tasks and to block on time/task completion.

``background_task_kill`` terminates a running background task (process tree).
``background_task_status`` returns the status and (partial or final) output of
a background task, plus the full-output file path when available.
``wait`` blocks the current turn for a number of seconds (required) and
optionally until a specific background task finishes first.
"""

from __future__ import annotations

from typing import Any, Dict

from .background_tasks import (
    BG_TASK_STATUS_NOT_FOUND,
    BG_TASK_STATUS_RUNNING,
    BackgroundTaskManager,
)
from .base import BaseTool


def _manager(agent: Any) -> BackgroundTaskManager:
    mgr = getattr(agent, "_background_task_manager", None)
    if mgr is None:
        mgr = BackgroundTaskManager(agent)
        agent._background_task_manager = mgr
    return mgr


class BackgroundTaskKillTool(BaseTool):
    name = "background_task_kill"
    description = (
        "Force-terminate a background shell task started with shell "
        "background=true. Provide the background_task_id returned by the "
        "shell tool call."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "background_task_id": {"type": "string"},
        },
        "required": ["background_task_id"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        task_id = str(params.get("background_task_id") or "").strip()
        if not task_id:
            return {
                "success": False,
                "error": "missing required parameter: background_task_id",
            }
        try:
            return _manager(agent).kill(task_id)
        except Exception as exc:  # pragma: no cover - defensive
            return {"success": False, "error": f"background_task_kill failed: {exc}"}


class BackgroundTaskStatusTool(BaseTool):
    name = "background_task_status"
    description = (
        "Query the status and current/final output of a background shell task "
        "started with shell background=true. Returns the background_task_id, "
        "status (running/completed/failed/killed), return code, bounded output "
        "and the full_output_path of the complete output file."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "background_task_id": {"type": "string"},
        },
        "required": ["background_task_id"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        task_id = str(params.get("background_task_id") or "").strip()
        if not task_id:
            return {
                "success": False,
                "error": "missing required parameter: background_task_id",
            }
        try:
            return _manager(agent).status(task_id)
        except Exception as exc:  # pragma: no cover - defensive
            return {"success": False, "error": f"background_task_status failed: {exc}"}


class WaitTool(BaseTool):
    name = "wait"
    description = (
        "Block this turn for a number of seconds (required). When "
        "background_task_id is given, the wait ends as soon as that background "
        "task finishes (or fails/killed), whichever comes first, and the "
        "result payload includes the task's final status and output."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "seconds": {
                "type": "integer",
                "description": "Maximum seconds to wait (0-3600).",
            },
            "background_task_id": {
                "type": "string",
                "description": (
                    "Optional background task id to wait for; returns early "
                    "when the task finishes before the timeout."
                ),
            },
        },
        "required": ["seconds"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        try:
            seconds = int(params.get("seconds") or 0)
        except Exception:
            return {"success": False, "error": "seconds must be an integer"}
        seconds = max(0, min(3600, seconds))
        task_id = str(params.get("background_task_id") or "").strip()
        try:
            mgr = _manager(agent)
            if task_id:
                record = mgr.get(task_id)
                if record is None:
                    return {
                        "success": False,
                        "error": f"unknown background task id: {task_id}",
                        "background_task_id": task_id,
                        "status": BG_TASK_STATUS_NOT_FOUND,
                    }
            return mgr.wait(task_id or None, seconds)
        except Exception as exc:  # pragma: no cover - defensive
            return {"success": False, "error": f"wait failed: {exc}"}


__all__ = [
    "BackgroundTaskKillTool",
    "BackgroundTaskStatusTool",
    "WaitTool",
]
