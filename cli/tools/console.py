"""GUI-only embedded-console tools.

These let the model drive the desktop GUI's embedded console. They are only
exposed under the GUI (the serve app sets ``agent._console_dispatch``). All
tools operate on the ACTIVE console tab: the model shares the user's live,
interactive shell session (cwd, environment and history are shared), so there
is no tab-id parameter to manage.

``console_exec`` writes a command into the running shell; ``console_read``
returns a range of buffered output lines plus the absolute total line count;
``console_info`` returns the active shell's basic info (kind, cwd, size);
``console_send`` writes arbitrary text/keystrokes (for interactive programs
such as gdb); ``console_wait`` blocks until new output appears; 
``console_interrupt`` sends Ctrl+C; ``console_resize`` changes the terminal
dimensions.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from .base import BaseTool
from .shell import is_ai_workspace_script_command, is_dependency_install_command


def _t(agent: Any, key: str, fallback: Optional[str] = None, **kwargs: Any) -> str:
    from ..core.localization import get_display_language, translate

    return translate(key, get_display_language(agent), fallback=fallback, **kwargs)


_ESCAPE_RE = re.compile(r"\\(?:n|r|t|e|\\|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4})")


def _decode_escapes(text: str) -> str:
    """Decode backslash escape sequences in ``text`` into control characters.

    Supported: ``\\n`` ``\\r`` ``\\t`` ``\\e`` (ESC) ``\\\\`` ``\\xNN``
    ``\\uNNNN``. Used by ``console_send`` with ``interpretEscapes=true`` so the
    model can express Enter (``\\r``), Ctrl+C (``\\x03``), arrow keys
    (``\\u001b[A``), etc. as portable text.
    """

    def _sub(match: "re.Match[str]") -> str:
        esc = match.group(0)
        if esc == "\\n":
            return "\n"
        if esc == "\\r":
            return "\r"
        if esc == "\\t":
            return "\t"
        if esc == "\\e":
            return "\x1b"
        if esc == "\\\\":
            return "\\"
        return chr(int(esc[2:], 16))

    return _ESCAPE_RE.sub(_sub, text)


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
                builder = getattr(agent, "_confirm_declined_result", None)
                if callable(builder):
                    return builder("Operation cancelled by user")
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


class ConsoleSendTool(BaseTool):
    name = "console_send"
    description = (
        "Send raw text/keystrokes to the active embedded console. Unlike console_exec, "
        "nothing is appended: end data with \\r (Enter) to submit, e.g. \"next\\r\" at "
        "a (gdb) prompt. Use it to interact with interactive programs (gdb, debuggers, "
        "REPLs, prompts): reply y/n, or send control keys such as \\x03 (Ctrl+C) and "
        "\\u001b[A (Up arrow). Escape sequences are decoded by default "
        "(interpretEscapes defaults to true); write \\\\ for a literal backslash. "
        "Does not auto-open a console; requires an active tab."
    )
    requires_gui = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "data": {
                "type": "string",
                "description": (
                    "Text/keystrokes to send. End with \\r to submit (\\r is decoded "
                    "to the Enter key). Example: \"next\\r\", \"y\", \"\\u001b[B\" (Down)."
                ),
            },
            "interpretEscapes": {
                "type": "boolean",
                "description": (
                    "If true, decode escape sequences in data: \\n \\r \\t \\e \\\\ "
                    "\\xNN \\uNNNN (e.g. \"\\x03\" is Ctrl+C, \"\\u001b[A\" is Up arrow). "
                    "Default true."
                ),
            },
        },
        "required": ["data"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        p = params or {}
        text = str(p.get("data") or "")
        if not text:
            return {"success": False, "error": "missing data"}
        if bool(p.get("interpretEscapes", True)):
            text = _decode_escapes(text)
        return _dispatch(agent, "send", {"data": text})


class ConsoleWaitTool(BaseTool):
    name = "console_wait"
    description = (
        "Wait for new output in the active embedded console, then read it. Blocks up "
        "to `timeout` seconds until new output appears, then returns the new lines "
        "plus totalLines. Without `start` it waits for output after the last "
        "console_read/console_wait position (recommended). Use after "
        "console_send/console_exec to synchronize with interactive programs, e.g. wait "
        "for the next (gdb) prompt instead of polling console_read. Returns "
        "immediately if lines past `start` already exist. For long tasks with "
        "continuous output (download progress bars, percent refresh, build "
        "logs that rewrite the screen), set `stable` true: the wait then "
        "returns only after the output stops changing for 3s, so one call "
        "awaits the task end instead of returning on every refresh frame."
    )
    requires_gui = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "start": {
                "type": "integer",
                "description": (
                    "Optional absolute line index to wait past (default: the position "
                    "after the previous console_read/console_wait)."
                ),
            },
            "count": {
                "type": "integer",
                "description": "Max lines to return (default 200, capped 2000).",
            },
            "timeout": {
                "type": "number",
                "description": (
                    "Seconds to wait for new output (default 5, max 30). Returns "
                    "immediately if the session closes."
                ),
            },
            "stable": {
                "type": "boolean",
                "description": (
                    "If true, wait until the output settles (3s quiet after the last "
                    "change). Use for progress bars / percent updates / long tasks "
                    "with continuous output. Default false."
                ),
            },
        },
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        p = params or {}
        raw_start = p.get("start")
        if raw_start is None:
            start = None
        else:
            try:
                start = int(raw_start)
            except (TypeError, ValueError):
                start = None
        try:
            count = int(p.get("count") or 200)
        except (TypeError, ValueError):
            count = 200
        try:
            timeout = float(p.get("timeout") or 5)
        except (TypeError, ValueError):
            timeout = 5
        stable = bool(p.get("stable"))
        return _dispatch(
            agent, "wait", {"start": start, "count": count, "timeout": timeout, "stable": stable}
        )


class ConsoleInterruptTool(BaseTool):
    name = "console_interrupt"
    description = (
        "Send Ctrl+C (SIGINT) to the active embedded console. Use to interrupt a "
        "running program or debugger (gdb) and return to its prompt."
    )
    requires_gui = True
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        return _dispatch(agent, "interrupt")


class ConsoleResizeTool(BaseTool):
    name = "console_resize"
    description = (
        "Resize the active embedded console (cols x rows, 1-2000). Needed for "
        "full-screen TUI programs such as gdb TUI mode."
    )
    requires_gui = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "cols": {"type": "integer", "description": "New width (1-2000)."},
            "rows": {"type": "integer", "description": "New height (1-2000)."},
        },
        "required": ["cols", "rows"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        p = params or {}
        return _dispatch(agent, "resize", {"cols": p.get("cols"), "rows": p.get("rows")})
