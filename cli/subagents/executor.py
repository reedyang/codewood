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
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from ..core.logging.app_logging import get_logger
from ..core.workspace_scope import (
    effective_workspace_config_dir,
    effective_workspace_root,
)

logger = get_logger()

from ..ai.ai_orchestrator import AIOrchestrator, AgentAIContext, _build_reply_records
from ..ai.ai_provider_clients import (
    AICallContext,
    resolve_api_mode,
    _sanitize_assistant_text,
    _StreamingSanitizer,
)
from ..core.config.subagents_loader import DEFAULT_SUBAGENT_MAX_ROUNDS, SubAgentRecord
from ..core.localization import get_display_language, translate
from ..core.console_utils import (
    GUI_CMD_PROMPT_BEGIN,
    GUI_CMD_PROMPT_END,
    GUI_CMD_OUTPUT_BEGIN,
    GUI_CMD_OUTPUT_END,
    GUI_SUBAGENT_SESSION_BEGIN,
    GUI_SUBAGENT_SESSION_END,
    escape_gui_sentinels,
)


class SubAgentSessionStore:
    """Manages persistence of sub-agent sessions to disk.

    Sessions are stored as JSON files under the chat's data directory:
    ``chats/<YYYY>/<MM>/<DD>/data/<record-stem>/subagent-sessions/<session-id>.json``
    """

    _SUBAGENT_SESSIONS_DIRNAME = "subagent-sessions"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: Dict[str, Dict[str, Any]] = {}
        # Resolved per-chat session directory cache (chat_id -> Path). Lets
        # later mutations persist without re-passing the ``agent`` reference
        # (e.g. the ``set_assistant_tool_rounds_raw`` path).
        self._dirs: Dict[str, Path] = {}

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
            with self._lock:
                self._dirs[chat_id] = session_dir
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
        topic: str = "",
    ) -> Dict[str, Any]:
        """Create a new sub-agent session and persist it to disk."""
        session_id = f"sa_{uuid.uuid4().hex[:12]}"
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        session: Dict[str, Any] = {
            "id": session_id,
            "name": name,
            "description": description,
            "topic": topic,
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

    def attach_image(
        self,
        agent: Any,
        chat_id: str,
        session_id: str,
        src_path: str,
    ) -> Optional[str]:
        """Copy an image passed to a sub-agent into the session directory and
        record it on the session as ``image``.

        The recorded value is the path RELATIVE to the session record file's
        directory (the image lives next to ``<session-id>.json``), so the
        record stays portable and the GUI resolves it against the session dir.
        Returns the absolute path of the copied file (for live SSE events), or
        ``None`` when the copy/record failed.
        """
        try:
            session_dir = self._session_dir(agent, chat_id)
            if session_dir is None:
                return None
            src = Path(str(src_path or ""))
            if not src.is_file():
                return None
            ext = src.suffix.lower() or ".png"
            dest = session_dir / f"{session_id}{ext}"
            if dest.exists():
                dest = session_dir / f"{session_id}_{uuid.uuid4().hex[:6]}{ext}"
            shutil.copy2(str(src), str(dest))
            abs_path = str(dest.resolve())
            with self._lock:
                session = self._cache.get(session_id)
                if session is not None:
                    session["image"] = dest.name
            if session is not None:
                self._persist(agent, chat_id, session)
            return abs_path
        except Exception as exc:
            logger.warning("attach_image: failed for session=%s: %s", session_id, exc)
            return None

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

    def set_assistant_tool_rounds_raw(
        self,
        session_id: str,
        tool_rounds_raw: List[Dict[str, Any]],
        tool_rounds: Optional[List[str]] = None,
    ) -> None:
        """Attach the structured ``_tool_rounds_raw`` entries to the most recent
        assistant message.

        Sub-agent tool calls are recorded with the same shape the main chat
        uses (``{"tool", "args", "failed", "elapsed", "output", ["marker"]}``),
        so the GUI/TUI renderers are shared and descriptions can be re-rendered
        in any language. Only ``_tool_rounds_raw`` is persisted — the rendered
        ``tool_rounds`` display string is NOT stored. The GUI derives both the
        tool description and the expandable tool output from ``_tool_rounds_raw``
        on load (the subagent-session-history endpoint re-renders it through the
        same ``agent._rerender_tool_rounds`` pipeline the main chat uses), so a
        stale pre-rendered blob can never desync from the structured data.
        """
        if not tool_rounds_raw:
            return
        with self._lock:
            session = self._cache.get(session_id)
            if session is None:
                return
            # The last appended message is the assistant turn we just stored.
            for msg in reversed(session["messages"]):
                if msg.get("role") == "assistant":
                    msg["_tool_rounds_raw"] = list(tool_rounds_raw)
                    # Drop any previously persisted pre-rendered tool_rounds so
                    # the GUI is forced to re-derive the display from raw data.
                    msg.pop("tool_rounds", None)
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
        # Fall back to the per-chat cached directory when no agent is supplied
        # (e.g. the ``set_assistant_tool_rounds_raw`` path), so tool_rounds can
        # persist without the agent reference.
        if session_dir is None and chat_id:
            with self._lock:
                session_dir = self._dirs.get(chat_id)
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
        # Fallback: derive a human-friendly "• <label> <detail>" line using the
        # agent's own tool-label logic, so known tools (read, project_context_search,
        # ...) still read naturally instead of a bare "• <tool>" with no detail.
        bullet = "\u2022"
        label = str(tool_name or "tool")
        detail = ""
        natural = getattr(agent, "_natural_tool_action", None)
        humanize = getattr(agent, "_humanize_tool_name", None)
        try:
            if callable(natural):
                label, detail = natural(str(tool_name or "tool"), args if isinstance(args, dict) else {})
            elif callable(humanize):
                label = humanize(str(tool_name or "tool"))
        except Exception:
            pass
        prompt = f"{GUI_CMD_PROMPT_BEGIN}{bullet} {label}{(' ' + detail) if detail else ''}{GUI_CMD_PROMPT_END}"

    # Extract the human-readable result payload.
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
    return f"{prompt}\n{GUI_CMD_OUTPUT_BEGIN}{escape_gui_sentinels(output_text)}{GUI_CMD_OUTPUT_END}"


# Keys that carry metadata rather than user-facing tool output.
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
        # Reuse the main session's *current* model rather than the bare
        # ``agent.params``. ``_session_model_for_call`` returns the model the
        # active chat is pinned to (``sess.call_model_*``), which already carries
        # the user's live ``reasoning_effort``/thinking setting. Using
        # ``agent.params`` directly would silently drop that setting for the
        # sub-agent, so it would never think the way the main session does.
        try:
            resolved = agent._session_model_for_call()
            provider, model_name, params, _openai_conf = resolved
        except Exception:
            provider = str(getattr(agent, "provider", "") or "")
            model_name = str(getattr(agent, "model_name", "") or "")
            params = dict(getattr(agent, "params", {}) or {})
        params = dict(params or {})
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
    # Inherit the user's live reasoning effort unless the selector's own params
    # already pin one. Mirrors the main session so the sub-agent thinks at the
    # same level the user selected.
    try:
        level = agent._current_reasoning_effort()
    except Exception:
        level = ""
    if level and "reasoning_effort" not in params:
        params["reasoning_effort"] = level
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
            workspace_root=str(effective_workspace_root(agent)),
            self_repo_root=str(getattr(agent, "_self_repo_root", "") or ""),
            display_language=get_display_language(agent),
            workspace_config_dir=str(effective_workspace_config_dir(agent)),
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
        effective_workspace_root(agent),
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


def _subagent_cancelled(agent: Any, cancel_check: Optional[Callable[[], bool]]) -> bool:
    """True when the sub-agent should stop: a global interrupt fired, the
    active chat task was interrupted, or an explicit per-task cancel was
    requested (background-task kill).  All checks are defensive — a broken
    hook must never crash the sub-agent loop."""
    try:
        if getattr(agent, "_task_interrupt_requested", False):
            return True
    except Exception:
        pass
    try:
        chat_interrupt = getattr(agent, "_chat_task_interrupt_requested", None)
        if callable(chat_interrupt) and chat_interrupt():
            return True
    except Exception:
        pass
    if cancel_check is not None:
        try:
            if cancel_check():
                return True
        except Exception:
            pass
    return False


def run_subagent(
    agent: Any,
    subagent_name: str,
    prompt: str,
    image: Optional[str] = None,
    topic: str = "",
    cancel_check: Optional[Callable[[], bool]] = None,
    suppress_session_marker: bool = False,
    on_session_created: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Execute a sub-agent and return ``{success, output}`` (or error).

    When ``image`` is provided, it is attached to the sub-agent's own model
    calls so a multimodal sub-agent can analyze it directly (independent of the
    main agent's model).

    The sub-agent session is persisted to disk and SSE events are emitted
    for real-time viewing in the GUI.

    ``cancel_check`` lets a caller (e.g. the background-task worker) abort the
    sub-agent at its next round boundary. ``suppress_session_marker`` hides the
    ``GUI_SUBAGENT_SESSION`` stdout marker — used when the sub-agent runs on a
    background thread whose stdout must not leak into the current turn.
    ``on_session_created`` is invoked with the session id the moment the
    session exists (before any SSE event) — the background worker uses it to
    push the session marker to the GUI while the sub-agent is still running.
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
        topic=topic,
    )
    session_id = session["id"]
    # Persist a copy of the attached image next to the session record and
    # record it as ``session["image"]`` (relative to the record file). The
    # absolute path is returned for the live SSE event; the history endpoint
    # re-resolves the relative value when the session is reopened later.
    image_abs: Optional[str] = None
    if image_path:
        image_abs = store.attach_image(agent, chat_id, session_id, image_path)
    if on_session_created is not None:
        try:
            on_session_created(session_id)
        except Exception:
            pass

    # Emit session start event
    _emit_subagent_event(agent, "sub_agent_start", {
        "sessionId": session_id,
        "name": record.name,
        "topic": topic,
        "description": str(record.description or ""),
        "prompt": prompt_text,
        "image": image_abs or "",
    })

    # Print the session marker early so the GUI can show the ">" button
    # to enter the session viewer while the sub-agent is still running.
    # Skip when stdout is a TTY (TUI mode) — the marker is meaningless there —
    # and for background workers, whose stdout would leak into the wrong turn.
    if not sys.stdout.isatty() and not suppress_session_marker:
        print(f"{GUI_SUBAGENT_SESSION_BEGIN}{session_id}{GUI_SUBAGENT_SESSION_END}", flush=True)

    # Require the sub-agent to reply in the same language the user is using.
    # The caller is instructed (see SubagentsPart / run_subagent tool schema) to
    # write the delegated ``prompt`` in the user's language, so anchoring the
    # response language to the caller's request keeps the whole subtask in the
    # user's tongue end-to-end. The language is auto-detected from the caller's
    # request rather than pinned to any UI display setting.
    language_directive = (
        "\n\n"
        "## Response language\n"
        "Respond in the SAME language the caller used in their request for this subtask. "
        "Do not switch languages when answering."
    )
    system_content = (str(record.instructions or "") + language_directive)

    # Store the initial messages (system + user)
    store.append_message(agent, chat_id, session_id, {
        "role": "system",
        "content": system_content,
    })
    store.append_message(agent, chat_id, session_id, {
        "role": "user",
        "content": prompt_text,
    })

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": prompt_text},
    ]

    # Priority: sub-agent's own max_rounds → main session's max_tool_rounds → unlimited
    record_rounds = record.max_rounds if isinstance(getattr(record, "max_rounds", None), int) else None
    agent_rounds = getattr(agent, "max_tool_rounds", None)
    agent_rounds = int(agent_rounds) if isinstance(agent_rounds, int) and agent_rounds > 0 else None
    max_rounds = record_rounds if record_rounds is not None else agent_rounds
    last_assistant_text = ""

    agent._subagent_depth = int(getattr(agent, "_subagent_depth", 0) or 0) + 1
    _started_at = time.monotonic()
    try:
        _round = 0
        while max_rounds is None or _round < max_rounds:
            _round += 1
            if _subagent_cancelled(agent, cancel_check):
                cancelled_msg = _t(agent, "subagents.error.cancelled")
                store.finish_session(agent, chat_id, session_id, cancelled_msg, False)
                _emit_subagent_event(agent, "sub_agent_end", {
                    "sessionId": session_id,
                    "output": cancelled_msg,
                    "success": False,
                })
                return {
                    "success": False,
                    "cancelled": True,
                    "cancelled_by_user": True,
                    "output": cancelled_msg,
                    "subagent": record.name,
                    "sessionId": session_id,
                    "_guiSessionMarker": f"{GUI_SUBAGENT_SESSION_BEGIN}{session_id}{GUI_SUBAGENT_SESSION_END}",
                    "_elapsed_seconds": round(time.monotonic() - _started_at, 1),
                }
            call_ctx = AICallContext(
                user_input="",
                messages_override=list(messages),
                record_history_override=False,
                return_message=True,
                stream=True,
                tool_schemas=tool_schemas or None,
                tool_choice="auto" if tool_schemas else None,
                # Re-attach the image on every round: ``prepare_image_input``
                # injects it into the request payload only, never into the
                # stored ``messages``, so it must be supplied each call to stay
                # visible to the sub-agent's multimodal model.
                image_path=image_path,
            )
            # In stream mode ``orchestrator.call`` returns a generator-like
            # result object. Errors may still be returned as a plain string.
            _thinking_started_at_sub = time.monotonic()
            stream_result = orchestrator.call(call_ctx=call_ctx)
            _thinking_ended_at_sub: Optional[float] = None
            if isinstance(stream_result, str):
                message = stream_result
            else:
                message = None
                _streamed_text: List[str] = []
                # Sanitize the streamed text on the fly so hidden markers
                # (e.g. <|channel>thought ... <channel|>) never reach the GUI.
                # Mirrors the main session's streaming sanitizer; the cleaned
                # text is also accumulated so the fallback message stays
                # marker-free.
                _sanitizer = _StreamingSanitizer()
                _emitted_thinking_len = 0
                try:
                    for _delta in stream_result:
                        if isinstance(_delta, str) and _delta:
                            _clean_delta = _sanitizer.feed(_delta)
                            if _clean_delta:
                                _streamed_text.append(_clean_delta)
                                _emit_subagent_event(agent, "sub_agent_assistant", {
                                    "sessionId": session_id,
                                    "text": _clean_delta,
                                })
                        # Surface accumulated reasoning (all providers: reasoning
                        # deltas, <|channel>thought markers, ollama thinking) so
                        # the GUI renders a live, collapsible thinking block like
                        # the main session. ``thinking_text`` is updated live by
                        # the stream generator; emit only the newly-appended tail.
                        _full_thinking = str(getattr(stream_result, "thinking_text", "") or "")
                        if _full_thinking and len(_full_thinking) > _emitted_thinking_len:
                            _thinking_delta = _full_thinking[_emitted_thinking_len:]
                            _emitted_thinking_len = len(_full_thinking)
                            if _thinking_delta:
                                logger.debug("run_subagent: emitting sub_agent_thinking (len=%s, total=%s)", len(_thinking_delta), _emitted_thinking_len)
                                _emit_subagent_event(agent, "sub_agent_thinking", {
                                    "sessionId": session_id,
                                    "text": _thinking_delta,
                                })
                except Exception as _stream_exc:
                    logger.warning("run_subagent: stream iteration error: %s", _stream_exc)
                # Flush any trailing visible text (drops incomplete marker
                # suffixes that can never complete at end-of-stream).
                _tail = _sanitizer.flush()
                if _tail:
                    _streamed_text.append(_tail)
                    _emit_subagent_event(agent, "sub_agent_assistant", {
                        "sessionId": session_id,
                        "text": _tail,
                    })
                # Flush any final reasoning accumulated after the loop (e.g.
                # reasoning delivered only in the final snapshot/usage payload).
                _full_thinking = str(getattr(stream_result, "thinking_text", "") or "")
                if _full_thinking and len(_full_thinking) > _emitted_thinking_len:
                    _thinking_tail = _full_thinking[_emitted_thinking_len:]
                    _emitted_thinking_len = len(_full_thinking)
                    if _thinking_tail:
                        _emit_subagent_event(agent, "sub_agent_thinking", {
                            "sessionId": session_id,
                            "text": _thinking_tail,
                        })
                _thinking_ended_at_sub = time.monotonic()
                _thinking_end_elapsed = round(_thinking_ended_at_sub - _thinking_started_at_sub, 1)
                if _thinking_end_elapsed > 0:
                    _emit_subagent_event(agent, "sub_agent_thinking_end", {
                        "sessionId": session_id,
                        "thinkingElapsedSeconds": _thinking_end_elapsed,
                    })
                message = getattr(stream_result, "final_message", None)
                if not isinstance(message, dict):
                    message = {"role": "assistant", "content": "".join(_streamed_text)}

            if _subagent_cancelled(agent, cancel_check):
                cancelled_msg = _t(agent, "subagents.error.cancelled")
                store.finish_session(agent, chat_id, session_id, cancelled_msg, False)
                _emit_subagent_event(agent, "sub_agent_end", {
                    "sessionId": session_id,
                    "output": cancelled_msg,
                    "success": False,
                })
                return {
                    "success": False,
                    "cancelled": True,
                    "cancelled_by_user": True,
                    "output": cancelled_msg,
                    "subagent": record.name,
                    "sessionId": session_id,
                    "_guiSessionMarker": f"{GUI_SUBAGENT_SESSION_BEGIN}{session_id}{GUI_SUBAGENT_SESSION_END}",
                    "_elapsed_seconds": round(time.monotonic() - _started_at, 1),
                }

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
                    "_elapsed_seconds": round(time.monotonic() - _started_at, 1),
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
                    "_elapsed_seconds": round(time.monotonic() - _started_at, 1),
                }

            raw_content = str(message.get("content") or "")
            # Mirror the main session: deduplicate tool_calls that are identical
            # apart from their id (models sometimes emit the same call repeatedly).
            # Done BEFORE plans/assistant_msg/tool_msg are derived so the deduped
            # set stays consistent across execution, persisted history, and the
            # follow-up request (an unmatched tool result would otherwise break
            # the Responses API, which rejects role:"tool" input items).
            _raw_tcs = message.get("tool_calls")
            if isinstance(_raw_tcs, list) and len(_raw_tcs) > 1:
                _seen_tc: Set[str] = set()
                _deduped_tc: List[Dict[str, Any]] = []
                for _tc in _raw_tcs:
                    if not isinstance(_tc, dict):
                        _deduped_tc.append(_tc)
                        continue
                    _fn = _tc.get("function", {})
                    _key = json.dumps(
                        {"name": _fn.get("name"), "arguments": _fn.get("arguments")},
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                    if _key not in _seen_tc:
                        _seen_tc.add(_key)
                        _deduped_tc.append(_tc)
                if len(_deduped_tc) != len(_raw_tcs):
                    message["tool_calls"] = _deduped_tc
            # Mirror the main session: sanitize the text here so the sub-agent
            # never leaks hidden markers into its answer or stored history.
            clean_content = _sanitize_assistant_text(raw_content).strip()
            content_text = clean_content if clean_content else raw_content
            if content_text:
                last_assistant_text = content_text

            # Persist the model's reasoning so the session viewer can render a
            # thinking block on reload, mirroring the main chat's ``_thinking``.
            # Prefer the stream generator's live thinking (all providers), then
            # the final message's ``_thinking`` field.
            thinking_text = str(getattr(stream_result, "thinking_text", "") or "").strip()
            if not thinking_text:
                thinking_text = str(message.get("_thinking") or "").strip()

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
                    "_elapsed_seconds": round(time.monotonic() - _started_at, 1),
                }

            # Record the assistant turn (with its tool_calls) so the follow-up
            # tool messages are valid in the next request.
            # Persist the same big role:assistant block (_reply_records) the
            # main chat uses: natural-order nodes + a trailing raw node when
            # content cleaning happened. The in-memory follow-up request keeps a
            # flat provider-shaped message (cleaned content + tool_calls).
            _reply_events = message.pop("_reply_events", None)
            reply_records = _build_reply_records(message, _reply_events, message.get("tool_calls"))
            persisted_msg: Dict[str, Any] = {"role": "assistant", "content": "", "_reply_records": reply_records}
            _thinking_sent_elapsed: Optional[float] = None
            if thinking_text and _thinking_ended_at_sub is not None:
                _thinking_elapsed = _thinking_ended_at_sub - _thinking_started_at_sub
                if _thinking_elapsed > 0:
                    _thinking_elapsed_rounded = round(_thinking_elapsed, 1)
                    persisted_msg["_thinking_elapsed_seconds"] = _thinking_elapsed_rounded
                    _thinking_sent_elapsed = _thinking_elapsed_rounded
            store.append_message(agent, chat_id, session_id, persisted_msg)
            # Follow-up request message: flat provider shape (cleaned text so the
            # sub-agent never re-feeds hidden markers to itself).
            followup_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": clean_content if clean_content else raw_content,
            }
            tcs = message.get("tool_calls")
            if isinstance(tcs, list) and tcs:
                followup_msg["tool_calls"] = tcs
            if thinking_text:
                followup_msg["_thinking"] = thinking_text
            messages.append(followup_msg)

            # Collect each tool call + result as a structured ``_tool_rounds_raw``
            # entry (identical shape to the main session) so the GUI/TUI can
            # re-render the descriptions in any language and expand tool outputs
            # on reload — exactly like the main chat's tool-round history.
            round_raw: List[Dict[str, Any]] = []
            for idx, (tool_name, args) in enumerate(plans):
                if _subagent_cancelled(agent, cancel_check):
                    cancelled_msg = _t(agent, "subagents.error.cancelled")
                    store.finish_session(agent, chat_id, session_id, cancelled_msg, False)
                    _emit_subagent_event(agent, "sub_agent_end", {
                        "sessionId": session_id,
                        "output": cancelled_msg,
                        "success": False,
                    })
                    return {
                        "success": False,
                        "cancelled": True,
                        "cancelled_by_user": True,
                        "output": cancelled_msg,
                        "subagent": record.name,
                        "sessionId": session_id,
                        "_guiSessionMarker": f"{GUI_SUBAGENT_SESSION_BEGIN}{session_id}{GUI_SUBAGENT_SESSION_END}",
                        "_elapsed_seconds": round(time.monotonic() - _started_at, 1),
                    }
                call_id = _extract_tool_call_id(message, idx)
                t = str(tool_name).strip().lower()

                # Emit tool call event; surface the thinking elapsed time on the
                # first tool call so the GUI can backfill "Thought for Xs" live.
                _tc_data: Dict[str, Any] = {
                    "sessionId": session_id,
                    "toolName": str(tool_name),
                    "args": args if isinstance(args, dict) else {},
                }
                if _thinking_sent_elapsed is not None and idx == 0:
                    _tc_data["thinkingElapsedSeconds"] = _thinking_sent_elapsed
                    _thinking_sent_elapsed = None  # only send once
                _emit_subagent_event(agent, "sub_agent_tool_call", _tc_data)
                logger.debug("[sa-tc] emitted tool_call session=%s tool=%s idx=%s", session_id, str(tool_name), idx)

                if t in _EXCLUDED_SUBAGENT_TOOLS:
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

                # TUI: mirror the main session's live tool-feedback line so a
                # terminal user sees the same "Ran <tool> ..." descriptions.
                if sys.stdout.isatty():
                    try:
                        _feedback = agent._format_tool_call_feedback_line(
                            str(tool_name),
                            args if isinstance(args, dict) else {},
                            failed=not bool((tool_result or {}).get("success", True)),
                        )
                        if _feedback:
                            print(_feedback, flush=True)
                    except Exception:
                        pass

                # Structured raw entry — same keys/shape as the main session's
                # ``_tool_rounds_raw`` so the GUI and TUI renderers are shared.
                r = tool_result if isinstance(tool_result, dict) else {}
                _round_output = ""
                _extract = getattr(agent, "_extract_tool_result_output", None)
                if callable(_extract):
                    try:
                        _out_src = r
                        # MCP tool results wrap the underlying tool output in
                        # ``r["result"]``; extract its raw content.
                        if t.startswith("mcp__") and isinstance(r.get("result"), dict):
                            _out_src = r["result"]
                        _round_output = _extract(str(tool_name), _out_src) or str(r.get("message") or "")
                    except Exception:
                        _round_output = ""
                # When the extractor produced nothing, fall back to the common
                # output carriers, mirroring ``_render_subagent_tool_round`` so
                # persisted data matches the live SSE rendering.
                if not _round_output:
                    _round_output = str(r.get("output") or r.get("content") or "")
                if not _round_output and not bool(r.get("success", True)):
                    _round_output = str(r.get("error") or r.get("message") or "")
                raw_entry = {
                    "tool": str(tool_name),
                    "args": dict(args) if isinstance(args, dict) else {},
                    "failed": not bool(r.get("success", True)),
                    "elapsed": r.get("_elapsed_seconds"),
                    "output": _round_output or "",
                }
                # Persist shell diff preview entries so sub-agent session
                # replay can render inline diff blocks (same path as the
                # main agent in _record_model_tool_execution_history).
                _shell_entries = r.get("_shell_diff_entries") if str(tool_name) == "shell" else None
                if isinstance(_shell_entries, list) and _shell_entries:
                    import secrets as _secrets
                    _refs: list = []
                    for _entry in _shell_entries:
                        _ref = _secrets.token_hex(8)
                        _refs.append(_ref)
                        agent._persist_apply_patch_preview_sidecar(
                            {"file_path": _entry.get("file") or ""},
                            {
                                "change_preview_rows": _entry.get("diffRows") or [],
                                "file": _entry.get("file") or "",
                            },
                            _ref,
                        )
                    raw_entry["previewRef"] = "|".join(_refs)
                _err_text = str(r.get("error") or "")
                if _err_text:
                    raw_entry["error"] = _err_text
                _marker = str(r.get("_guiSessionMarker") or "")
                if _marker:
                    raw_entry["marker"] = _marker
                round_raw.append(raw_entry)
                logger.debug(
                    "[sa-output] raw_entry session=%s tool=%s failed=%s outputLen=%s outputPreview=%s",
                    session_id,
                    str(tool_name),
                    raw_entry["failed"],
                    len(_round_output or ""),
                    (_round_output or "")[:200] if _round_output else "<empty>",
                )

                # Pre-render the tool round via the SAME pipeline the main
                # session's GUI renderer uses (agent._rerender_tool_rounds), so
                # live and reloaded views match. Fall back to the legacy
                # renderer if that call is unavailable.
                tool_round = _render_subagent_tool_round(agent, str(tool_name), args, tool_result)
                try:
                    _rendered = agent._rerender_tool_rounds([raw_entry])
                    if _rendered:
                        tool_round = _rendered[0]
                except Exception:
                    pass
                _emit_subagent_event(agent, "sub_agent_output", {
                    "sessionId": session_id,
                    "text": result_text[:5000],  # Truncate very long outputs for SSE
                    "toolName": str(tool_name),
                    "toolRound": tool_round,
                })
                logger.debug(
                    "[sa-output] emitted session=%s tool=%s toolRoundLen=%s textLen=%s",
                    session_id,
                    str(tool_name),
                    len(tool_round or ""),
                    len(result_text or ""),
                )

            # Persist only the structured ``_tool_rounds_raw``. The GUI derives
            # both the tool description and the expandable tool output from this
            # on load (the subagent-session-history endpoint re-renders it through
            # the same ``agent._rerender_tool_rounds`` pipeline the main chat
            # uses), so no pre-rendered ``tool_rounds`` blob is stored.
            if round_raw:
                store.set_assistant_tool_rounds_raw(session_id, round_raw)

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
            "_elapsed_seconds": round(time.monotonic() - _started_at, 1),
        }
    except Exception as _loop_exc:
        # An unexpected error inside the sub-agent loop must not leave the
        # session half-persisted (only the seeded system/user messages and
        # endedAt=null). Finish it with an error so the GUI viewer can still
        # open the session and see whatever progress was recorded, and log the
        # traceback so the failure is diagnosable.
        logger.exception("run_subagent: loop error for session %s: %s", session_id, _loop_exc)
        err_out = f"[subagent error] {_loop_exc}"
        try:
            store.finish_session(agent, chat_id, session_id, err_out, False)
        except Exception:
            pass
        _emit_subagent_event(agent, "sub_agent_end", {
            "sessionId": session_id,
            "output": err_out,
            "success": False,
        })
        return {
            "success": False,
            "error": err_out,
            "subagent": record.name,
            "sessionId": session_id,
            "_guiSessionMarker": f"{GUI_SUBAGENT_SESSION_BEGIN}{session_id}{GUI_SUBAGENT_SESSION_END}",
            "_elapsed_seconds": round(time.monotonic() - _started_at, 1),
        }
    finally:
        agent._subagent_depth = max(0, int(getattr(agent, "_subagent_depth", 1) or 1) - 1)
