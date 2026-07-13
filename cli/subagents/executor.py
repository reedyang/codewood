"""Run a configured sub-agent in an isolated nested agentic loop.

The sub-agent uses its own model (optional selector), its own system
instructions (the markdown body), and a configurable tool allowlist. Its
message history is fully isolated: it never touches ``agent.conversation_history``
or ``agent.operation_results``. The final text answer is returned to the caller,
which feeds it back into the main loop's context.

Sub-agent sessions are persisted to disk under the chat's data directory so
they can be reviewed later via the GUI's sub-agent session viewer.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.logging.app_logging import get_logger

logger = get_logger()

from ..ai.ai_orchestrator import AIOrchestrator, AgentAIContext
from ..ai.ai_provider_clients import AICallContext, resolve_api_mode
from ..core.config.subagents_loader import SubAgentRecord
from ..core.localization import get_display_language, translate
from ..core.console_utils import (
    GUI_CMD_OUTPUT_BEGIN,
    GUI_CMD_OUTPUT_END,
    GUI_SUBAGENT_SESSION_BEGIN,
    GUI_SUBAGENT_SESSION_END,
)


class SubAgentSessionStore:
    """Manages persistence of sub-agent sessions to disk.

    Sessions are stored as JSON files under the chat's data directory:
    ``chats/data/<chat-id>/subagent-sessions/<session-id>.json``
    """

    _SUBAGENT_SESSIONS_DIRNAME = "subagent-sessions"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: Dict[str, Dict[str, Any]] = {}

    def _session_dir(self, agent: Any, chat_id: str) -> Optional[Path]:
        """Resolve the subagent-sessions directory for a chat."""
        try:
            mgr = getattr(agent, "_chat_state_manager", None)
            if mgr is None:
                logger.debug("_session_dir: no _chat_state_manager")
                return None
            data_dir = mgr.chat_data_dir_for_chat(chat_id)
            if data_dir is None:
                logger.debug("_session_dir: chat_data_dir_for_chat(%r) returned None", chat_id)
                return None
            session_dir = data_dir / self._SUBAGENT_SESSIONS_DIRNAME
            session_dir.mkdir(parents=True, exist_ok=True)
            logger.debug("_session_dir: resolved %s (exists=%s)", session_dir, session_dir.exists())
            return session_dir
        except Exception as exc:
            logger.debug("_session_dir: exception: %s", exc)
            return None

    def create_session(
        self,
        agent: Any,
        chat_id: str,
        name: str,
        description: str,
        prompt: str,
    ) -> Dict[str, Any]:
        """Create a new sub-agent session and persist it to disk."""
        session_id = f"sa_{uuid.uuid4().hex[:12]}"
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        session: Dict[str, Any] = {
            "id": session_id,
            "name": name,
            "description": description,
            "prompt": prompt,
            "startedAt": now,
            "endedAt": None,
            "messages": [],
            "output": None,
            "success": None,
            "max_rounds_reached": False,
            # Internal bookkeeping so later mutations (e.g. attaching
            # tool_rounds) can re-persist without re-passing chat_id.
            "_chat_id": chat_id,
        }
        logger.info("create_session: id=%s, chat_id=%r, name=%s", session_id, chat_id, name)
        with self._lock:
            self._cache[session_id] = session
        self._persist(agent, chat_id, session)
        return session

    def append_message(
        self,
        agent: Any,
        chat_id: str,
        session_id: str,
        message: Dict[str, Any],
    ) -> None:
        """Append a message to an existing session."""
        with self._lock:
            session = self._cache.get(session_id)
            if session is None:
                return
            session["messages"].append(message)
        self._persist(agent, chat_id, session)

    def set_assistant_tool_rounds(
        self,
        session_id: str,
        tool_rounds: List[str],
    ) -> None:
        """Attach the rendered ``tool_rounds`` display text to the most recent
        assistant message in an existing session.

        Sub-agent tool calls are rendered into the same display envelope the
        main chat uses (a "• Ran <tool> <args>" prompt + output, wrapped in the
        GUI sentinels) and stored as a single list on the assistant message,
        rather than as separate raw tool messages. This lets the GUI session
        viewer render them through the identical ``StepsView`` path.
        """
        if not tool_rounds:
            return
        with self._lock:
            session = self._cache.get(session_id)
            if session is None:
                return
            # The last appended message is the assistant turn we just stored.
            for msg in reversed(session["messages"]):
                if msg.get("role") == "assistant":
                    msg["tool_rounds"] = list(tool_rounds)
                    break
            chat_id = session.get("_chat_id")
        if chat_id is not None:
            self._persist(None, chat_id, session)

    def finish_session(
        self,
        agent: Any,
        chat_id: str,
        session_id: str,
        output: str,
        success: bool,
        max_rounds_reached: bool = False,
    ) -> None:
        """Mark a session as finished and persist the final state."""
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            session = self._cache.get(session_id)
            if session is None:
                logger.warning("finish_session: session %s not in cache", session_id)
                return
            session["endedAt"] = now
            session["output"] = output
            session["success"] = success
            session["max_rounds_reached"] = max_rounds_reached
        logger.info("finish_session: id=%s, chat_id=%r, success=%s", session_id, chat_id, success)
        self._persist(agent, chat_id, session)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a session from the in-memory cache."""
        with self._lock:
            return self._cache.get(session_id)

    def load_session_from_disk(self, agent: Any, chat_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        """Load a session from disk if not in cache."""
        with self._lock:
            if session_id in self._cache:
                return self._cache[session_id]
        session_dir = self._session_dir(agent, chat_id)
        if session_dir is None:
            return None
        session_file = session_dir / f"{session_id}.json"
        if not session_file.exists():
            return None
        try:
            with open(session_file, "r", encoding="utf-8") as f:
                session = json.load(f)
            with self._lock:
                self._cache[session_id] = session
            return session
        except Exception:
            return None

    def list_sessions(self, agent: Any, chat_id: str) -> List[Dict[str, Any]]:
        """List all sessions for a chat from disk."""
        session_dir = self._session_dir(agent, chat_id)
        if session_dir is None:
            return []
        sessions = []
        try:
            for f in sorted(session_dir.glob("sa_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        session = json.load(fh)
                    sessions.append(session)
                    with self._lock:
                        self._cache[session["id"]] = session
                except Exception:
                    continue
        except Exception:
            pass
        return sessions

    def delete_session(self, agent: Any, chat_id: str, session_id: str) -> None:
        """Delete a sub-agent session file and remove it from the in-memory cache."""
        session_dir = self._session_dir(agent, chat_id)
        if session_dir is None:
            return
        session_file = session_dir / f"{session_id}.json"
        try:
            if session_file.exists():
                session_file.unlink()
        except Exception:
            pass
        with self._lock:
            self._cache.pop(session_id, None)

    def _persist(self, agent: Any, chat_id: str, session: Dict[str, Any]) -> None:
        """Write a session to disk."""
        session_dir = self._session_dir(agent, chat_id)
        if session_dir is None:
            logger.warning("_persist: session_dir is None for chat_id=%r, skipping disk write", chat_id)
            return
        session_file = session_dir / f"{session['id']}.json"
        try:
            tmp = session_file.with_suffix(".tmp")
            # Strip internal bookkeeping keys (session-level only) before
            # writing so the persisted JSON exposes only the public schema.
            clean = {k: v for k, v in session.items() if not k.startswith("_chat")}
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=2)
            os.replace(str(tmp), str(session_file))
            logger.debug("_persist: wrote %s (%d bytes)", session_file, session_file.stat().st_size)
        except Exception as exc:
            logger.warning("_persist: failed to write %s: %s", session_file, exc)


# Module-level session store singleton
_session_store = SubAgentSessionStore()


def _render_subagent_tool_round(
    agent: Any,
    tool_name: str,
    args: Dict[str, Any],
    tool_result: Dict[str, Any],
) -> str:
    """Render one sub-agent tool call into the exact same display envelope the
    main chat uses, so the GUI session viewer can show it through the identical
    ``StepsView`` / ``PromptWithAttachment`` path.

    Produces a "• <label> <detail>" prompt line (with the main chat's
    ANSI-colored bullet and full relative paths / brackets) wrapped in the
    ``GUI_CMD_PROMPT_*`` sentinels, followed by the human-readable result
    wrapped in the ``GUI_CMD_OUTPUT_*`` sentinels.
    """
    failed = not bool((tool_result or {}).get("success", True))
    prompt = ""
    formatter = getattr(agent, "_format_tool_call_feedback_line", None)
    if callable(formatter):
        try:
            prompt = formatter(str(tool_name or ""), args if isinstance(args, dict) else {}, failed=failed)
        except Exception:
            prompt = ""
    if not prompt:
        # Fallback if the agent helper is unavailable.
        bullet = "\u2022"
        prompt = f"{GUI_CMD_PROMPT_BEGIN}{bullet} {str(tool_name or 'tool')}{GUI_CMD_PROMPT_END}"

    # Extract the human-readable result payload (mirrors the main chat's
    # _build_model_tool_result_history_content).
    r = tool_result if isinstance(tool_result, dict) else {}
    content = r.get("content")
    output_text = str(content) if isinstance(content, str) and content else ""
    if not output_text:
        output_text = str(r.get("output") or "")
    if not output_text:
        if not bool(r.get("success", True)):
            output_text = str(r.get("error") or r.get("message") or "")
        else:
            data = {k: v for k, v in r.items() if k not in _META_KEYS}
            if data:
                try:
                    output_text = json.dumps(data, ensure_ascii=False, default=str)
                except Exception:
                    output_text = ""
    return f"{prompt}\n{GUI_CMD_OUTPUT_BEGIN}{output_text}{GUI_CMD_OUTPUT_END}"


# Keys that carry metadata rather than user-facing tool output; mirrored from
# agent._build_model_tool_result_history_content so non-success tool results
# with tool-specific data keys still surface their payload.
_META_KEYS = {
    "success", "error", "message", "return_code", "output", "content",
    "file", "call", "server", "tool", "prompt", "uri", "arguments",
    "from_cache", "count", "total_count", "ok_count",
    "error_count", "has_error", "calls",
}


def get_session_store() -> SubAgentSessionStore:
    """Return the module-level session store singleton."""
    return _session_store


# Default core tool set granted to a sub-agent when its frontmatter omits
# ``tools``. ``run_subagent`` is always excluded to prevent recursion.
DEFAULT_SUBAGENT_TOOLS = (
    "shell",
    "apply_patch",
    "read",
    "project_context_search",
    "update_plan",
    "request_skill_prompt",
)

_EXCLUDED_SUBAGENT_TOOLS = {"run_subagent"}


def _t(agent: Any, key: str, **kwargs: object) -> str:
    return translate(key, get_display_language(agent), **kwargs)


def _find_subagent(agent: Any, name: str) -> Optional[SubAgentRecord]:
    needle = str(name or "").strip().lower()
    if not needle:
        return None
    for rec in list(getattr(agent, "subagents", []) or []):
        if str(getattr(rec, "name", "") or "").strip().lower() == needle:
            return rec
    return None


def _resolve_subagent_model(
    agent: Any, record: SubAgentRecord
) -> Tuple[Optional[Tuple[str, str, Dict[str, Any]]], Optional[str]]:
    """Resolve the sub-agent's (provider, model_name, params).

    Returns ``(resolved, None)`` on success or ``(None, error_message)`` when a
    declared ``model`` selector cannot be resolved. A missing selector means
    "reuse the main agent's current model".
    """
    selector = str(getattr(record, "model_selector", "") or "").strip()
    if not selector:
        provider = str(getattr(agent, "provider", "") or "")
        model_name = str(getattr(agent, "model_name", "") or "")
        params = dict(getattr(agent, "params", {}) or {})
        return (provider, model_name, params), None

    choice = agent._find_configured_model_choice(selector)
    if not choice:
        # Do NOT silently fall back to the main model: that would route image/
        # specialized work to the wrong (e.g. non-multimodal) model and produce
        # confusing results. Surface a clear, actionable error instead.
        try:
            available = ", ".join(s for s in agent._get_configured_model_selectors() if s) or "-"
        except Exception:
            available = "-"
        return None, _t(
            agent,
            "subagents.error.model_not_found",
            selector=selector,
            subagent=record.name,
            available=available,
        )

    provider = str(choice.get("provider") or "").strip() or str(getattr(agent, "provider", "") or "")
    model_name = str(choice.get("name") or "").strip() or str(getattr(agent, "model_name", "") or "")
    params = dict(choice.get("params") or {})
    if model_name:
        params["model"] = model_name
    return (provider, model_name, params), None


def _build_orchestrator(
    agent: Any, provider: str, model_name: str, params: Dict[str, Any]
) -> AIOrchestrator:
    """Build a throwaway orchestrator for the sub-agent (no main-agent mutation)."""
    params = dict(params or {})
    api_mode = resolve_api_mode(params=params, provider=provider)
    openai_conf = None if api_mode == "ollama" else params

    def _noop_history_writer(role: str, content: str) -> None:  # isolated: no history
        return None

    def _unused_message_builder(user_input: str, context: str):  # never used (messages_override)
        return [], False

    return AIOrchestrator(
        AgentAIContext(
            provider=provider,
            model_name=model_name,
            model_params=params,
            openai_conf=openai_conf,
            history_writer=_noop_history_writer,
            regular_message_builder=_unused_message_builder,
            ollama_importer=lambda: None,
            workspace_root=str(getattr(agent, "workspace_root", "") or ""),
            self_repo_root=str(getattr(agent, "_self_repo_root", "") or ""),
            display_language=get_display_language(agent),
        )
    )


def _resolve_allowed_tool_schemas(agent: Any, record: SubAgentRecord) -> List[Dict[str, Any]]:
    allowlist = [str(x).strip() for x in (getattr(record, "tools", []) or []) if str(x).strip()]
    if not getattr(record, "tools_specified", False):
        # Frontmatter omitted ``tools`` entirely -> grant the default core set.
        allowlist = list(DEFAULT_SUBAGENT_TOOLS)
    # When ``tools_specified`` is True we honor ``allowlist`` exactly, so an
    # explicit empty ``tools: []`` yields no tools at all.
    allowed = {name.lower() for name in allowlist} - {x.lower() for x in _EXCLUDED_SUBAGENT_TOOLS}

    out: List[Dict[str, Any]] = []
    for spec in (getattr(agent, "tool_specs", []) or []):
        fn = (spec or {}).get("function", {})
        name = str(fn.get("name") or "").strip()
        if not name or name.lower() in _EXCLUDED_SUBAGENT_TOOLS:
            continue
        if name.lower() in allowed:
            out.append(spec)
    return out


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}


def _candidate_image_paths(agent: Any, raw: str) -> List[Path]:
    """Build an ordered list of candidate absolute paths for an image argument.

    Relative paths are resolved against the same bases the rest of the app uses
    (workspace_root via the canonical resolver, then work_directory, the AI temp
    dir, the workspace config dir, and finally the process CWD), so a relative
    path like ``test.jpg`` works without the caller needing an absolute path.
    """
    candidates: List[Path] = []

    def _add(p: Optional[Path]) -> None:
        if p is None:
            return
        try:
            resolved = p.expanduser()
        except Exception:
            resolved = p
        candidates.append(resolved)

    candidate = Path(raw)
    if candidate.is_absolute():
        _add(candidate)
        return candidates

    # Canonical resolver first: matches how apply_patch / shell-relative paths
    # behave (workspace_root, then work_directory).
    resolver = getattr(agent, "_resolve_user_path", None)
    if callable(resolver):
        try:
            _add(Path(resolver(raw)))
        except Exception:
            pass

    for root in (
        getattr(agent, "workspace_root", None),
        getattr(agent, "work_directory", None),
        getattr(agent, "ai_workspace_temp_dir", None),
        getattr(agent, "workspace_config_dir", None),
    ):
        if root is not None:
            _add(Path(root) / raw)
    try:
        _add(Path.cwd() / raw)
    except Exception:
        pass
    _add(candidate)
    return candidates


def _resolve_image_path(agent: Any, image: str) -> Optional[str]:
    """Resolve an image argument to an existing absolute image-file path.

    Returns the absolute path string, or ``None`` if no candidate resolves to an
    existing supported image file.
    """
    raw = str(image or "").strip().strip('"').strip("'")
    if not raw:
        return None
    for candidate in _candidate_image_paths(agent, raw):
        try:
            if candidate.is_file() and candidate.suffix.lower() in _IMAGE_EXTS:
                return str(candidate.resolve())
        except OSError:
            continue
    return None


def _extract_tool_call_id(message: Dict[str, Any], index: int) -> str:
    try:
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and index < len(tool_calls):
            entry = tool_calls[index]
            if isinstance(entry, dict):
                cid = str(entry.get("id") or "").strip()
                if cid:
                    return cid
    except Exception:
        pass
    return f"call_{index}"


def _emit_subagent_event(agent: Any, event: str, data: Dict[str, Any]) -> None:
    """Emit a sub-agent SSE event via the agent's broadcaster hook.

    The serve_app sets up ``_gui_subagent_event`` as a callable that publishes
    to the SSE broadcaster with the correct chat/workspace routing.
    """
    hook = getattr(agent, "_gui_subagent_event", None)
    if callable(hook):
        try:
            hook(event, data)
        except Exception:
            pass


def _get_active_chat_id(agent: Any) -> str:
    """Get the current active chat ID from the agent."""
    try:
        state = getattr(agent, "_chat_state", None)
        if isinstance(state, dict):
            chat_id = str(state.get("active") or "")
            logger.debug("_get_active_chat_id: _chat_state keys=%s, active=%s", list(state.keys()), chat_id)
            return chat_id
        else:
            logger.debug("_get_active_chat_id: _chat_state is %s, not dict", type(state))
    except Exception as exc:
        logger.debug("_get_active_chat_id: exception: %s", exc)
    return ""


def run_subagent(
    agent: Any,
    subagent_name: str,
    prompt: str,
    image: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a sub-agent and return ``{success, output}`` (or error).

    When ``image`` is provided, it is attached to the sub-agent's own model
    calls so a multimodal sub-agent can analyze it directly (independent of the
    main agent's model).

    The sub-agent session is persisted to disk and SSE events are emitted
    for real-time viewing in the GUI.
    """
    # Recursion guard: forbid nesting.
    if int(getattr(agent, "_subagent_depth", 0) or 0) > 0:
        return {
            "success": False,
            "error": _t(agent, "subagents.error.nesting_forbidden"),
        }

    record = _find_subagent(agent, subagent_name)
    if record is None:
        available = ", ".join(
            sorted(str(getattr(r, "name", "")) for r in (getattr(agent, "subagents", []) or []))
        ) or "-"
        return {
            "success": False,
            "error": _t(agent, "subagents.error.not_found", name=subagent_name, available=available),
        }

    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        return {"success": False, "error": _t(agent, "subagents.error.empty_prompt")}

    image_path: Optional[str] = None
    if image:
        image_path = _resolve_image_path(agent, image)
        if image_path is None:
            return {
                "success": False,
                "error": _t(agent, "subagents.error.image_not_found", path=str(image)),
                "subagent": record.name,
            }

    resolved_model, model_error = _resolve_subagent_model(agent, record)
    if model_error:
        return {"success": False, "error": model_error, "subagent": record.name}

    # Import here to avoid a circular import at module load time.
    from ..runtime.runtime_loop import _parse_tool_plans_from_model_message

    provider, model_name, model_params = resolved_model
    orchestrator = _build_orchestrator(agent, provider, model_name, model_params)
    tool_schemas = _resolve_allowed_tool_schemas(agent, record)

    # Create a session for tracking and persistence
    chat_id = _get_active_chat_id(agent)
    store = get_session_store()
    session = store.create_session(
        agent=agent,
        chat_id=chat_id,
        name=record.name,
        description=str(record.description or ""),
        prompt=prompt_text,
    )
    session_id = session["id"]

    # Emit session start event
    _emit_subagent_event(agent, "sub_agent_start", {
        "sessionId": session_id,
        "name": record.name,
        "description": str(record.description or ""),
        "prompt": prompt_text,
    })

    # Store the initial messages (system + user)
    store.append_message(agent, chat_id, session_id, {
        "role": "system",
        "content": str(record.instructions or ""),
    })
    store.append_message(agent, chat_id, session_id, {
        "role": "user",
        "content": prompt_text,
    })

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": str(record.instructions or "")},
        {"role": "user", "content": prompt_text},
    ]

    max_rounds = int(getattr(record, "max_rounds", 20) or 20)
    last_assistant_text = ""

    agent._subagent_depth = int(getattr(agent, "_subagent_depth", 0) or 0) + 1
    try:
        for _round in range(max_rounds):
            call_ctx = AICallContext(
                user_input="",
                messages_override=list(messages),
                record_history_override=False,
                return_message=True,
                stream=False,
                tool_schemas=tool_schemas or None,
                tool_choice="auto" if tool_schemas else None,
                # Re-attach the image on every round: ``prepare_image_input``
                # injects it into the request payload only, never into the
                # stored ``messages``, so it must be supplied each call to stay
                # visible to the sub-agent's multimodal model.
                image_path=image_path,
            )
            message = orchestrator.call(call_ctx=call_ctx)

            if isinstance(message, str):
                # Provider returned an error string (no message dict).
                store.finish_session(agent, chat_id, session_id, message, False)
                _emit_subagent_event(agent, "sub_agent_end", {
                    "sessionId": session_id,
                    "output": message,
                    "success": False,
                })
                return {
                    "success": False,
                    "error": message,
                    "subagent": record.name,
                    "sessionId": session_id,
                    "_guiSessionMarker": f"{GUI_SUBAGENT_SESSION_BEGIN}{session_id}{GUI_SUBAGENT_SESSION_END}",
                }
            if not isinstance(message, dict):
                error_msg = _t(agent, "subagents.error.bad_response")
                store.finish_session(agent, chat_id, session_id, error_msg, False)
                _emit_subagent_event(agent, "sub_agent_end", {
                    "sessionId": session_id,
                    "output": error_msg,
                    "success": False,
                })
                return {
                    "success": False,
                    "error": error_msg,
                    "subagent": record.name,
                    "sessionId": session_id,
                    "_guiSessionMarker": f"{GUI_SUBAGENT_SESSION_BEGIN}{session_id}{GUI_SUBAGENT_SESSION_END}",
                }

            content_text = str(message.get("content") or "").strip()
            if content_text:
                last_assistant_text = content_text

            plans = _parse_tool_plans_from_model_message(message)
            if not plans:
                # No tool calls -> final answer.
                output = content_text or last_assistant_text
                store.finish_session(agent, chat_id, session_id, output, True)
                _emit_subagent_event(agent, "sub_agent_end", {
                    "sessionId": session_id,
                    "output": output,
                    "success": True,
                })
                return {
                    "success": True,
                    "output": output,
                    "subagent": record.name,
                    "sessionId": session_id,
                    "_guiSessionMarker": f"{GUI_SUBAGENT_SESSION_BEGIN}{session_id}{GUI_SUBAGENT_SESSION_END}",
                }

            # Record the assistant turn (with its tool_calls) so the follow-up
            # tool messages are valid in the next request.
            assistant_msg = dict(message)
            messages.append(assistant_msg)
            store.append_message(agent, chat_id, session_id, assistant_msg)

            # Emit assistant content event if there's text
            if content_text:
                _emit_subagent_event(agent, "sub_agent_assistant", {
                    "sessionId": session_id,
                    "text": content_text,
                })

            # Collect each tool call + result so we can render them into the
            # same display envelope the main chat uses (a single collapsible
            # "Tool calls: N" block), stored on the assistant message instead
            # of as separate raw tool messages.
            round_tools: List[Dict[str, Any]] = []
            for idx, (tool_name, args) in enumerate(plans):
                call_id = _extract_tool_call_id(message, idx)

                # Emit tool call event
                _emit_subagent_event(agent, "sub_agent_tool_call", {
                    "sessionId": session_id,
                    "toolName": str(tool_name),
                    "args": args if isinstance(args, dict) else {},
                })

                if str(tool_name).strip().lower() in _EXCLUDED_SUBAGENT_TOOLS:
                    tool_result: Dict[str, Any] = {
                        "success": False,
                        "error": _t(agent, "subagents.error.nesting_forbidden"),
                    }
                else:
                    try:
                        tool_result = agent.execute_tool_call(tool_name, args if isinstance(args, dict) else {})
                    except Exception as e:  # never let a tool crash the sub-agent loop
                        tool_result = {"success": False, "error": str(e)}
                try:
                    result_text = json.dumps(tool_result, ensure_ascii=False)
                except Exception:
                    result_text = str(tool_result)

                # Keep the raw tool message in the in-memory loop context so the
                # follow-up request stays valid; do NOT persist it separately.
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": str(tool_name),
                    "content": result_text,
                }
                messages.append(tool_msg)

                # Also persist the raw tool message so the session record is a
                # faithful archive of the sub-agent's real interaction protocol.
                store.append_message(agent, chat_id, session_id, tool_msg)

                round_tools.append({
                    "tool_name": str(tool_name),
                    "args": args if isinstance(args, dict) else {},
                    "tool_result": tool_result,
                })

                # Emit tool output event
                _emit_subagent_event(agent, "sub_agent_output", {
                    "sessionId": session_id,
                    "text": result_text[:5000],  # Truncate very long outputs for SSE
                    "toolName": str(tool_name),
                })

            # Render the collected tool calls via the main-chat display envelope
            # and attach them to the persisted assistant message so the GUI
            # session viewer matches the main transcript exactly.
            if round_tools:
                tool_rounds = [
                    _render_subagent_tool_round(
                        agent,
                        rt["tool_name"],
                        rt["args"],
                        rt["tool_result"],
                    )
                    for rt in round_tools
                ]
                store.set_assistant_tool_rounds(session_id, tool_rounds)

        # max_rounds exhausted: return the last text we have.
        output = last_assistant_text or _t(agent, "subagents.error.max_rounds", rounds=max_rounds)
        store.finish_session(agent, chat_id, session_id, output, True, max_rounds_reached=True)
        _emit_subagent_event(agent, "sub_agent_end", {
            "sessionId": session_id,
            "output": output,
            "success": True,
            "max_rounds_reached": True,
        })
        return {
            "success": True,
            "output": output,
            "subagent": record.name,
            "max_rounds_reached": True,
            "sessionId": session_id,
            "_guiSessionMarker": f"{GUI_SUBAGENT_SESSION_BEGIN}{session_id}{GUI_SUBAGENT_SESSION_END}",
        }
    finally:
        agent._subagent_depth = max(0, int(getattr(agent, "_subagent_depth", 1) or 1) - 1)
