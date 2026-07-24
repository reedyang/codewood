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
from .shell import is_ai_workspace_script_command, is_dependency_install_command


def _t(agent: Any, key: str, fallback: Optional[str] = None, **kwargs: Any) -> str:
    from ..core.localization import get_display_language, translate

    return translate(key, get_display_language(agent), fallback=fallback, **kwargs)


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
        # Enforce the same running-app protection as the ``shell`` tool so the
        # guard cannot be trivially bypassed via the interactive console.
        policy = getattr(agent, "path_policy", None)
        if policy is not None and hasattr(policy, "can_run_shell_in_workdir"):
            decision = policy.can_run_shell_in_workdir(
                is_dependency_install=is_dependency_install_command(command),
                is_ai_workspace_script=is_ai_workspace_script_command(agent, command),
            )
            if not decision.get("allowed", False):
                return {"success": False, "error": decision.get("error", "")}
        # Enforce the same execution-policy confirmation as the ``shell`` tool.
        agent._load_confirm_allowlist()
        execution_policy = str(getattr(agent, "execution_policy", "confirmation")).lower()
        in_allowlist = agent._shell_command_in_allowlist(command)
        should_prompt = (
            (execution_policy == "confirmation")
            or (execution_policy in ("moderate", "unlimited") and not in_allowlist)
        )
        if should_prompt:
            prompt_text = _t(
                agent,
                "execution_policy.prompt.confirm_shell_no_command",
                fallback="⚠️ Confirm executing this command in the embedded console?",
            )
            ok = agent._prompt_confirm_yes_no_maybe_always(
                prompt_text,
                offer_always=agent._shell_confirm_should_offer_always(command),
                kind="console",
                shell_command=command,
                display_command=command,
            )
            if not ok:
                return {"success": False, "error": "Operation cancelled by user"}
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
