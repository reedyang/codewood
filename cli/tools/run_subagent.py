"""Tool: run_subagent."""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from ..core.console_utils import GUI_SUBAGENT_SESSION_BEGIN, GUI_SUBAGENT_SESSION_END
from .base import BaseTool


class RunSubagentTool(BaseTool):
    name = "run_subagent"
    description = "Delegate a subtask to a configured sub-agent. Returns a text result to continue the main task."
    requires_subagents = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "subagent": {
                "type": "string",
                "description": "The name of the sub-agent to invoke (must match one of the available sub-agents).",
            },
            "topic": {
                "type": "string",
                "description": "A short, concise topic describing what this sub-agent call is about. Displayed in the GUI chat title bar while viewing the sub-session.",
            },
            "prompt": {
                "type": "string",
                "description": "A complete, self-contained task description for the sub-agent, including all context it needs. Write it in the SAME language the user is using in their message (do not translate or switch languages when delegating).",
            },
            "image": {
                "type": "string",
                "description": "Optional path to an image file to attach to the sub-agent. Use this to delegate image analysis to a multimodal sub-agent (e.g. when the main model is not multimodal). The image is analyzed by the sub-agent's own model, not the main model.",
            },
            "background": {
                "type": "boolean",
                "description": (
                    "Run the sub-agent as a background task. Returns immediately "
                    "with a background_task_id (same as the tool call id); the "
                    "sub-agent keeps running independently. Manage it with "
                    "`background_task_status`, `wait` and `background_task_kill`; "
                    "its result is delivered to you automatically when it finishes."
                ),
            },
        },
        "required": [
            "subagent",
            "topic",
            "prompt",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..core.localization import get_display_language, translate

        def _t(key: str, **kwargs: object) -> str:
            return translate(key, get_display_language(agent), **kwargs)

        args = params if isinstance(params, dict) else {}
        subagent = str(args.get("subagent") or "").strip()
        topic = str(args.get("topic") or "").strip()
        prompt = str(args.get("prompt") or "").strip()
        image = str(args.get("image") or "").strip() or None
        if not subagent:
            return {"success": False, "error": _t("subagents.error.missing_subagent")}
        if not topic:
            return {"success": False, "error": _t("subagents.error.missing_topic")}
        if not prompt:
            return {"success": False, "error": _t("subagents.error.empty_prompt")}

        from ..subagents.executor import run_subagent

        background = bool(args.get("background", False))
        if not background:
            return run_subagent(agent, subagent, prompt, image=image, topic=topic)
        return _start_background_subagent(agent, subagent, topic, prompt, image)


def _start_background_subagent(
    agent: Any,
    subagent: str,
    topic: str,
    prompt: str,
    image: Optional[str],
) -> Dict[str, Any]:
    """Run the sub-agent on a background worker thread and register it with
    the shared ``BackgroundTaskManager`` so ``background_task_status`` /
    ``wait`` / ``background_task_kill`` and the completion-notification
    pipeline all work exactly like background shell tasks.

    The tool returns immediately with ``{success, background, background_task_id,
    status: "running"}``.  The worker thread runs the normal sub-agent executor
    (which still emits live ``sub_agent_*`` SSE events so the GUI session
    viewer works); when it finishes the manager finalizes the record: it
    collapses the result text, rewrites the persisted tool round, emits the
    final ``background_task_output`` SSE event and queues the hidden internal
    user message with the result.
    """
    from ..subagents.executor import run_subagent as _run_subagent
    from .background_tasks import BackgroundTaskManager

    task_id = ""
    try:
        task_id = str(agent._next_tool_call_id() or "").strip()
    except Exception:
        task_id = ""
    if not task_id:
        import secrets

        task_id = f"bg_{secrets.token_hex(8)}"

    mgr = getattr(agent, "_background_task_manager", None)
    if mgr is None:
        mgr = BackgroundTaskManager(agent)
        agent._background_task_manager = mgr

    done_event = threading.Event()
    abort_event = threading.Event()
    stdout_chunks: List[str] = []
    stream_lock = threading.Lock()
    worker_state: Dict[str, Any] = {"done": done_event}

    chat_key = ""
    try:
        chat_key = str(agent._current_session_chat_key() or "")
    except Exception:
        pass
    cwd = str(getattr(agent, "work_directory", "") or "")
    command = f"run_subagent(subagent={subagent!r}, topic={topic!r})"

    record = mgr.register_task(
        task_id=task_id,
        agent=agent,
        chat_key=chat_key,
        command=command,
        cwd=cwd,
        process_ref={},
        worker_state=worker_state,
        stdout_chunks=stdout_chunks,
        stream_chunks_lock=stream_lock,
        merge_path=None,
        sink=None,
        kind="subagent",
        cancel=abort_event.set,
    )
    mgr.watch(record)

    def _push_session_marker(session_id: str) -> None:
        """Push ``\\ue008<sessionId>\\ue009`` into the GUI tool-call block the
        moment the worker created the session, so the "open sub-session"
        button is available WHILE the sub-agent is still running (the initial
        tool result cannot carry the id: the session only exists later)."""
        hook = getattr(agent, "_gui_bg_task_output_emit", None)
        if not callable(hook):
            return
        try:
            marker = (
                f"{GUI_SUBAGENT_SESSION_BEGIN}{session_id}"
                f"{GUI_SUBAGENT_SESSION_END}"
            )
            hook(record.task_id, marker, end=False, status="running")
        except Exception:
            pass

    def _worker() -> None:
        try:
            # The sub-agent runs on a fresh thread; inherit the parent chat's
            # workspace override so its tools/prompts resolve against the
            # parent chat's workspace even when another workspace is focused.
            get_ctx = getattr(agent, "_workspace_ctx", None)
            set_ctx = getattr(agent, "_set_workspace_ctx", None)
            if callable(get_ctx) and callable(set_ctx):
                try:
                    ctx = get_ctx()
                    if ctx:
                        set_ctx(ctx)
                except Exception:
                    pass
            result = _run_subagent(
                agent,
                subagent,
                prompt,
                image=image,
                topic=topic,
                cancel_check=abort_event.is_set,
                suppress_session_marker=True,
                on_session_created=_push_session_marker,
            )
        except Exception as exc:  # never lose the worker
            result = {"success": False, "error": f"background subagent crashed: {exc}"}
        if not isinstance(result, dict):
            result = {"success": False, "error": str(result)}
        out_text = str(
            result.get("output") or result.get("error") or result.get("message") or ""
        )
        success = bool(result.get("success", False))
        aborted = abort_event.is_set()
        rc = 0 if success else 1
        if aborted:
            rc = -1
        with stream_lock:
            stdout_chunks.append(out_text or "")
        # Persist the session marker so the finalizer can write it back into
        # the raw tool round — the GUI uses it to render the "open sub-session"
        # button next to the run_subagent tool-call block (the initial tool
        # result cannot carry it: the session is only created by the worker).
        marker = str(result.get("_guiSessionMarker") or "")
        if marker:
            record.session_marker = marker
        worker_state["return_code"] = rc
        worker_state["aborted"] = aborted
        worker_state["done"].set()

    threading.Thread(
        target=_worker,
        name="codewood-subagent-bg",
        daemon=True,
    ).start()

    return {
        "success": True,
        "background": True,
        "background_task_id": task_id,
        "status": "running",
        "subagent": subagent,
        "message": (
            f"Sub-agent '{subagent}' is running in background (id={task_id}); "
            "use `background_task_status` to query it, `wait` to block until "
            "it finishes, and `background_task_kill` to cancel it."
        ),
        "output": (
            "Background sub-agent started "
            f"(id={task_id}, subagent={subagent}). It keeps running in the "
            "background; use `background_task_status` to query it, `wait` to "
            "block until it finishes, and `background_task_kill` to cancel it."
        ),
        "return_code": None,
    }
