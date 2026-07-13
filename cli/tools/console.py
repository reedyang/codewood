"""GUI-only embedded-console tools.

These let the model drive the desktop GUI's embedded console. They are only
exposed under the GUI (the serve app sets ``agent._console_dispatch``). All
tools operate on the ACTIVE console tab: the model shares the user's live,
interactive shell session (cwd, environment and history are shared), so there
is no tab-id parameter to manage.

``console_exec`` writes a command into the running shell; ``console_read``
returns a range of buffered output lines plus the absolute total line count;
``console_info`` returns the active shell's basic info (kind, cwd, size).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base import BaseTool


def _dispatch(agent: Any, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fn = getattr(agent, "_console_dispatch", None)
    if not callable(fn):
        return {"success": False, "error": "console is not available (GUI only)"}
    try:
        return fn(action, payload or {})
    except Exception as exc:  # never let a transport error crash the loop
        return {"success": False, "error": str(exc)}


class ConsoleExecTool(BaseTool):
    name = "console_exec"
    description = "Run a command in the active embedded console tab. If no console tab is open, a default one is auto-opened. Use console_read to get the output."
    requires_gui = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The command line to run in the active console.",
            },
        },
        "required": ["command"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        command = str((params or {}).get("command") or "")
        if not command.strip():
            return {"success": False, "error": "missing command"}
        return _dispatch(agent, "exec", {"command": command})


class ConsoleReadTool(BaseTool):
    name = "console_read"
    description = "Read output lines from the active embedded console. Returns totalLines for pagination."
    requires_gui = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "start": {
                "type": "integer",
                "description": "Absolute index of the first line to read (0-based). "
                "Read `console_info`/a prior `console_read` `totalLines` to find the end.",
            },
            "count": {
                "type": "integer",
                "description": "How many lines to read (default 200, capped).",
            },
        },
        "required": ["start"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        p = params or {}
        try:
            start = int(p.get("start") or 0)
        except (TypeError, ValueError):
            start = 0
        try:
            count = int(p.get("count") or 200)
        except (TypeError, ValueError):
            count = 200
        return _dispatch(agent, "read", {"start": start, "count": count})


class ConsoleInfoTool(BaseTool):
    name = "console_info"
    description = "Return info about the active embedded console: shell, cwd, dimensions, line count."
    requires_gui = True
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        return _dispatch(agent, "info")
