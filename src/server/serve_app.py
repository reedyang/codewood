"""Headless HTTP + SSE server that drives the existing Agent.

Design goals:
- Reuse 100% of the existing command/AI logic by running the normal
  ``run_agent_loop`` in a worker thread and feeding it input lines the
  same way a terminal user would type them (prompts and ``/slash``
  commands alike).
- Stream all terminal output back to GUI clients over SSE after
  stripping ANSI escape codes.
- Route interactive confirmation/elicitation prompts to the GUI via a
  ``confirm`` SSE event answered through ``POST /confirm``.

Security: binds to loopback only, rejects non-loopback peers, and
requires a per-launch bearer token on every request. The token is
emitted once on the real stdout as a single JSON handshake line so the
launching host can read it; it is never written to logs.
"""

from __future__ import annotations

import io
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from ..core.console_utils import (
    GUI_FORCE_PROMPT_PREFIX,
    GUI_INTERNAL_COMMAND_PREFIX,
)

# Matches CSI / SGR and most other ANSI escape sequences.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")

# Upper bounds to reject oversized/abusive payloads (input guarding).
_MAX_INPUT_CHARS = 200_000
_MAX_CONFIRM_ANSWER_CHARS = 64
_MAX_BODY_BYTES = 1_048_576  # 1 MiB
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences so the GUI can theme output itself."""
    if not text:
        return ""
    return _ANSI_RE.sub("", text)


def strip_ansi_keep_sgr(text: str) -> str:
    """Drop cursor/erase escapes but preserve SGR color codes (``\\x1b[..m``).

    The GUI renders the surviving SGR runs into colored spans so step output
    (e.g. the green/red success-or-failure bullet) matches the terminal.
    """
    if not text:
        return ""

    def _repl(m: "re.Match[str]") -> str:
        seq = m.group(0)
        return seq if (seq.startswith("\x1b[") and seq.endswith("m")) else ""

    return _ANSI_RE.sub(_repl, text)


def _open_in_file_manager(path: str) -> bool:
    """Open a validated directory in the OS file manager (no shell).

    ``path`` must already resolve to an existing directory; the caller is
    responsible for mapping a trusted id to this root.
    """
    try:
        target = os.path.realpath(str(path))
    except Exception:
        return False
    if not target or not os.path.isdir(target):
        return False
    try:
        if sys.platform.startswith("win"):
            os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target], close_fds=True)
        else:
            subprocess.Popen(["xdg-open", target], close_fds=True)
        return True
    except Exception:
        return False


def _read_workspace_chat_index(storage_dir: Any) -> List[Dict[str, Any]]:
    """Read chat summaries from a workspace's on-disk chat index.

    Returns ``[{id, name, updatedAt}]`` (possibly empty). The path is derived
    from trusted agent state, not from any client input.
    """
    from ..agent import CHAT_STATE_FILE

    out: List[Dict[str, Any]] = []
    try:
        index_path = os.path.join(str(storage_dir), "chats", CHAT_STATE_FILE)
        if not os.path.isfile(index_path):
            return out
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return out
    chats = data.get("chats") if isinstance(data, dict) else None
    if not isinstance(chats, list):
        return out
    for c in chats:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "")
        if not cid:
            continue
        out.append(
            {
                "id": cid,
                "name": str(c.get("name") or ""),
                "updatedAt": str(c.get("updated_at") or ""),
            }
        )
    return out


def _build_structured_turns(agent: Any) -> List[Dict[str, Any]]:
    """Group the active chat's history into GUI turns of ordered model rounds.

    Each turn is ``{"userText", "timestamp", "rounds"}`` where every round is
    one model request/response: ``{"waitSeconds", "text", "tools"}``. ``text``
    is the model's reply for that round (markdown); ``tools`` is that round's
    tool call(s) + result(s) rendered in natural order; ``waitSeconds`` is how
    long the model took to answer that round (derived from message timestamps).
    Internal command inputs (slash / direct shell) and their outputs are
    filtered out.
    """
    import contextlib
    from datetime import datetime

    from ..controllers.chat_command_controller import (
        _genuine_user_positions_in_list,
    )
    from ..core.assistant_output_highlighter import (
        format_assistant_display_response,
    )

    hist = list(getattr(agent, "conversation_history", None) or [])
    genuine = set(_genuine_user_positions_in_list(hist))
    turns: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    current_round: Optional[Dict[str, Any]] = None
    prev_ts: Optional[float] = None
    sms = getattr(agent, "session_memory_service", None)

    def _parse_ts(value: Any) -> Optional[float]:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            return None

    def _ensure_turn() -> Dict[str, Any]:
        nonlocal current
        if current is None:
            current = {"userText": "", "timestamp": "", "rounds": []}
            turns.append(current)
        return current

    def _new_round(turn: Dict[str, Any], wait_seconds: float) -> Dict[str, Any]:
        rnd = {"waitSeconds": max(0, int(round(wait_seconds))), "text": "", "tools": ""}
        turn["rounds"].append(rnd)
        return rnd

    def _render_step(idx: int, msg: Dict[str, Any]) -> str:
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                agent._render_transcript_single_message(idx, msg, hist)
        except Exception:
            pass
        return (
            strip_ansi_keep_sgr(buffer.getvalue())
            .replace("\r\n", "\n")
            .replace("\r", "")
            .strip("\n")
        )

    def _is_answer(content: str) -> bool:
        """A plain final reply, not a bookkeeping/tool/compaction payload."""
        try:
            if sms is not None:
                if sms.parse_context_compaction_notice_content(content) is not None:
                    return False
                if sms.parse_context_compaction_summary_content(content) is not None:
                    return False
            if agent._parse_conversation_interrupted_history_content(content) is not None:
                return False
            if agent._parse_direct_shell_result_history_content(content) is not None:
                return False
            if agent._parse_task_worked_summary_history_content(content) is not None:
                return False
            if agent._parse_model_tool_plan_history_content(content) is not None:
                return False
            if agent._parse_model_tool_result_history_content(content) is not None:
                return False
        except Exception:
            return False
        try:
            return bool(format_assistant_display_response(content))
        except Exception:
            return False

    def _is_tool_result(content: str) -> bool:
        try:
            return agent._parse_model_tool_result_history_content(content) is not None
        except Exception:
            return False

    def _looks_like_tool_calls_blob(content: str) -> bool:
        """A raw ``tool_calls`` JSON payload the strict parser may not match.

        Some models emit assistant content that is literally a tool-call JSON
        object/array (occasionally malformed or multi-call). It must never be
        shown as natural-language text — treat it as a (skipped) tool step.
        """
        text = str(content or "").strip()
        if not (text.startswith("{") or text.startswith("[")):
            return False
        try:
            payload = json.loads(text)
        except Exception:
            # Unparseable but clearly a tool-call shape (e.g. truncated stream).
            return '"tool_calls"' in text or '"function"' in text
        if isinstance(payload, dict):
            return "tool_calls" in payload or "tool" in payload or "function" in payload
        if isinstance(payload, list) and payload:
            first = payload[0]
            return isinstance(first, dict) and (
                "tool" in first or "name" in first or "function" in first
            )
        return False

    def _is_tool_plan(content: str) -> bool:
        """A pure tool-call model message (no natural-language reply)."""
        try:
            if agent._parse_model_tool_plan_history_content(content) is not None:
                return True
        except Exception:
            pass
        return _looks_like_tool_calls_blob(content)

    for idx, msg in enumerate(hist):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip().lower()
        content = str(msg.get("content") or "")
        ts = _parse_ts(msg.get("created_at"))
        if idx in genuine:
            current = {
                "userText": content,
                "timestamp": str(msg.get("created_at") or ""),
                "rounds": [],
            }
            turns.append(current)
            current_round = None
            prev_ts = ts
            continue
        if role == "user":
            # Non-genuine user entries are internal command inputs (slash
            # commands or "!cmd" direct shell). The GUI never executes these
            # directly, so neither the command echo nor its output is shown.
            continue
        if role == "assistant":
            # A recorded ask_more_info selection: render it as a left-side
            # "selection" bubble (a reply to the agent's question, distinct
            # from a user-initiated right-side turn).
            try:
                ami_answer = agent._parse_ask_more_info_answer_history_content(content)
            except Exception:
                ami_answer = None
            if ami_answer is not None:
                if ami_answer:
                    turn = _ensure_turn()
                    wait = (
                        (ts - prev_ts)
                        if (ts is not None and prev_ts is not None)
                        else 0
                    )
                    sel_round = _new_round(turn, wait)
                    sel_round["selection"] = strip_ansi(str(ami_answer))
                if ts is not None:
                    prev_ts = ts
                continue
            # Drop slash-command outputs and durable compaction summaries.
            try:
                if agent._parse_internal_slash_result_history_content(content) is not None:
                    continue
            except Exception:
                pass
            # Drop output of user-initiated direct shell ("!cmd") executions; the
            # GUI does not surface direct commands or their results.
            try:
                if agent._parse_direct_shell_result_history_content(content) is not None:
                    continue
            except Exception:
                pass
            try:
                if sms is not None and sms.parse_context_compaction_summary_content(content) is not None:
                    continue
            except Exception:
                pass
            # The per-turn worked summary is superseded by per-round timers.
            try:
                if agent._parse_task_worked_summary_history_content(content) is not None:
                    continue
            except Exception:
                pass

        # A tool result belongs to the round whose model message requested it,
        # accumulating its wait into that round's total time.
        if _is_tool_result(content):
            turn = _ensure_turn()
            if current_round is None:
                current_round = _new_round(turn, 0)
            if ts is not None and prev_ts is not None:
                current_round["waitSeconds"] += max(0, int(round(ts - prev_ts)))
            rendered = _render_step(idx, msg)
            if rendered.strip():
                current_round["tools"] = current_round["tools"] + rendered + "\n"
            if ts is not None:
                prev_ts = ts
            continue

        # A pure tool-call model message (no natural-language reply) belongs to
        # the same collapsible tool group: merge it into the current tool round
        # (creating one only if none is open) so its wait adds to the group's
        # total time instead of spawning a separate timer.
        if _is_tool_plan(content):
            turn = _ensure_turn()
            wait = (ts - prev_ts) if (ts is not None and prev_ts is not None) else 0
            if current_round is None or current_round.get("text"):
                # Start a fresh tool group either at the turn's first activity or
                # right after a round that already carried a model reply.
                current_round = _new_round(turn, wait)
            else:
                current_round["waitSeconds"] += max(0, int(round(wait)))
            # Tool-call plans are bookkeeping: the matching tool-result message
            # renders the "Ran <tool>" feedback line. Render the plan only when
            # the per-message renderer recognizes it (so it stays silent); for a
            # blob the strict parser misses, skip rendering entirely rather than
            # letting the raw JSON leak as text.
            recognized_plan = False
            try:
                recognized_plan = (
                    agent._parse_model_tool_plan_history_content(content) is not None
                )
            except Exception:
                recognized_plan = False
            if recognized_plan:
                rendered = _render_step(idx, msg)
                if rendered.strip():
                    current_round["tools"] = current_round["tools"] + rendered + "\n"
            if ts is not None:
                prev_ts = ts
            continue

        # Otherwise resolve the model's natural-language reply (if any). Only a
        # message that actually carries answer text opens a new round; anything
        # else (empty/blank assistant turns, bookkeeping that slipped through)
        # merges into the current tool group so it can't split one collapsible
        # group into two with separate timers.
        turn = _ensure_turn()
        wait = (ts - prev_ts) if (ts is not None and prev_ts is not None) else 0
        answer_text = ""
        if _is_answer(content):
            try:
                answer_text = format_assistant_display_response(content) or ""
            except Exception:
                answer_text = ""
            answer_text = (
                strip_ansi(str(answer_text)).replace("\r\n", "\n").replace("\r", "\n").strip("\n")
            )
        if answer_text.strip():
            # Some models re-emit an identical final reply (e.g. an empty
            # tool-call round followed by a repeat of the same answer). Collapse
            # a verbatim repeat of the previous round's text instead of showing
            # the same answer twice with its own timer.
            rounds_so_far = turn.get("rounds", [])
            prev_round = rounds_so_far[-1] if rounds_so_far else None
            if (
                prev_round is not None
                and str(prev_round.get("text") or "").strip() == answer_text.strip()
            ):
                if ts is not None:
                    prev_ts = ts
                continue
            current_round = _new_round(turn, wait)
            current_round["text"] = answer_text
        else:
            rendered = _render_step(idx, msg)
            if current_round is None or current_round.get("text"):
                current_round = _new_round(turn, wait)
            else:
                current_round["waitSeconds"] += max(0, int(round(wait)))
            if rendered.strip():
                current_round["tools"] = current_round["tools"] + rendered + "\n"
        if ts is not None:
            prev_ts = ts

    for turn in turns:
        rounds = [
            {
                "waitSeconds": int(r.get("waitSeconds") or 0),
                "text": str(r.get("text") or "").rstrip("\n"),
                "tools": str(r.get("tools") or "").rstrip("\n"),
                "selection": str(r.get("selection") or "").strip(),
            }
            for r in turn.get("rounds", [])
        ]
        # Drop rounds that produced nothing renderable (e.g. an empty model
        # response) so we don't show a stray timer with no content. A round
        # carrying an ask_more_info selection is always renderable.
        turn["rounds"] = [
            r
            for r in rounds
            if r["text"].strip() or r["tools"].strip() or r["selection"].strip()
        ]
    return turns


class _Broadcaster:
    """Fan-out of server events to all connected SSE subscribers."""

    def __init__(self) -> None:
        self._subscribers: List["queue.Queue[Dict[str, Any]]"] = []
        self._lock = threading.Lock()

    def subscribe(self) -> "queue.Queue[Dict[str, Any]]":
        q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[Dict[str, Any]]") -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def publish(self, event: str, data: Optional[Dict[str, Any]] = None) -> None:
        message = {"event": str(event), "data": data or {}}
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(message)
            except Exception:
                pass


class _OutputBridge(io.TextIOBase):
    """A text stream that forwards all writes to SSE subscribers.

    Installed as ``sys.stdout`` / ``sys.stderr`` for the serve process
    after the stdout handshake, so every ``print`` in the agent loop
    becomes an ``output`` event instead of hitting the console.
    """

    def __init__(self, broadcaster: _Broadcaster, chat_id_getter: Any = None) -> None:
        self._broadcaster = broadcaster
        # Returns the chat id that output is currently attributed to, so the
        # GUI can route streamed text to the correct chat when several chats
        # run concurrently. Falls back to "" when unknown.
        self._chat_id_getter = chat_id_getter
        # The current output tag ("output" steps vs "assistant" reply) and the
        # suppression flag are per-thread: each concurrent chat loop runs on its
        # own thread and must not flip the other's tag or silence the other's
        # output. Defaults: tag="output", suppressed=False.
        self._tls = threading.local()

    def _chat_id(self) -> str:
        getter = self._chat_id_getter
        if not callable(getter):
            return ""
        try:
            return str(getter() or "")
        except Exception:
            return ""

    @property
    def suppressed(self) -> bool:
        return bool(getattr(self._tls, "suppressed", False))

    @suppressed.setter
    def suppressed(self, value: bool) -> None:
        self._tls.suppressed = bool(value)

    def set_tag(self, tag: str) -> None:
        self._tls.tag = str(tag or "output")

    def write(self, s: Any) -> int:  # type: ignore[override]
        if s is None:
            return 0
        text = s if isinstance(s, str) else str(s)
        if not text:
            return 0
        if bool(getattr(self._tls, "suppressed", False)):
            # Consume silently so the command still runs but nothing streams.
            return len(text)
        # Keep SGR color runs (so the GUI can theme step output like the
        # terminal) but drop cursor/erase control sequences a non-TTY SSE
        # sink cannot honor. Normalize carriage returns to plain newlines.
        cleaned = strip_ansi_keep_sgr(text).replace("\r\n", "\n").replace("\r", "")
        if cleaned:
            tag = str(getattr(self._tls, "tag", "output") or "output")
            self._broadcaster.publish(tag, {"text": cleaned, "chatId": self._chat_id()})
        return len(text)

    def writable(self) -> bool:  # type: ignore[override]
        return True

    def flush(self) -> None:  # type: ignore[override]
        return None

    def isatty(self) -> bool:  # type: ignore[override]
        return False


def _primary_active_chat_id(agent: Any) -> str:
    """The workspace's primary/focused chat id from the shared chat index.

    ``agent.active_chat_id`` is now per-session (thread-bound), so reading it on
    an HTTP handler thread would yield that thread's (usually empty) session.
    The shared ``_chat_state["active"]`` is the stable, cross-thread truth.
    """
    try:
        cs = getattr(agent, "_chat_state", None)
        if isinstance(cs, dict):
            return str(cs.get("active") or "")
    except Exception:
        pass
    return ""


def _safe_pending_ask_more_info(agent: Any) -> Optional[Dict[str, Any]]:
    """Return the active chat's pending ``ask_more_info`` request, if any.

    Reads from the chat-record marker so a GUI that loads (or refreshes)
    a chat already mid-prompt re-renders the selection panel — including
    the case where a *different* process (TUI) triggered the prompt and
    the local backend never saw the in-memory request id. The panel
    submit handler will POST the marker's id; if the local backend can't
    match it the call is a no-op until cross-process answer routing
    lands.

    Callers that surface this state across processes (``select_chat``,
    ``chat_history``, ``_build_state`` after a cross-process amend) are
    expected to refresh the chat record from disk *before* invoking this
    helper — the refresh is intentionally NOT done here to avoid
    clobbering an in-progress local stream (the GUI's own runtime mutates
    ``conversation_history`` without re-persisting on every chunk).
    """
    try:
        cid = _primary_active_chat_id(agent)
        with agent._session_scope(cid):
            payload = agent._peek_pending_ask_more_info()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        return None
    raw_options = payload.get("options")
    options: List[str] = []
    if isinstance(raw_options, list):
        for raw in raw_options:
            label = strip_ansi(str(raw or ""))[:120]
            if label:
                options.append(label)
    return {
        "id": str(payload.get("id") or ""),
        "question": strip_ansi(str(payload.get("question") or "")),
        "options": options,
        "multiSelect": bool(payload.get("multi_select", False)),
        "chatId": str(_primary_active_chat_id(agent) or ""),
    }


def _safe_active_plan(agent: Any) -> Dict[str, Any]:
    """Return the active chat's plan ({plan:[{step,status}], explanation}).

    ``_active_chat_plan`` is session-scoped, but this runs on HTTP handler
    threads that may not be bound to the active chat's session (and a
    focus-only chat switch never rebinds/refreshes it). Bind to the active
    chat and refresh the in-memory plan from its message stream so the panel
    always reflects the latest plan-bearing message of the chat being shown.
    """
    snapshot = None
    try:
        cid = _primary_active_chat_id(agent)
        with agent._session_scope(cid):
            try:
                agent._chat_state_manager.refresh_active_chat_plan_from_messages()
            except Exception:
                pass
            snapshot = agent._chat_state_manager.active_chat_plan()
    except Exception:
        snapshot = None
    if not isinstance(snapshot, dict):
        return {"plan": [], "explanation": ""}
    steps = []
    for item in snapshot.get("plan") or []:
        if not isinstance(item, dict):
            continue
        steps.append(
            {
                "step": str(item.get("step") or ""),
                "status": str(item.get("status") or ""),
            }
        )
    return {"plan": steps, "explanation": str(snapshot.get("explanation") or "")}


def _safe_reasoning_level(agent: Any) -> str:
    # Reasoning level is session-scoped; bind to the active chat so HTTP
    # handler threads read the focused chat's saved selection (restored from
    # its chat record on activation), not the ambient/unbound session.
    try:
        with agent._session_scope(_primary_active_chat_id(agent)):
            return str(agent._current_reasoning_level() or "")
    except Exception:
        return ""


def _safe_reasoning_levels(agent: Any) -> List[str]:
    try:
        with agent._session_scope(_primary_active_chat_id(agent)):
            return [str(x) for x in (agent._current_model_reasoning_levels() or []) if str(x)]
    except Exception:
        return []


def _build_state(agent: Any) -> Dict[str, Any]:
    """Serialize a read-only snapshot of agent state for the GUI."""
    from ..config.app_info import get_app_name, get_app_version
    from ..core.localization import get_display_language

    default_ws_id = ""
    try:
        default_ws_id = str(
            getattr(getattr(agent, "_workspace_state_manager", None), "_default_workspace_id", "")
            or ""
        )
    except Exception:
        default_ws_id = ""

    workspaces: List[Dict[str, Any]] = []
    try:
        raw = agent._workspaces_state.get("workspaces", {})
        active_ws_id = str(getattr(agent, "workspace_id", "") or "")
        if isinstance(raw, dict):
            for entry in raw.values():
                if not isinstance(entry, dict):
                    continue
                try:
                    root = str(agent._workspace_root_path(entry))
                except Exception:
                    root = str(entry.get("root") or "")
                ws_id = str(entry.get("id") or "")
                is_default = (
                    str(entry.get("kind") or "").lower() == "default"
                    or (bool(default_ws_id) and ws_id == default_ws_id)
                )
                workspaces.append(
                    {
                        "id": ws_id,
                        "name": str(entry.get("name") or ""),
                        "root": root,
                        "active": ws_id == active_ws_id,
                        "isDefault": is_default,
                    }
                )
    except Exception:
        pass

    chats: List[Dict[str, Any]] = []
    active_chat_model = ""
    active_context_percent = 0
    active_context_tokens = 0
    active_context_window = 0
    # Chat ids whose agent loop is actively streaming a turn in THIS process.
    # Serialized per-chat as ``running`` so the GUI can keep the busy/blue dot
    # on every running chat even after the user switches focus away (the
    # transient turn_start/idle SSE events alone can't survive a focus change
    # or reload).
    running_chat_ids: set = set()
    try:
        runner = getattr(agent, "_active_runtime_chat_ids", None)
        if callable(runner):
            running_chat_ids = {str(x) for x in (runner() or [])}
    except Exception:
        running_chat_ids = set()
    try:
        active_chat_id = _primary_active_chat_id(agent)
        for i, c in enumerate(agent._chat_entries(), start=1):
            if not isinstance(c, dict):
                continue
            prov = str(c.get("model_provider") or "").strip()
            name = str(c.get("model_name") or "").strip()
            chat_model = f"{prov}:{name}" if prov and name else ""
            cid = str(c.get("id") or "")
            if cid == active_chat_id:
                active_chat_model = chat_model
                # Pull the focused chat's context-usage snapshot so the GUI can
                # surface it next to the model selector without a separate
                # request-per-tick. These numbers are kept in sync by the
                # session manager every time the model returns input-token
                # accounting.
                try:
                    active_context_percent = int(c.get("context_usage_percent") or 0)
                except Exception:
                    active_context_percent = 0
                try:
                    active_context_tokens = int(c.get("context_input_tokens") or 0)
                except Exception:
                    active_context_tokens = 0
                try:
                    active_context_window = int(c.get("context_window") or 0)
                except Exception:
                    active_context_window = 0
            chats.append(
                {
                    "index": i,
                    "id": cid,
                    "name": str(c.get("name") or ""),
                    "messageCount": len(c.get("messages") or []),
                    "updatedAt": str(c.get("updated_at") or ""),
                    "active": cid == active_chat_id,
                    # Per-chat model selector so the GUI can show the right model
                    # for the focused chat and prefill new chats from it.
                    "model": chat_model,
                    # True while this chat's agent loop is mid-turn, so the
                    # sidebar busy dot survives focus changes and reloads.
                    "running": cid in running_chat_ids,
                }
            )
    except Exception:
        pass

    model_available: List[str] = []
    # The displayed "current model" must follow the focused chat, not the shared
    # global agent selection (which a concurrent chat's switch can clobber).
    model_current = active_chat_model
    if not model_current:
        try:
            model_current = str(agent._current_model_selector() or "")
        except Exception:
            model_current = ""
    try:
        model_available = [str(s) for s in (agent._get_configured_model_selectors() or []) if str(s)]
    except Exception:
        pass

    try:
        agent_language = get_display_language(agent)
    except Exception:
        agent_language = "en"

    theme = ""
    ui_prefs: Dict[str, Any] = {}
    gui_language = ""
    try:
        from ..core.config.gui_config import (
            load_gui_config,
            normalize_gui_language,
            normalize_ui_prefs,
        )

        gui_cfg = load_gui_config(agent.config_dir)
        theme = str(gui_cfg.get("theme") or "")
        ui_prefs = normalize_ui_prefs(gui_cfg.get("uiPrefs"))
        gui_language = normalize_gui_language(gui_cfg.get("language"))
    except Exception:
        theme = ""
        ui_prefs = {}
        gui_language = ""

    # The GUI's display language is intentionally decoupled from the agent's
    # ``display_language`` (which drives TUI prompts and model system text).
    # We override only when the GUI has its own saved preference so existing
    # configs (no GUI language set) keep following the agent's locale.
    language = gui_language or agent_language

    return {
        "app": {"name": get_app_name(), "version": get_app_version()},
        "workspace": {
            "name": str(getattr(agent, "workspace_name", "") or ""),
            "id": str(getattr(agent, "workspace_id", "") or ""),
            "root": str(getattr(agent, "workspace_root", "") or ""),
            "workDirectory": str(getattr(agent, "work_directory", "") or ""),
        },
        "workspaces": workspaces,
        "chats": chats,
        "activeChatId": _primary_active_chat_id(agent),
        "model": {
            "current": model_current,
            "available": model_available,
            "reasoningLevel": _safe_reasoning_level(agent),
            "reasoningLevels": _safe_reasoning_levels(agent),
        },
        "contextUsage": {
            "percent": active_context_percent,
            "tokens": active_context_tokens,
            "window": active_context_window,
        },
        "language": language,
        "theme": theme,
        "uiPrefs": ui_prefs,
        "plan": _safe_active_plan(agent),
        "askMoreInfo": _safe_pending_ask_more_info(agent),
        "executionPolicy": str(getattr(agent, "execution_policy", "") or ""),
    }


class _ChatRuntime:
    """Per-chat execution context: its input queue, busy flag, loop thread.

    Each chat that receives input gets one long-lived ``run_agent_loop`` thread
    bound to that chat's :class:`SessionState`, so several chats can execute
    turns concurrently without sharing conversation/plan/usage state.
    """

    __slots__ = ("chat_id", "input_queue", "busy", "thread", "turn_started_at", "turn_record_pending")

    def __init__(self, chat_id: str) -> None:
        self.chat_id = str(chat_id or "")
        self.input_queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self.busy = threading.Event()
        self.thread: Optional[threading.Thread] = None
        # Per-turn wall-clock tracking so the elapsed "Worked for" time is
        # persisted to this chat's history and survives a reload.
        self.turn_started_at: Optional[float] = None
        self.turn_record_pending = False


class ServeApp:
    """Owns the agent loop, the event broadcaster, and the HTTP server."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.broadcaster = _Broadcaster()
        self._confirms: Dict[str, "queue.Queue[str]"] = {}
        self._confirms_lock = threading.Lock()
        # Pending ``ask_more_info`` prompts: id -> reply queue. The agent
        # blocks on a per-prompt queue until the frontend POSTs the chosen
        # option (or freeform answer) to ``/answer-ask-more-info``.
        self._ask_more_info: Dict[str, "queue.Queue[str]"] = {}
        self._ask_more_info_lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._token = secrets.token_urlsafe(32)
        self._httpd: Optional[ThreadingHTTPServer] = None
        # One runtime (input queue + busy flag + loop thread + per-turn timing)
        # per chat, so multiple chats can run their agent loop concurrently. A
        # chat's runtime is created lazily the first time input is routed to it.
        self._runtimes: Dict[str, "_ChatRuntime"] = {}
        self._runtimes_lock = threading.Lock()
        self._bridge: Optional["_OutputBridge"] = None
        # Let the chat-state saver know which chats this process owns a
        # live runtime for, so a cross-process merge-on-save never skips
        # overwriting a chat whose loop is actively streaming here.
        try:
            self.agent._active_runtime_chat_ids = self._owned_runtime_chat_ids
        except Exception:
            pass

    def _owned_runtime_chat_ids(self) -> List[str]:
        """Chat ids with an ACTIVELY RUNNING turn in THIS process.

        Only a chat whose runtime is busy (a turn is mid-stream) is an
        authoritative writer whose in-memory state must not be overwritten by
        a disk merge. An idle parked runtime does not count — its chat may
        have been amended by a peer process and should be refreshable.
        """
        out: List[str] = []
        try:
            with self._runtimes_lock:
                for cid, rt in self._runtimes.items():
                    if rt is not None and rt.busy.is_set():
                        out.append(str(cid))
        except Exception:
            return []
        return out

    def _chat_is_busy(self, chat_id: str) -> bool:
        """True iff ``chat_id`` has a runtime with a turn currently streaming."""
        cid = str(chat_id or "")
        if not cid:
            return False
        try:
            with self._runtimes_lock:
                rt = self._runtimes.get(cid)
            return bool(rt is not None and rt.busy.is_set())
        except Exception:
            return False

    def _active_chat_id(self) -> str:
        """Chat id the running turn / streamed output is attributed to.

        On the agent-loop thread this is the thread-bound session's chat; on
        HTTP handler threads (which have no bound session) it falls back to the
        shared focused chat.
        """
        try:
            cid = str(getattr(self.agent, "active_chat_id", "") or "")
        except Exception:
            cid = ""
        return cid or _primary_active_chat_id(self.agent)

    def _runtime_for_thread(self) -> Optional["_ChatRuntime"]:
        """The runtime owning the calling loop thread (bound chat)."""
        try:
            key = str(self.agent._current_session_chat_key() or "")
        except Exception:
            key = ""
        with self._runtimes_lock:
            return self._runtimes.get(key)

    # ----- agent loop hooks ------------------------------------------------
    def _record_turn_elapsed(self, rt: Optional["_ChatRuntime"]) -> None:
        """Append the just-finished turn's elapsed time to chat history.

        Called when a chat's loop returns to ask for the next input, at which
        point the previous turn's messages are already appended/persisted. The
        timing is per-chat so concurrent loops don't clobber each other.
        """
        if rt is None:
            return
        started = rt.turn_started_at
        pending = rt.turn_record_pending
        rt.turn_started_at = None
        rt.turn_record_pending = False
        if started is None or not pending:
            return
        elapsed = int(max(0, time.monotonic() - started))
        agent = self.agent
        try:
            hist = list(getattr(agent, "conversation_history", None) or [])
            if not hist:
                return
            last = hist[-1]
            if isinstance(last, dict):
                content = str(last.get("content") or "")
                # Avoid a duplicate when the runtime already recorded one
                # (e.g. on an ask_more_info pause).
                if agent._parse_task_worked_summary_history_content(content) is not None:
                    return
            rec = getattr(agent, "_record_task_worked_summary_history", None)
            if callable(rec):
                rec(elapsed)
        except Exception:
            pass

    def _input_provider(self) -> str:
        """Replacement for ``agent._get_user_input_with_history``.

        Runs on a chat's dedicated loop thread (bound to that chat's session),
        so it reads input from that chat's queue and tags all events with that
        chat's id.
        """
        rt = self._runtime_for_thread()
        self._record_turn_elapsed(rt)
        if rt is not None:
            rt.busy.clear()
        self.broadcaster.publish(
            "idle", {"state": _build_state(self.agent), "chatId": self._active_chat_id()}
        )
        if rt is None:
            # No runtime bound (should not happen); block on a private queue so
            # the loop parks instead of busy-spinning.
            return "/exit"
        text = rt.input_queue.get()
        if text is None:
            # Shutdown sentinel: ask the loop to exit cleanly.
            return "/exit"
        rt.busy.set()
        rt.turn_started_at = time.monotonic()
        # Composer input carries a force-prompt sentinel; strip it from the
        # displayed/broadcast text but keep it on the line the loop consumes.
        forced = str(text).startswith(GUI_FORCE_PROMPT_PREFIX)
        if forced:
            display = str(text)[len(GUI_FORCE_PROMPT_PREFIX):]
        elif str(text).startswith(GUI_INTERNAL_COMMAND_PREFIX):
            display = str(text)[len(GUI_INTERNAL_COMMAND_PREFIX):]
        else:
            display = str(text)
        # GUI-issued internal commands (rename, switch, ...) carry their own
        # sentinel. A bare slash line with no sentinel is treated the same way
        # defensively. Hide its echo/output and skip turn bookkeeping.
        is_internal_command = (not forced) and display.lstrip().startswith("/")
        if self._bridge is not None:
            # Per-thread: only silences this loop thread's writes.
            self._bridge.suppressed = is_internal_command
        rt.turn_record_pending = not is_internal_command
        if not is_internal_command:
            self.broadcaster.publish(
                "turn_start", {"text": display, "chatId": self._active_chat_id()}
            )
        return text

    def _confirm_provider(self, prompt: str = "") -> str:
        """Replacement for ``agent._suspended_input`` (y/n + elicitation)."""
        cid = secrets.token_hex(8)
        reply: "queue.Queue[str]" = queue.Queue()
        with self._confirms_lock:
            self._confirms[cid] = reply
        self.broadcaster.publish(
            "confirm",
            {
                "id": cid,
                "prompt": strip_ansi(str(prompt or "")),
                "chatId": self._active_chat_id(),
            },
        )
        try:
            answer = reply.get()
        finally:
            with self._confirms_lock:
                self._confirms.pop(cid, None)
        return str(answer or "")

    def _ask_more_info_provider(
        self,
        question: str,
        options: List[str],
        multi_select: bool = False,
    ) -> str:
        """Replacement for the TUI ``ask_more_info`` prompt.

        Broadcasts an ``ask_more_info`` SSE event carrying the question,
        the model-supplied options, the single/multi-select mode, and a
        per-prompt id, then blocks until the frontend POSTs the user's
        chosen answer back via ``/answer-ask-more-info``. The returned
        string is the answer the agent should treat as the user's
        supplement; it is NOT broadcast as a normal user message so the
        chat transcript stays clean.

        An empty answer means the user dismissed/cancelled the prompt —
        the runtime loop interprets that as "no selection received" and
        pauses the task, matching the TUI behaviour.
        """
        pid = secrets.token_hex(8)
        reply: "queue.Queue[str]" = queue.Queue()
        with self._ask_more_info_lock:
            self._ask_more_info[pid] = reply
        # Sanitize once at the boundary so the frontend never sees ANSI
        # escapes or untrusted control codes from the model. ``strip_ansi``
        # also collapses lone CRs; the option labels are model output but
        # bounded to 120 chars by the tool handler.
        safe_options: List[str] = []
        for raw in options or []:
            label = strip_ansi(str(raw or ""))[:120]
            if label:
                safe_options.append(label)
        safe_question = strip_ansi(str(question or ""))
        # Overwrite the chat-record marker (the runtime loop wrote a
        # short stub before invoking this provider) with our real pid so
        # any concurrent reader of the chat JSON sees the same id we'll
        # accept on /answer-ask-more-info.
        try:
            setter = getattr(self.agent, "_set_pending_ask_more_info", None)
            if callable(setter):
                setter(
                    {
                        "id": pid,
                        "question": safe_question,
                        "options": safe_options,
                        "multi_select": bool(multi_select),
                    }
                )
        except Exception:
            pass
        self.broadcaster.publish(
            "ask_more_info",
            {
                "id": pid,
                "question": safe_question,
                "options": safe_options,
                "multiSelect": bool(multi_select),
                "chatId": self._active_chat_id(),
            },
        )
        try:
            answer = reply.get()
        finally:
            with self._ask_more_info_lock:
                self._ask_more_info.pop(pid, None)
        return str(answer or "")

    # ----- API surface used by the HTTP handler ---------------------------
    @property
    def token(self) -> str:
        return self._token

    def submit_input(self, text: str, chat_id: str = "", as_prompt: bool = False) -> None:
        line = str(text or "")
        # Composer input is forced to a model prompt: prefix a sentinel the
        # runtime loop strips so "/foo" / "!bar" never run as command/shell.
        if as_prompt and line:
            line = GUI_FORCE_PROMPT_PREFIX + line
        elif line.lstrip().startswith("/"):
            # A non-prompt slash line is a command the GUI issued on the user's
            # behalf (rename/switch/pin/...). Mark it so the runtime loop runs it
            # but keeps it out of the user's input history (history.json).
            line = GUI_INTERNAL_COMMAND_PREFIX + line
        cid = str(chat_id or "").strip() or _primary_active_chat_id(self.agent)
        rt = self._get_or_spawn_runtime(cid)
        rt.input_queue.put(line)

    def _get_or_spawn_runtime(self, chat_id: str) -> "_ChatRuntime":
        """Return the chat's runtime, starting its loop thread on first use."""
        cid = str(chat_id or "")
        with self._runtimes_lock:
            rt = self._runtimes.get(cid)
            if rt is not None:
                return rt
            rt = _ChatRuntime(cid)
            self._runtimes[cid] = rt
            rt.thread = threading.Thread(
                target=self._run_chat_loop,
                args=(rt,),
                name=f"codewood-chat-{cid[:8] or 'main'}",
                daemon=True,
            )
            rt.thread.start()
            return rt

    def answer_confirm(self, cid: str, answer: str) -> bool:
        with self._confirms_lock:
            reply = self._confirms.get(str(cid or ""))
        if reply is None:
            return False
        reply.put(str(answer or ""))
        return True

    def answer_ask_more_info(self, pid: str, answer: str) -> bool:
        """Resolve a pending ``ask_more_info`` prompt with the user's answer.

        Returns ``False`` when no such pending prompt exists (e.g. the
        request was cancelled, already answered, or the chat was reset
        between rendering and answering); the frontend treats that as a
        no-op and just dismisses the panel.
        """
        with self._ask_more_info_lock:
            reply = self._ask_more_info.get(str(pid or ""))
        if reply is None:
            return False
        reply.put(str(answer or ""))
        return True

    def interrupt(self) -> None:
        agent = self.agent
        for name in ("_mark_process_interrupt_requested", "_terminate_interruptible_processes"):
            try:
                fn = getattr(agent, name, None)
                if callable(fn):
                    fn()
            except Exception:
                pass

    def state(self) -> Dict[str, Any]:
        return _build_state(self.agent)

    def _resolve_workspace_root(self, ws_id: str) -> Optional[str]:
        """Map a workspace id to its on-disk root using agent state only.

        The client never supplies a path; only an id that must match a known
        workspace, preventing arbitrary-path disclosure/open.
        """
        ws_id = str(ws_id or "").strip()
        if not ws_id:
            return None
        agent = self.agent
        try:
            raw = agent._workspaces_state.get("workspaces", {})
        except Exception:
            raw = None
        if isinstance(raw, dict):
            for entry in raw.values():
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("id") or "") != ws_id:
                    continue
                try:
                    root = str(agent._workspace_root_path(entry))
                except Exception:
                    root = str(entry.get("root") or "")
                return root or None
        if str(getattr(agent, "workspace_id", "") or "") == ws_id:
            return str(getattr(agent, "workspace_root", "") or "") or None
        return None

    def open_workspace(self, ws_id: str) -> bool:
        root = self._resolve_workspace_root(ws_id)
        if not root:
            return False
        return _open_in_file_manager(root)

    def chat_history(self, before: Optional[int], limit: int) -> Dict[str, Any]:
        """Return a paginated slice of structured turns for the active chat.

        ``before`` is the exclusive end index (0-based among turns); ``None``
        means "from the end". The newest ``limit`` turns up to ``before`` are
        returned along with ``start`` (the index of the first returned turn) and
        the overall ``total`` count, so the GUI can lazily load older turns.
        """
        # Reading history happens on an HTTP handler thread; bind it to the
        # focused chat so the per-session conversation_history resolves to that
        # chat's live session.
        try:
            focus_chat = _primary_active_chat_id(self.agent)
            # When the active chat is not actively streaming a turn here
            # (no busy runtime), pull the latest record from disk before
            # building turns so messages, ``pending_ask_more_info`` markers
            # and plan updates written by a peer process (e.g. another
            # codewood TUI) are reflected. An idle parked runtime no longer
            # blocks the refresh, which is what lets the GUI pick up a
            # changed history on switch/reload.
            is_busy = self._chat_is_busy(focus_chat)
            if focus_chat and not is_busy:
                try:
                    refresh = getattr(self.agent, "_refresh_chat_record_from_disk", None)
                    if callable(refresh):
                        refresh(focus_chat)
                        # Rebind the session so conversation_history
                        # reflects the freshly re-validated chat dict.
                        self.agent._activate_chat(
                            focus_chat,
                            announce=False,
                            clear_screen=False,
                            print_history=False,
                        )
                except Exception:
                    pass
            with self.agent._session_scope(focus_chat):
                turns = _build_structured_turns(self.agent)
        except Exception:
            turns = []
        total = len(turns)
        if limit <= 0:
            limit = 12
        if before is None or before < 0 or before > total:
            end = total
        else:
            end = before
        start = max(0, end - limit)
        return {"turns": turns[start:end], "start": start, "total": total}

    def select_chat(self, chat_id: str, workspace_id: str = "") -> bool:
        """Silently switch the focused workspace and/or chat (no history replay).

        No longer refused while a task is running: a chat with a live loop keeps
        executing in the background, so switching focus must not reload its
        conversation from disk (that would clobber the in-progress turn). For a
        chat that has a running runtime we only move the focus pointer; for an
        idle chat we fully activate it (binding + loading its session).
        ``chat_id`` may be empty to only switch workspace.
        """
        import contextlib

        cid = str(chat_id or "").strip()
        wsid = str(workspace_id or "").strip()
        if not cid and not wsid:
            return False
        agent = self.agent
        try:
            if wsid and wsid != str(getattr(agent, "workspace_id", "") or ""):
                # A workspace switch rebinds the agent's SHARED, workspace-level
                # runtime: it reloads ``_chat_state`` with the target
                # workspace's chat index, swaps ``workspace_root`` /
                # ``work_directory``, and may tear down workspace services / MCP
                # runtime. Those are process-global (not per-session), so doing
                # it while another chat's loop is mid-turn destroys that chat's
                # execution environment: when the background chat finishes,
                # ``find_chat_by_id`` no longer locates its record in the
                # now-replaced ``_chat_state`` and its reply is silently dropped
                # (never persisted to history) — exactly the "switch away while
                # waiting and the reply is lost" symptom. Refuse the switch
                # while any chat is actively running so the in-flight task keeps
                # its workspace context and persists normally.
                running_ids: list = []
                try:
                    runner = getattr(agent, "_active_runtime_chat_ids", None)
                    if callable(runner):
                        running_ids = [str(x) for x in (runner() or []) if str(x)]
                except Exception:
                    running_ids = []
                if running_ids:
                    return False

                from ..controllers.workspace_command_controller import (
                    workspace_switch_command,
                )

                # The command returns a status string; redirect any incidental
                # output so nothing leaks into the SSE stream.
                with contextlib.redirect_stdout(io.StringIO()):
                    workspace_switch_command(agent, wsid)
            if cid:
                with agent._chat_state_lock:
                    target = agent._resolve_chat_selector(cid)
                    rid = str(target.get("id") or "") if target else ""
                if not rid:
                    return False
                if self._chat_is_busy(rid):
                    # A turn is actively streaming here: the live loop owns this
                    # chat's session, so just repoint the workspace's active
                    # chat and persist it without touching conversation_history
                    # (reloading would clobber the in-progress turn).
                    with agent._chat_state_lock:
                        agent._chat_state["active"] = rid
                        agent._save_chat_state()
                else:
                    # The chat is idle here (it may have no runtime, or a parked
                    # one). Another codewood process (typically the TUI) may have
                    # amended it on disk since we last loaded it, so pull its
                    # latest record before binding the session. This is what
                    # makes "switch chats and pick up the peer's new messages /
                    # pending ask_more_info / plan updates" work, even when this
                    # process still holds an idle runtime for the chat.
                    try:
                        refresh = getattr(agent, "_refresh_chat_record_from_disk", None)
                        if callable(refresh):
                            refresh(rid)
                    except Exception:
                        pass
                    result = agent._activate_chat(
                        rid, announce=False, clear_screen=False, print_history=False
                    )
                    if result:
                        return False
        except Exception:
            return False
        # Use the non-terminating ``state`` event (not ``idle``) to push the
        # refreshed snapshot. Switching focus must never flip the GUI's busy
        # flag or freeze the live turn of the chat we land on: when the user
        # switches back to a chat whose loop is still streaming, an ``idle``
        # here would call ``endActiveTurn``/clear-busy for that chat even
        # though it is genuinely still running — wiping its in-progress reply
        # and the sidebar busy/blue dot, with no ``turn_start`` to restore
        # them. ``state`` only re-syncs the snapshot (driving the active-chat
        # history reload and plan panel) and leaves turn/busy state intact.
        self.broadcaster.publish(
            "state", {"state": _build_state(agent), "chatId": self._active_chat_id()}
        )
        return True

    def set_theme(self, theme: str) -> bool:
        """Persist the GUI theme preference to the GUI-only config file."""
        agent = self.agent
        try:
            from ..core.config.gui_config import (
                load_gui_config,
                normalize_theme,
                save_gui_config,
            )

            value = normalize_theme(theme)
            if not value:
                return False
            data = load_gui_config(agent.config_dir)
            data["theme"] = value
            save_gui_config(agent.config_dir, data)
        except Exception:
            return False
        return True

    def set_gui_language(self, language: str) -> bool:
        """Persist a GUI-only display language without touching the TUI's locale.

        The agent's ``display_language`` continues to drive the TUI, system
        prompts and tool output language. This setting overrides only the
        webview's rendered locale.
        """
        agent = self.agent
        try:
            from ..core.config.gui_config import (
                load_gui_config,
                normalize_gui_language,
                save_gui_config,
            )

            value = normalize_gui_language(language)
            if not value:
                return False
            data = load_gui_config(agent.config_dir)
            data["language"] = value
            save_gui_config(agent.config_dir, data)
        except Exception:
            return False
        # Push a fresh state snapshot so the webview immediately re-renders
        # with the new locale without waiting for the next agent tick.
        try:
            self.broadcaster.publish(
                "idle", {"state": _build_state(agent), "chatId": self._active_chat_id()}
            )
        except Exception:
            pass
        return True

    def set_ui_prefs(self, prefs: Dict[str, Any]) -> bool:
        """Persist GUI presentation prefs (pin/archive) to the GUI-only file.

        These are GUI-only and never affect the TUI; storing them server-side
        keeps them across restarts even when the webview clears localStorage.
        """
        if not isinstance(prefs, dict):
            return False
        agent = self.agent
        try:
            from ..core.config.gui_config import (
                load_gui_config,
                normalize_ui_prefs,
                save_gui_config,
            )

            data = load_gui_config(agent.config_dir)
            data["uiPrefs"] = normalize_ui_prefs(prefs)
            save_gui_config(agent.config_dir, data)
        except Exception:
            return False
        return True

    # General settings (auto_compact / max_tool_rounds / memory / mcp_tools)
    # ----------------------------------------------------------------------
    # These four toggles live at the top level of ``config.jsonc`` because they
    # change agent runtime behavior and must remain consistent between the TUI
    # and the GUI. We surface them through dedicated endpoints rather than the
    # generic ``/save-models-config`` path so the GUI never has to read or
    # rewrite the rest of the config file just to flip a single switch.
    _GENERAL_KEYS = (
        "auto_compact_trigger_percent",
        "max_tool_rounds",
        "memory_enabled",
        "mcp_tools_enabled",
    )

    def get_general_config(self) -> Dict[str, Any]:
        """Return current values for the General settings panel.

        Falls back to live agent attributes when ``config.jsonc`` is missing or
        unreadable so the panel always renders the same values that the
        running agent is actually using.
        """
        agent = self.agent
        # Always start from the live agent attributes; the file may not exist
        # yet on a brand-new install but those attributes were populated from
        # bootstrap defaults.
        out: Dict[str, Any] = {
            "auto_compact_trigger_percent": int(
                getattr(agent, "auto_compact_trigger_percent", 0) or 0
            ),
            "max_tool_rounds": getattr(agent, "max_tool_rounds", None),
            "memory_enabled": bool(getattr(agent, "memory_enabled", False)),
            "mcp_tools_enabled": bool(getattr(agent, "mcp_tools_enabled", False)),
        }
        try:
            from ..core.config.config_jsonc import (
                CONFIG_JSONC_FILENAME,
                load_config_jsonc,
            )

            cfg_path = agent.config_dir / CONFIG_JSONC_FILENAME
            if cfg_path.exists():
                cfg = load_config_jsonc(cfg_path) or {}
                if isinstance(cfg, dict):
                    if "auto_compact_trigger_percent" in cfg:
                        try:
                            out["auto_compact_trigger_percent"] = int(
                                cfg.get("auto_compact_trigger_percent") or 0
                            )
                        except Exception:
                            pass
                    if "max_tool_rounds" in cfg:
                        mtr = cfg.get("max_tool_rounds")
                        if mtr is None:
                            out["max_tool_rounds"] = None
                        else:
                            try:
                                out["max_tool_rounds"] = int(mtr)
                            except Exception:
                                pass
                    if "memory_enabled" in cfg:
                        out["memory_enabled"] = bool(cfg.get("memory_enabled"))
                    if "mcp_tools_enabled" in cfg:
                        out["mcp_tools_enabled"] = bool(cfg.get("mcp_tools_enabled"))
        except Exception:
            pass
        return out

    def save_general_config(self, payload: Dict[str, Any]) -> bool:
        """Persist General settings to ``config.jsonc`` and apply immediately.

        Validates each field, rejects clearly out-of-range values, and applies
        the result to the live agent so the change takes effect without
        restarting the process. The rest of ``config.jsonc`` is preserved.
        """
        if not isinstance(payload, dict):
            return False
        normalized: Dict[str, Any] = {}
        if "auto_compact_trigger_percent" in payload:
            try:
                pct = int(payload.get("auto_compact_trigger_percent"))
            except Exception:
                return False
            if pct < 0 or pct > 100:
                return False
            normalized["auto_compact_trigger_percent"] = pct
        if "max_tool_rounds" in payload:
            raw = payload.get("max_tool_rounds")
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                normalized["max_tool_rounds"] = None
            else:
                try:
                    rounds = int(raw)
                except Exception:
                    return False
                if rounds <= 0:
                    normalized["max_tool_rounds"] = None
                else:
                    normalized["max_tool_rounds"] = rounds
        if "memory_enabled" in payload:
            normalized["memory_enabled"] = bool(payload.get("memory_enabled"))
        if "mcp_tools_enabled" in payload:
            normalized["mcp_tools_enabled"] = bool(payload.get("mcp_tools_enabled"))
        if not normalized:
            return False
        agent = self.agent
        try:
            from ..core.config.config_jsonc import (
                CONFIG_JSONC_FILENAME,
                load_config_jsonc,
                save_config_jsonc,
            )

            cfg_path = agent.config_dir / CONFIG_JSONC_FILENAME
            cfg_data: Dict[str, Any] = {}
            if cfg_path.exists():
                try:
                    cfg_data = load_config_jsonc(cfg_path) or {}
                except Exception:
                    cfg_data = {}
            if not isinstance(cfg_data, dict):
                cfg_data = {}
            for key, value in normalized.items():
                cfg_data[key] = value
            save_config_jsonc(cfg_path, cfg_data)
        except Exception:
            return False
        # Apply to the live agent so the change takes effect immediately.
        try:
            for key, value in normalized.items():
                setattr(agent, key, value)
        except Exception:
            pass
        # Drop the resolved-config cache so downstream consumers re-read fresh.
        try:
            agent._resolved_config_data = {}
        except Exception:
            pass
        # Push a fresh state snapshot to refresh any open settings page.
        try:
            self.broadcaster.publish(
                "idle", {"state": _build_state(agent), "chatId": self._active_chat_id()}
            )
        except Exception:
            pass
        return True

    def get_completion_catalog(self) -> Dict[str, Any]:
        """Return suggestion sources used by the composer's slash popup.

        Includes:
          - ``skills``: list of {name, description}. Sourced from the loaded
            skill records on the agent (already merged across builtin/global/
            workspace skill roots and respecting language).
          - ``mcpTools``: list of {server, name, description} for every
            enabled MCP server with a cached catalog. Disabled tools (via
            ``disabled_tools`` policy) are filtered out so the user can't
            invoke them by typing ``/``.
          - ``mcpPrompts``: list of {server, name, description} sourced the
            same way.

        The endpoint is intentionally cheap (cache-only, no live MCP I/O) so
        it can be polled on every slash keypress without throttling.
        """
        agent = self.agent
        out_skills: List[Dict[str, str]] = []
        try:
            for record in getattr(agent, "skills", []) or []:
                name = str(getattr(record, "name", "") or "")
                desc = str(getattr(record, "description", "") or "")
                if name:
                    out_skills.append({"name": name, "description": desc})
        except Exception:
            out_skills = []
        out_tools: List[Dict[str, str]] = []
        out_prompts: List[Dict[str, str]] = []
        try:
            mgr = getattr(agent, "mcp_manager", None)
            if mgr is not None:
                cfg = getattr(mgr, "mcp_config", {}) or {}
                servers_cfg = cfg.get("mcpServers") if isinstance(cfg, dict) else {}
                if isinstance(servers_cfg, dict):
                    for srv, conf in servers_cfg.items():
                        if not isinstance(conf, dict):
                            continue
                        if bool(conf.get("skip_preload", False)):
                            continue
                        try:
                            tools, _ = mgr.list_tools(str(srv), use_cache=True)
                        except Exception:
                            tools = []
                        try:
                            prompts, _ = mgr.list_prompts(str(srv), use_cache=True)
                        except Exception:
                            prompts = []
                        for t in tools or []:
                            if not isinstance(t, dict):
                                continue
                            tn = str(t.get("name") or "")
                            if not tn:
                                continue
                            out_tools.append(
                                {
                                    "server": str(srv),
                                    "name": tn,
                                    "description": str(t.get("description") or ""),
                                }
                            )
                        for p in prompts or []:
                            if not isinstance(p, dict):
                                continue
                            pn = str(p.get("name") or "")
                            if not pn:
                                continue
                            out_prompts.append(
                                {
                                    "server": str(srv),
                                    "name": pn,
                                    "description": str(p.get("description") or ""),
                                }
                            )
        except Exception:
            out_tools = out_tools or []
            out_prompts = out_prompts or []
        return {
            "skills": out_skills,
            "mcpTools": out_tools,
            "mcpPrompts": out_prompts,
        }

    # MCP settings: per-server enable/disable + per-tool toggle.
    # ----------------------------------------------------------------------
    # The GUI's MCP page reads ``mcp.jsonc`` (server list) and the live
    # mcp_manager (status + tool/prompt catalog + per-tool policy), exposes a
    # consolidated view, and applies changes back to both. Per-server enable
    # state maps to the ``skip_preload`` flag — ``true`` skips startup preload
    # and reconnection, ``false`` makes the server eligible. Per-tool toggles
    # use the manager's existing ``disable_tools/enable_tools`` policy.

    def _mcp_load_jsonc(self) -> Dict[str, Any]:
        try:
            from ..core.config.config_jsonc import load_config_jsonc

            path = self.agent.config_dir / "mcp.jsonc"
            if not path.is_file():
                return {"mcpServers": {}}
            data = load_config_jsonc(path) or {}
            if not isinstance(data, dict):
                return {"mcpServers": {}}
            servers = data.get("mcpServers")
            if not isinstance(servers, dict):
                data["mcpServers"] = {}
            return data
        except Exception:
            return {"mcpServers": {}}

    def _mcp_save_jsonc(self, data: Dict[str, Any]) -> bool:
        try:
            from ..core.config.config_jsonc import save_config_jsonc

            path = self.agent.config_dir / "mcp.jsonc"
            save_config_jsonc(path, data)
            return True
        except Exception:
            return False

    def search_workspace_files(
        self, query: str, workspace_id: str = "", limit: int = 10
    ) -> Dict[str, Any]:
        """Filename search for the GUI ``@`` quick file-reference dropdown.

        Returns up to ``limit`` workspace-relative paths whose name matches
        ``query`` (case-insensitive). ``query`` may be empty to surface the
        shallowest files right after the user types a bare ``@``.
        """
        try:
            from ..tools.project_context_index import search_workspace_files
        except Exception:
            return {"ok": False, "candidates": []}
        root = ""
        wsid = str(workspace_id or "").strip()
        if wsid:
            root = self._resolve_workspace_root(wsid) or ""
        if not root:
            root = str(getattr(self.agent, "workspace_root", "") or "")
        if not root:
            return {"ok": False, "candidates": []}
        try:
            cap = max(1, min(50, int(limit or 10)))
        except Exception:
            cap = 10
        try:
            from pathlib import Path as _Path

            candidates = search_workspace_files(_Path(root), str(query or ""), cap)
        except Exception:
            candidates = []
        return {"ok": True, "candidates": candidates}

    def get_mcp_overview(self) -> Dict[str, Any]:
        """Return a snapshot of every configured MCP server for the GUI page.

        Each entry combines: configured (``skip_preload`` flipped to ``enabled``),
        live status (state/source/tool counts) from ``mcp_manager.get_status``,
        and the per-server disabled-tool list. Tools and prompts themselves are
        intentionally NOT fetched here to keep the page snappy — the frontend
        requests them lazily when the user expands a server.
        """
        agent = self.agent
        cfg = self._mcp_load_jsonc()
        servers_cfg = cfg.get("mcpServers")
        if not isinstance(servers_cfg, dict):
            servers_cfg = {}
        statuses: Dict[str, Any] = {}
        disabled_by_server: Dict[str, List[str]] = {}
        try:
            mgr = getattr(agent, "mcp_manager", None)
            if mgr is not None:
                snap = mgr.get_status() or {}
                items = snap.get("items") if isinstance(snap, dict) else None
                if isinstance(items, dict):
                    statuses = items
                try:
                    disabled_by_server = {
                        str(k): list(v) for k, v in (mgr.list_disabled_tools() or {}).items()
                    }
                except Exception:
                    disabled_by_server = {}
        except Exception:
            statuses = {}
            disabled_by_server = {}

        out: List[Dict[str, Any]] = []
        for name, conf in servers_cfg.items():
            if not isinstance(conf, dict):
                conf = {}
            enabled = not bool(conf.get("skip_preload", False))
            status = statuses.get(name) if isinstance(statuses, dict) else None
            status_dict = status if isinstance(status, dict) else {}
            transport = ""
            if "url" in conf:
                transport = "http"
            elif "command" in conf:
                transport = "stdio"
            # ``get_status`` reports the counts it has cached on its in-memory
            # status dict, but the timing varies: when the GUI opens the
            # settings page just after launch the status entry may still be
            # ``loading`` and report ``0`` even though the catalog cache for
            # this server has already been hydrated by a prior list_tools
            # call. Fall back to a direct cache lookup so the row reflects
            # the true catalog size instead of a transient zero.
            tools_count = int(status_dict.get("tools_count") or 0)
            prompts_count = int(status_dict.get("prompts_count") or 0)
            if enabled and (tools_count == 0 or prompts_count == 0):
                mgr_obj = getattr(agent, "mcp_manager", None)
                if mgr_obj is not None:
                    if tools_count == 0:
                        try:
                            cached_tools, _ = mgr_obj.list_tools(str(name), use_cache=True)
                            if isinstance(cached_tools, list):
                                tools_count = len(cached_tools)
                        except Exception:
                            pass
                    if prompts_count == 0:
                        try:
                            cached_prompts, _ = mgr_obj.list_prompts(str(name), use_cache=True)
                            if isinstance(cached_prompts, list):
                                prompts_count = len(cached_prompts)
                        except Exception:
                            pass
            out.append(
                {
                    "name": str(name),
                    "enabled": enabled,
                    "transport": transport,
                    "state": str(status_dict.get("state") or ""),
                    "lastError": str(status_dict.get("last_error") or ""),
                    "toolsCount": tools_count,
                    "promptsCount": prompts_count,
                    "disabledTools": disabled_by_server.get(str(name), []),
                }
            )
        return {"servers": out}

    def get_mcp_server_details(self, name: str) -> Dict[str, Any]:
        """Fetch the tool/prompt catalog for a single server (cache-first)."""
        srv = str(name or "").strip()
        if not srv:
            return {"ok": False, "tools": [], "prompts": []}
        agent = self.agent
        mgr = getattr(agent, "mcp_manager", None)
        if mgr is None:
            return {"ok": False, "tools": [], "prompts": []}
        tools: List[Dict[str, Any]] = []
        prompts: List[Dict[str, Any]] = []
        try:
            t, _ = mgr.list_tools(srv, use_cache=True)
            tools = list(t) if isinstance(t, list) else []
        except Exception:
            tools = []
        try:
            p, _ = mgr.list_prompts(srv, use_cache=True)
            prompts = list(p) if isinstance(p, list) else []
        except Exception:
            prompts = []
        disabled_names: List[str] = []
        try:
            mapping = mgr.list_disabled_tools(srv) or {}
            disabled_names = list(mapping.get(srv, []))
        except Exception:
            disabled_names = []
        # Trim each tool/prompt to the fields the GUI actually renders to
        # keep payloads small (some MCP catalogs are very chatty).
        def _slim(item: Dict[str, Any]) -> Dict[str, Any]:
            if not isinstance(item, dict):
                return {}
            return {
                "name": str(item.get("name") or ""),
                "description": str(item.get("description") or ""),
            }

        return {
            "ok": True,
            "tools": [_slim(x) for x in tools],
            "prompts": [_slim(x) for x in prompts],
            "disabledTools": sorted({str(x) for x in disabled_names if str(x)}),
        }

    def set_mcp_server_enabled(self, name: str, enabled: bool) -> bool:
        """Toggle a server's ``skip_preload`` flag and apply the change live.

        When enabling we attempt an immediate reconnect so the GUI can show
        live status without waiting for the next manual refresh; when
        disabling we just flip the flag (existing sessions are left to time
        out naturally rather than ripped down here).
        """
        srv = str(name or "").strip()
        if not srv:
            return False
        try:
            cfg = self._mcp_load_jsonc()
            servers = cfg.get("mcpServers")
            if not isinstance(servers, dict) or srv not in servers:
                return False
            entry = servers.get(srv)
            if not isinstance(entry, dict):
                entry = {}
            entry = dict(entry)
            entry["skip_preload"] = not bool(enabled)
            servers[srv] = entry
            cfg["mcpServers"] = servers
            if not self._mcp_save_jsonc(cfg):
                return False
        except Exception:
            return False
        # Apply to the live agent: refresh in-memory config + reconnect on
        # enable so the user sees status update right away.
        try:
            agent = self.agent
            agent.mcp_config = cfg
            agent._mcp_config_struct_sig = agent._calc_mcp_config_sig(cfg)
            mgr = getattr(agent, "mcp_manager", None)
            if mgr is not None:
                try:
                    mgr.mcp_config = cfg
                except Exception:
                    pass
                if enabled:
                    try:
                        mgr.reconnect_server(srv, timeout_s=12.0)
                    except Exception:
                        pass
        except Exception:
            pass
        # Push fresh state so the page reflects the change immediately.
        try:
            self.broadcaster.publish(
                "idle", {"state": _build_state(self.agent), "chatId": self._active_chat_id()}
            )
        except Exception:
            pass
        return True

    def set_plan_mode(self, enabled: bool) -> bool:
        """Toggle the agent's sticky plan mode.

        The GUI uses this to keep the user's message bubble free of any
        injected planning instruction: instead of prefixing the outgoing
        message text, the GUI flips this flag and lets ``runtime_loop``
        prepend the localized instruction inside the agent boundary. The
        flag is shared with the TUI's ``/plan`` command — flipping it from
        the GUI is equivalent to a TUI ``/plan on`` for the same process.
        """
        try:
            self.agent._plan_mode_sticky = bool(enabled)
        except Exception:
            return False
        return True

    def get_mcp_server_config(self, name: str) -> Dict[str, Any]:
        """Return the raw mcp.jsonc entry for a single server (or ``{}``)."""
        srv = str(name or "").strip()
        if not srv:
            return {}
        try:
            cfg = self._mcp_load_jsonc()
            servers = cfg.get("mcpServers")
            if not isinstance(servers, dict):
                return {}
            entry = servers.get(srv)
            return entry if isinstance(entry, dict) else {}
        except Exception:
            return {}

    def set_mcp_tool_enabled(self, server: str, tool: str, enabled: bool) -> bool:
        """Toggle a single tool's disabled-by-policy state."""
        srv = str(server or "").strip()
        name = str(tool or "").strip()
        if not srv or not name:
            return False
        mgr = getattr(self.agent, "mcp_manager", None)
        if mgr is None:
            return False
        try:
            if enabled:
                mgr.enable_tools(srv, [name])
            else:
                mgr.disable_tools(srv, [name])
        except Exception:
            return False
        return True

    def set_mcp_tools_enabled(
        self, server: str, tools: Any, enabled: bool
    ) -> bool:
        """Bulk-toggle several tools' disabled-by-policy state at once.

        Used by the MCP settings page's "Enable all" / "Disable all" button
        so the page can flip every tool on a server in a single round-trip
        instead of sending one request per tool (which both flickers the UI
        and risks the writes interleaving with a concurrent reload).
        """
        srv = str(server or "").strip()
        if not srv:
            return False
        if not isinstance(tools, (list, tuple)):
            return False
        # Bound the per-name length and overall count to keep this from
        # turning into an abuse vector via a runaway payload.
        names: List[str] = []
        for item in tools[:1024]:
            n = str(item or "").strip()[:256]
            if n:
                names.append(n)
        if not names:
            return False
        mgr = getattr(self.agent, "mcp_manager", None)
        if mgr is None:
            return False
        try:
            if enabled:
                mgr.enable_tools(srv, names)
            else:
                mgr.disable_tools(srv, names)
        except Exception:
            return False
        return True

    # ------------------------------------------------------------------
    # MCP add / update / delete
    #
    # The GUI editor for MCP servers writes to ``mcp.jsonc`` and then asks
    # the live ``mcp_manager`` to pick up the change (reconnect on add /
    # update, disconnect on delete). The form is intentionally minimal —
    # only the fields the UI exposes are read; anything else in the
    # original entry is preserved so the user can keep custom fields like
    # ``description`` or ``trust`` by hand.
    # ------------------------------------------------------------------

    _ALLOWED_MCP_FIELDS = (
        "command",
        "args",
        "env",
        "url",
        "headers",
        "skip_preload",
        "transport",
        "timeout",
    )

    def _normalize_mcp_entry(self, payload: Any) -> Optional[Dict[str, Any]]:
        """Validate an MCP server entry coming from the GUI.

        Returns the cleaned dict on success or ``None`` if the payload is
        unusable. We require AT LEAST one of ``command`` (stdio) or ``url``
        (http/sse) so the entry actually addresses a server.
        """
        if not isinstance(payload, dict):
            return None
        out: Dict[str, Any] = {}
        cmd = payload.get("command")
        if isinstance(cmd, str) and cmd.strip():
            out["command"] = cmd.strip()
            args = payload.get("args")
            if isinstance(args, list):
                out["args"] = [str(a) for a in args if isinstance(a, (str, int, float))]
            env = payload.get("env")
            if isinstance(env, dict):
                out["env"] = {
                    str(k): str(v) for k, v in env.items() if isinstance(k, str) and k
                }
        url = payload.get("url")
        if isinstance(url, str) and url.strip():
            # Reject anything that isn't HTTP(S) — file:// / javascript: etc.
            # are not valid MCP transports and shouldn't be smuggled into
            # the config from the GUI even if the user pastes one in.
            u = url.strip()
            lower = u.lower()
            if not (lower.startswith("http://") or lower.startswith("https://")):
                return None
            out["url"] = u
            headers = payload.get("headers")
            if isinstance(headers, dict):
                out["headers"] = {
                    str(k): str(v) for k, v in headers.items() if isinstance(k, str) and k
                }
        if "command" not in out and "url" not in out:
            return None
        if "skip_preload" in payload:
            out["skip_preload"] = bool(payload.get("skip_preload"))
        if "transport" in payload and isinstance(payload["transport"], str):
            t = payload["transport"].strip().lower()
            if t in ("stdio", "http", "sse"):
                out["transport"] = t
        return out

    def _refresh_mcp_after_edit(self, cfg: Dict[str, Any], server: str, *, reconnect: bool) -> None:
        """Apply a freshly-written mcp.jsonc to the running agent + manager."""
        try:
            agent = self.agent
            agent.mcp_config = cfg
            agent._mcp_config_struct_sig = agent._calc_mcp_config_sig(cfg)
            mgr = getattr(agent, "mcp_manager", None)
            if mgr is not None:
                try:
                    mgr.mcp_config = cfg
                except Exception:
                    pass
                if reconnect:
                    try:
                        mgr.reconnect_server(server, timeout_s=12.0)
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            self.broadcaster.publish(
                "idle", {"state": _build_state(self.agent), "chatId": self._active_chat_id()}
            )
        except Exception:
            pass

    def add_mcp_server(self, name: str, config: Any) -> Dict[str, Any]:
        """Create a new MCP server entry. Returns ``{ok, error?}``."""
        srv = str(name or "").strip()
        if not srv or not srv.replace("-", "").replace("_", "").replace(".", "").isalnum():
            # Restrict to a conservative character set so the name is safe to
            # use as a JSON key / log identifier and matches what users
            # already see in the existing mcp.jsonc samples.
            return {"ok": False, "error": "invalid_name"}
        clean = self._normalize_mcp_entry(config)
        if clean is None:
            return {"ok": False, "error": "invalid_config"}
        try:
            cfg = self._mcp_load_jsonc()
            servers = cfg.get("mcpServers")
            if not isinstance(servers, dict):
                servers = {}
            if srv in servers:
                return {"ok": False, "error": "duplicate"}
            servers[srv] = clean
            cfg["mcpServers"] = servers
            if not self._mcp_save_jsonc(cfg):
                return {"ok": False, "error": "save_failed"}
        except Exception:
            return {"ok": False, "error": "save_failed"}
        # Reconnect by default unless the user explicitly added it disabled.
        self._refresh_mcp_after_edit(
            cfg, srv, reconnect=not bool(clean.get("skip_preload", False))
        )
        return {"ok": True}

    def update_mcp_server(
        self, original_name: str, name: str, config: Any
    ) -> Dict[str, Any]:
        """Update an existing MCP server entry. Supports rename."""
        old = str(original_name or "").strip()
        new = str(name or old or "").strip()
        if not old or not new:
            return {"ok": False, "error": "invalid_name"}
        if not new.replace("-", "").replace("_", "").replace(".", "").isalnum():
            return {"ok": False, "error": "invalid_name"}
        clean = self._normalize_mcp_entry(config)
        if clean is None:
            return {"ok": False, "error": "invalid_config"}
        try:
            cfg = self._mcp_load_jsonc()
            servers = cfg.get("mcpServers")
            if not isinstance(servers, dict):
                servers = {}
            if old not in servers:
                return {"ok": False, "error": "missing"}
            if new != old and new in servers:
                return {"ok": False, "error": "duplicate"}
            # Preserve fields the GUI form doesn't expose (e.g. custom
            # ``description`` / ``trust`` flags) by merging onto the prior
            # entry rather than replacing it wholesale.
            prior = servers[old] if isinstance(servers[old], dict) else {}
            merged = dict(prior)
            for k in self._ALLOWED_MCP_FIELDS:
                if k in merged:
                    del merged[k]
            merged.update(clean)
            del servers[old]
            servers[new] = merged
            cfg["mcpServers"] = servers
            if not self._mcp_save_jsonc(cfg):
                return {"ok": False, "error": "save_failed"}
        except Exception:
            return {"ok": False, "error": "save_failed"}
        self._refresh_mcp_after_edit(
            cfg, new, reconnect=not bool(clean.get("skip_preload", False))
        )
        return {"ok": True}

    def delete_mcp_server(self, name: str) -> Dict[str, Any]:
        """Remove an MCP server entry from ``mcp.jsonc``."""
        srv = str(name or "").strip()
        if not srv:
            return {"ok": False, "error": "invalid_name"}
        try:
            cfg = self._mcp_load_jsonc()
            servers = cfg.get("mcpServers")
            if not isinstance(servers, dict) or srv not in servers:
                return {"ok": False, "error": "missing"}
            del servers[srv]
            cfg["mcpServers"] = servers
            if not self._mcp_save_jsonc(cfg):
                return {"ok": False, "error": "save_failed"}
        except Exception:
            return {"ok": False, "error": "save_failed"}
        # Best-effort disconnect of the live session; failures are tolerable
        # since the next manager tick will notice the server is gone.
        try:
            mgr = getattr(self.agent, "mcp_manager", None)
            if mgr is not None:
                try:
                    self.agent.mcp_config = cfg
                except Exception:
                    pass
                try:
                    mgr.mcp_config = cfg
                except Exception:
                    pass
                disconnect = getattr(mgr, "disconnect_server", None)
                if callable(disconnect):
                    try:
                        disconnect(srv)
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            self.broadcaster.publish(
                "idle", {"state": _build_state(self.agent), "chatId": self._active_chat_id()}
            )
        except Exception:
            pass
        return {"ok": True}

    # ----- Sub-agents config CRUD ----------------------------------------
    def _available_subagent_tool_names(self) -> List[str]:
        """Tool names selectable for a sub-agent (core tools, no recursion)."""
        names: List[str] = []
        seen = set()
        try:
            for spec in list(getattr(self.agent, "tool_specs", []) or []):
                fn = spec.get("function") if isinstance(spec, dict) else None
                nm = str((fn or {}).get("name") or "").strip()
                if not nm or nm == "run_subagent" or nm in seen:
                    continue
                seen.add(nm)
                names.append(nm)
        except Exception:
            pass
        return names

    def get_subagents_overview(self) -> Dict[str, Any]:
        """List configured sub-agents plus the model/tool option catalogs."""
        from ..core.config.subagents_loader import list_subagents_for_config

        try:
            items = list_subagents_for_config(self.agent.config_dir)
        except Exception:
            items = []
        try:
            models = [s for s in self.agent._get_configured_model_selectors() if s]
        except Exception:
            models = []
        return {
            "subagents": items,
            "models": models,
            "tools": self._available_subagent_tool_names(),
        }

    def _reload_subagents(self) -> None:
        """Re-scan sub-agent files into the live agent after a config change."""
        try:
            from ..runtime import bootstrap

            bootstrap.setup_subagents(self.agent)
        except Exception:
            pass
        try:
            self.broadcaster.publish(
                "idle", {"state": _build_state(self.agent), "chatId": self._active_chat_id()}
            )
        except Exception:
            pass

    def save_subagent(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create or update a global sub-agent file from the config UI."""
        from ..core.config.subagents_loader import (
            DEFAULT_SUBAGENT_MAX_ROUNDS,
            write_subagent,
        )

        if not isinstance(payload, dict):
            return {"ok": False, "error": "invalid_payload"}
        tools_raw = payload.get("tools")
        tools = (
            [str(t).strip() for t in tools_raw if str(t or "").strip()]
            if isinstance(tools_raw, list)
            else []
        )
        try:
            max_rounds = int(payload.get("maxRounds") or DEFAULT_SUBAGENT_MAX_ROUNDS)
        except Exception:
            max_rounds = DEFAULT_SUBAGENT_MAX_ROUNDS
        try:
            result = write_subagent(
                self.agent.config_dir,
                original_name=str(payload.get("originalName") or ""),
                name=str(payload.get("name") or ""),
                description=str(payload.get("description") or ""),
                instructions=str(payload.get("instructions") or ""),
                model=str(payload.get("model") or ""),
                tools=tools,
                tools_specified=bool(payload.get("toolsSpecified", False)),
                max_rounds=max_rounds,
                enabled=bool(payload.get("enabled", True)),
            )
        except Exception:
            return {"ok": False, "error": "io_error"}
        if result.get("ok"):
            self._reload_subagents()
        return result

    def delete_subagent(self, name: str) -> Dict[str, Any]:
        """Delete a global sub-agent file by name."""
        from ..core.config.subagents_loader import delete_subagent as _delete

        try:
            result = _delete(self.agent.config_dir, str(name or ""))
        except Exception:
            return {"ok": False, "error": "io_error"}
        if result.get("ok"):
            self._reload_subagents()
        return result

    def set_subagent_enabled(self, name: str, enabled: bool) -> Dict[str, Any]:
        """Flip a global sub-agent's enabled flag."""
        from ..core.config.subagents_loader import set_subagent_enabled as _set

        try:
            result = _set(self.agent.config_dir, str(name or ""), bool(enabled))
        except Exception:
            return {"ok": False, "error": "io_error"}
        if result.get("ok"):
            self._reload_subagents()
        return result

    def get_models_config(self) -> List[Dict[str, Any]]:
        """Return the raw (unresolved) ``model_providers`` list for editing."""
        agent = self.agent
        try:
            from ..core.config.config_jsonc import (
                CONFIG_JSONC_FILENAME,
                load_config_jsonc,
            )

            cfg_path = agent.config_dir / CONFIG_JSONC_FILENAME
            if not cfg_path.exists():
                return []
            cfg = load_config_jsonc(cfg_path) or {}
            providers = cfg.get("model_providers")
            return providers if isinstance(providers, list) else []
        except Exception:
            return []

    def save_models_config(self, providers: List[Dict[str, Any]]) -> bool:
        """Persist a new ``model_providers`` list and apply it immediately.

        The raw list is written verbatim (env placeholders like ``${X}`` are
        preserved). The agent's resolved-config cache is cleared so the next
        catalog read reflects the change without an app restart.
        """
        if not isinstance(providers, list):
            return False
        agent = self.agent
        try:
            from ..core.config.config_jsonc import (
                CONFIG_JSONC_FILENAME,
                load_config_jsonc,
                save_config_jsonc,
            )

            cfg_path = agent.config_dir / CONFIG_JSONC_FILENAME
            cfg_data: Dict[str, Any] = {}
            if cfg_path.exists():
                try:
                    cfg_data = load_config_jsonc(cfg_path) or {}
                except Exception:
                    cfg_data = {}
            cfg_data["model_providers"] = providers
            save_config_jsonc(cfg_path, cfg_data)
            # Drop the resolved-config cache so the catalog re-reads from disk.
            try:
                agent._resolved_config_data = {}
            except Exception:
                pass
        except Exception:
            return False
        # Refresh GUI clients with the new available-model list.
        try:
            self.broadcaster.publish({"event": "idle", "data": {"state": self.state()}})
        except Exception:
            pass
        return True

    def fetch_provider_models(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch the model list from an OpenAI-compatible provider.

        Expects ``{base_url, api_key, api_mode}`` (api_key may be a ``${ENV}``
        placeholder, which is resolved before the call). Returns
        ``{ok, models:[name,...]}`` or ``{ok:false, error}``.
        """
        from ..core.config.config_env import resolve_string_values_in_data

        base_url = str(params.get("base_url") or "").strip()
        api_key_raw = str(params.get("api_key") or "").strip()
        api_mode = str(params.get("api_mode") or "").strip().lower()

        # Ollama has no base_url/api_key in the UI: derive a localhost URL from
        # the configured port (default 11434) and use its OpenAI-compatible API.
        if api_mode == "ollama":
            port_raw = params.get("port")
            try:
                port = int(port_raw) if port_raw not in (None, "") else 11434
            except (TypeError, ValueError):
                port = 11434
            base_url = f"http://localhost:{port}/v1"
            api_key = "ollama"
            try:
                from ..ai.ai_provider_clients import fetch_openai_compatible_models

                models = fetch_openai_compatible_models(base_url=base_url, api_key=api_key)
                return {"ok": True, "models": models}
            except Exception as e:  # noqa: BLE001 - surface a clean message to UI
                return {"ok": False, "error": str(e)[:300]}

        if not base_url:
            return {"ok": False, "error": "base_url is required"}
        try:
            resolved = resolve_string_values_in_data({"api_key": api_key_raw})
            api_key = str((resolved or {}).get("api_key") or "").strip()
        except Exception:
            api_key = api_key_raw
        try:
            from ..ai.ai_provider_clients import fetch_openai_compatible_models

            models = fetch_openai_compatible_models(base_url=base_url, api_key=api_key)
            return {"ok": True, "models": models}
        except Exception as e:  # noqa: BLE001 - surface a clean message to UI
            return {"ok": False, "error": str(e)[:300]}

    def _auto_refresh_models_on_startup(self) -> None:
        """For providers with ``auto_refresh: true``, refetch and enable all
        models on launch, then persist if anything changed.

        Failures (offline, bad key) are ignored so startup is never blocked.
        """
        providers = self.get_models_config()
        if not isinstance(providers, list) or not providers:
            return
        changed = False
        for entry in providers:
            if not isinstance(entry, dict):
                continue
            params = entry.get("params")
            if not isinstance(params, dict) or not params.get("auto_refresh"):
                continue
            result = self.fetch_provider_models(
                {
                    "base_url": params.get("base_url"),
                    "api_key": params.get("api_key"),
                    "api_mode": params.get("api_mode"),
                    "port": params.get("port"),
                }
            )
            if not result.get("ok"):
                continue
            fetched = result.get("models") or []
            if not isinstance(fetched, list) or not fetched:
                continue
            # Select all supported models, preserving any per-model overrides
            # already present in config (context_window/multimodal/headers).
            existing = {}
            for m in params.get("models") or []:
                if isinstance(m, dict) and m.get("name"):
                    existing[str(m.get("name"))] = m
                elif isinstance(m, str) and m:
                    existing[m] = {"name": m}
            new_models = []
            for name in fetched:
                new_models.append(existing.get(str(name), {"name": str(name)}))
            params["models"] = new_models
            changed = True
        if changed:
            self.save_models_config(providers)

    def new_chat(self) -> Optional[str]:
        """Silently create and activate a new chat; return its id.

        Not refused while other chats are running: the new chat gets its own
        loop thread on first input and is independent of any in-flight turn.
        """
        agent = self.agent
        try:
            from ..core.localization import get_display_language, translate

            name = translate("chat.new.default_name", get_display_language(agent))
            with agent._chat_state_lock:
                cid = agent._next_chat_id()
                agent._chat_entries().append(agent._new_chat_entry(cid, name=name))
                agent._save_chat_state()
            agent._activate_chat(
                cid, announce=False, clear_screen=False, print_history=False
            )
        except Exception:
            return None
        self.broadcaster.publish(
            "idle", {"state": _build_state(agent), "chatId": self._active_chat_id()}
        )
        return cid

    def delete_chat(self, chat_id: str, workspace_id: str = "") -> bool:
        """Delete a chat in the GUI, allowing the workspace to become chat-less.

        Unlike the TUI ``/chat delete`` command (which keeps at least one chat),
        the GUI can show a chat-less compose state, so removing the final chat is
        permitted. A chat whose agent loop is currently running is not deleted.
        """
        agent = self.agent
        cid = str(chat_id or "").strip()
        wsid = str(workspace_id or "").strip()
        if not cid:
            return False
        try:
            from ..controllers.workspace_command_controller import (
                workspace_switch_command,
            )

            if wsid and wsid != str(getattr(agent, "workspace_id", "") or ""):
                with agent._chat_state_lock:
                    workspace_switch_command(agent, wsid)
            with agent._chat_state_lock:
                target = agent._resolve_chat_selector(cid)
                rid = str(target.get("id") or "") if target else ""
            if not rid:
                return False
            # Refuse to delete a chat whose loop is mid-task; the user should
            # interrupt it first.
            with self._runtimes_lock:
                rt = self._runtimes.get(rid)
                if rt is not None and rt.busy.is_set():
                    return False
            with agent._chat_state_lock:
                chats = agent._chat_entries()
                was_active = rid == str(getattr(agent, "active_chat_id", "") or "")
                remaining = [c for c in chats if str(c.get("id") or "") != rid]
                chats[:] = remaining
                agent._chat_state["chats"] = chats
                if remaining:
                    if was_active:
                        agent._chat_state["active"] = str(remaining[0].get("id") or "")
                else:
                    # Chat-less workspace: clear the active marker; the GUI shows
                    # its compose (draft) state and creates a chat on next send.
                    agent._chat_state["active"] = ""
                agent._save_chat_state()
            # Drop the deleted chat's runtime (if any) and its session.
            with self._runtimes_lock:
                self._runtimes.pop(rid, None)
            try:
                reg = agent.__dict__.get("_session_registry")
                if isinstance(reg, dict):
                    reg.pop(rid, None)
            except Exception:
                pass
            if remaining and was_active:
                next_id = str(agent._chat_state.get("active") or "")
                if next_id:
                    with self._runtimes_lock:
                        has_runtime = next_id in self._runtimes
                    if not has_runtime:
                        agent._activate_chat(
                            next_id,
                            announce=False,
                            clear_screen=False,
                            print_history=False,
                        )
        except Exception:
            return False
        self.broadcaster.publish(
            "idle", {"state": _build_state(agent), "chatId": self._active_chat_id()}
        )
        return True

    def list_workspace_chats(self, ws_id: str) -> Optional[List[Dict[str, Any]]]:
        """List chats for a workspace by id without switching to it.

        The active workspace uses the fresher in-memory entries; other
        workspaces are read from their on-disk chat index. The id must match a
        known workspace; raw paths are never accepted.
        """
        ws_id = str(ws_id or "").strip()
        if not ws_id:
            return None
        agent = self.agent
        if str(getattr(agent, "workspace_id", "") or "") == ws_id:
            out: List[Dict[str, Any]] = []
            try:
                for c in agent._chat_entries():
                    if not isinstance(c, dict):
                        continue
                    out.append(
                        {
                            "id": str(c.get("id") or ""),
                            "name": str(c.get("name") or ""),
                            "updatedAt": str(c.get("updated_at") or ""),
                        }
                    )
            except Exception:
                return []
            return out

        try:
            raw = agent._workspaces_state.get("workspaces", {})
        except Exception:
            raw = None
        if not isinstance(raw, dict):
            return None
        for entry in raw.values():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("id") or "") != ws_id:
                continue
            try:
                storage = agent._workspace_storage_path(entry)
            except Exception:
                return []
            return _read_workspace_chat_index(storage)
        return None

    def request_shutdown(self) -> None:
        self._shutdown_event.set()
        # Send the exit sentinel to every chat loop so they all drain.
        with self._runtimes_lock:
            runtimes = list(self._runtimes.values())
        for rt in runtimes:
            try:
                rt.input_queue.put(None)
            except Exception:
                pass
        # Unblock any pending confirmation so the loops can drain.
        with self._confirms_lock:
            pending = list(self._confirms.values())
        for reply in pending:
            try:
                reply.put_nowait("n")
            except Exception:
                pass
        # Same for any unresolved ``ask_more_info`` prompts; pushing an
        # empty answer makes the runtime treat it as "no selection" and
        # pause the task cleanly instead of hanging the loop thread.
        with self._ask_more_info_lock:
            pending_ami = list(self._ask_more_info.values())
        for reply in pending_ami:
            try:
                reply.put_nowait("")
            except Exception:
                pass
        if self._httpd is not None:
            threading.Thread(target=self._httpd.shutdown, daemon=True).start()

    # ----- lifecycle -------------------------------------------------------
    def run(self, host: str = "127.0.0.1", port: int = 0) -> int:
        real_stdout = sys.stdout
        handler_cls = _make_handler(self)
        self._httpd = ThreadingHTTPServer((host, int(port or 0)), handler_cls)
        actual_port = int(self._httpd.server_address[1])

        # Handshake: one JSON line on the real stdout for the host to read.
        try:
            real_stdout.write(
                json.dumps({"port": actual_port, "token": self._token}) + "\n"
            )
            real_stdout.flush()
        except Exception:
            pass

        # Auto-refresh model lists for providers that opted in, before the agent
        # loop starts, so newly published models are selectable immediately.
        try:
            self._auto_refresh_models_on_startup()
        except Exception:
            pass

        # Install agent loop hooks before swapping stdout so output is captured.
        self.agent._get_user_input_with_history = self._input_provider  # type: ignore[assignment]
        self.agent._suspended_input = self._confirm_provider  # type: ignore[assignment]
        # The runtime loop calls this for ``ask_more_info`` so the GUI can
        # render clickable option chips instead of a raw text prompt.
        self.agent._ask_more_info_provider = self._ask_more_info_provider  # type: ignore[assignment]

        bridge = _OutputBridge(self.broadcaster, chat_id_getter=self._active_chat_id)
        self._bridge = bridge
        # GUI streaming mode: the runtime emits clean append-only deltas and
        # brackets the assistant reply with these hooks so the bridge can tag
        # those writes as "assistant" (vs "output" steps).
        self.agent._gui_plain_stream = True  # type: ignore[attr-defined]
        self.agent._gui_assistant_begin = lambda: bridge.set_tag("assistant")  # type: ignore[attr-defined]
        self.agent._gui_assistant_end = lambda: bridge.set_tag("output")  # type: ignore[attr-defined]
        # Each model round (one request->response within a turn) is bracketed so
        # the GUI can show a per-round "Working/Worked" wait timer and lay out
        # model text + tool output for that round in natural order. Scoped to the
        # chat bound to the calling loop thread so parallel chats stay separate.
        self.agent._gui_round_begin = lambda: self.broadcaster.publish(  # type: ignore[attr-defined]
            "round_start", {"chatId": self._active_chat_id()}
        )
        self.agent._gui_round_end = lambda: self.broadcaster.publish(  # type: ignore[attr-defined]
            "round_end", {"chatId": self._active_chat_id()}
        )
        # When the model updates its plan mid-turn, push a fresh state snapshot
        # so the GUI's plan panel reflects it immediately. We use the dedicated
        # ``state`` event (not ``idle``) because the loop is still actively
        # running — emitting ``idle`` here would falsely flip the GUI's busy
        # flag and freeze the current turn's "Working…" timer, which made the
        # Plan-mode "Execute now" button appear before the model had finished
        # streaming its plan reply.
        self.agent._gui_plan_changed = lambda: self.broadcaster.publish(  # type: ignore[attr-defined]
            "state", {"state": _build_state(self.agent), "chatId": self._active_chat_id()}
        )
        # The GUI renders its own layout, so disable terminal hard-wrapping and
        # force SGR color emission (stdout is not a TTY here). The bridge keeps
        # the SGR runs so the GUI can color step output like the terminal.
        self.agent._gui_no_wrap = True  # type: ignore[attr-defined]
        os.environ["FORCE_COLOR"] = "1"
        os.environ.pop("NO_COLOR", None)
        prev_stdout, prev_stderr = sys.stdout, sys.stderr
        sys.stdout = bridge
        sys.stderr = bridge

        # Spawn the loop for the startup-focused chat. Other chats get their own
        # loop thread lazily, the first time input is routed to them.
        self._get_or_spawn_runtime(_primary_active_chat_id(self.agent))

        try:
            self._httpd.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.request_shutdown()
            sys.stdout = prev_stdout
            sys.stderr = prev_stderr
            try:
                self._httpd.server_close()
            except Exception:
                pass
            try:
                self.agent.shutdown(wait=False)
            except Exception:
                pass
        return 0

    def _run_chat_loop(self, rt: "_ChatRuntime") -> None:
        """Run one chat's agent loop on its own thread, bound to its session.

        The chat's :class:`SessionState` is expected to already hold its
        conversation (loaded by the startup activation, ``select_chat``, or
        ``new_chat`` before input is routed here); this thread only binds to it
        so every per-session attribute the loop touches resolves correctly.
        """
        try:
            try:
                self.agent._bind_session(rt.chat_id)
            except Exception:
                pass

            from ..runtime.runtime_loop import run_agent_loop

            run_agent_loop(self.agent)
        except Exception:
            # Best-effort: surface fatal loop errors to subscribers (scoped to
            # this chat) without taking down the other chats' loops.
            try:
                self.broadcaster.publish(
                    "output", {"text": "\n[agent loop terminated]\n", "chatId": rt.chat_id}
                )
            except Exception:
                pass
        finally:
            with self._runtimes_lock:
                if self._runtimes.get(rt.chat_id) is rt:
                    self._runtimes.pop(rt.chat_id, None)


def _make_handler(app: ServeApp):
    class _Handler(BaseHTTPRequestHandler):
        server_version = "CodeWoodServe/1.0"
        protocol_version = "HTTP/1.1"

        # Silence default stderr request logging (would hit the SSE bridge).
        def log_message(self, *_args: Any) -> None:  # noqa: N802
            return None

        # ----- helpers -----
        def _is_loopback(self) -> bool:
            try:
                return str(self.client_address[0]) in _LOOPBACK_HOSTS
            except Exception:
                return False

        def _authorized(self, query: Dict[str, List[str]]) -> bool:
            header = self.headers.get("Authorization", "") or ""
            if header.startswith("Bearer "):
                if secrets.compare_digest(header[len("Bearer ") :].strip(), app.token):
                    return True
            token_param = (query.get("token") or [""])[0]
            if token_param and secrets.compare_digest(token_param, app.token):
                return True
            return False

        def _read_json_body(self) -> Optional[Dict[str, Any]]:
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                return None
            if length <= 0:
                return {}
            if length > _MAX_BODY_BYTES:
                return None
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                return None
            return data if isinstance(data, dict) else None

        def _send_cors(self) -> None:
            # Loopback-only, token-gated, no cookies: a permissive ACAO is
            # safe here and lets the WebView (file://, internal http server,
            # or the Vite dev origin) reach the API.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")

        def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._send_cors()
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self._send_cors()
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def _guard(self, query: Dict[str, List[str]]) -> bool:
            if not self._is_loopback():
                self._send_json(403, {"error": "forbidden"})
                return False
            if not self._authorized(query):
                self._send_json(401, {"error": "unauthorized"})
                return False
            return True

        # ----- routing -----
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path == "/health":
                self._send_json(200, {"ok": True})
                return
            if not self._guard(query):
                return
            if path == "/state":
                self._send_json(200, app.state())
                return
            if path == "/workspace-chats":
                ws_id = ""
                values = query.get("id") or []
                if values:
                    ws_id = str(values[0])[:256]
                result = app.list_workspace_chats(ws_id)
                if result is None:
                    self._send_json(404, {"error": "not found"})
                else:
                    self._send_json(200, {"chats": result})
                return
            if path == "/chat-history":
                before: Optional[int] = None
                limit = 12
                try:
                    before_vals = query.get("before") or []
                    if before_vals and str(before_vals[0]).strip():
                        before = int(str(before_vals[0])[:12])
                except (ValueError, TypeError):
                    before = None
                try:
                    limit_vals = query.get("limit") or []
                    if limit_vals and str(limit_vals[0]).strip():
                        limit = max(1, min(200, int(str(limit_vals[0])[:6])))
                except (ValueError, TypeError):
                    limit = 12
                self._send_json(200, app.chat_history(before, limit))
                return
            if path == "/events":
                self._stream_events()
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if not self._guard(query):
                return
            body = self._read_json_body()
            if body is None:
                self._send_json(400, {"error": "invalid body"})
                return
            if path == "/input":
                text = str(body.get("text") or "")
                if len(text) > _MAX_INPUT_CHARS:
                    self._send_json(413, {"error": "input too large"})
                    return
                chat_id = str(body.get("chatId") or "")[:256]
                app.submit_input(
                    text, chat_id=chat_id, as_prompt=bool(body.get("asPrompt"))
                )
                self._send_json(200, {"ok": True})
                return
            if path == "/confirm":
                cid = str(body.get("id") or "")
                answer = str(body.get("answer") or "")[:_MAX_CONFIRM_ANSWER_CHARS]
                ok = app.answer_confirm(cid, answer)
                self._send_json(200 if ok else 404, {"ok": ok})
                return
            if path == "/answer-ask-more-info":
                pid = str(body.get("id") or "")[:64]
                # Allow the "Other" freeform reply to carry a short
                # sentence (4 KB) but still bound it so a malicious
                # caller can't pile unbounded payloads onto the loop's
                # reply queue.
                answer = str(body.get("answer") or "")[:4096]
                ok = app.answer_ask_more_info(pid, answer)
                self._send_json(200 if ok else 404, {"ok": ok})
                return
            if path == "/interrupt":
                app.interrupt()
                self._send_json(200, {"ok": True})
                return
            if path == "/open-workspace":
                ws_id = str(body.get("id") or "")[:256]
                ok = app.open_workspace(ws_id)
                self._send_json(200 if ok else 404, {"ok": ok})
                return
            if path == "/select-chat":
                chat_id = str(body.get("id") or "")[:256]
                ws_id = str(body.get("workspaceId") or "")[:256]
                ok = app.select_chat(chat_id, ws_id)
                self._send_json(200 if ok else 409, {"ok": ok})
                return
            if path == "/new-chat":
                cid = app.new_chat()
                self._send_json(
                    200 if cid else 409, {"ok": bool(cid), "id": cid or ""}
                )
                return
            if path == "/delete-chat":
                chat_id = str(body.get("id") or "")[:256]
                ws_id = str(body.get("workspaceId") or "")[:256]
                ok = app.delete_chat(chat_id, ws_id)
                self._send_json(200 if ok else 409, {"ok": ok})
                return
            if path == "/set-theme":
                theme = str(body.get("theme") or "")[:16]
                ok = app.set_theme(theme)
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/set-gui-language":
                lang = str(body.get("language") or "")[:32]
                ok = app.set_gui_language(lang)
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/set-ui-prefs":
                prefs = body.get("prefs")
                ok = app.set_ui_prefs(prefs if isinstance(prefs, dict) else {})
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/models-config":
                self._send_json(200, {"ok": True, "providers": app.get_models_config()})
                return
            if path == "/save-models-config":
                providers = body.get("providers")
                ok = app.save_models_config(providers if isinstance(providers, list) else [])
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/general-config":
                self._send_json(200, {"ok": True, "general": app.get_general_config()})
                return
            if path == "/save-general-config":
                general = body.get("general")
                ok = app.save_general_config(general if isinstance(general, dict) else {})
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/mcp-overview":
                self._send_json(200, {"ok": True, **app.get_mcp_overview()})
                return
            if path == "/completion-catalog":
                self._send_json(200, {"ok": True, **app.get_completion_catalog()})
                return
            if path == "/mcp-server-details":
                srv = str(body.get("name") or "")[:256]
                payload = app.get_mcp_server_details(srv)
                self._send_json(200 if payload.get("ok") else 400, payload)
                return
            if path == "/mcp-server-config":
                srv = str(body.get("name") or "")[:256]
                self._send_json(200, {"ok": True, "config": app.get_mcp_server_config(srv)})
                return
            if path == "/set-plan-mode":
                ok = app.set_plan_mode(bool(body.get("enabled")))
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/search-workspace-files":
                query = str(body.get("query") or "")[:512]
                wsid = str(body.get("workspaceId") or "")[:128]
                try:
                    limit = int(body.get("limit") or 10)
                except Exception:
                    limit = 10
                result = app.search_workspace_files(query, wsid, limit)
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path == "/set-mcp-server-enabled":
                srv = str(body.get("name") or "")[:256]
                enabled = bool(body.get("enabled", True))
                ok = app.set_mcp_server_enabled(srv, enabled)
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/add-mcp-server":
                srv = str(body.get("name") or "")[:64]
                conf = body.get("config")
                result = app.add_mcp_server(srv, conf)
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path == "/update-mcp-server":
                old = str(body.get("originalName") or "")[:64]
                new = str(body.get("name") or "")[:64]
                conf = body.get("config")
                result = app.update_mcp_server(old, new, conf)
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path == "/delete-mcp-server":
                srv = str(body.get("name") or "")[:64]
                result = app.delete_mcp_server(srv)
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path == "/set-mcp-tool-enabled":
                srv = str(body.get("server") or "")[:256]
                tool = str(body.get("tool") or "")[:256]
                enabled = bool(body.get("enabled", True))
                ok = app.set_mcp_tool_enabled(srv, tool, enabled)
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/set-mcp-tools-enabled":
                srv = str(body.get("server") or "")[:256]
                tools = body.get("tools")
                enabled = bool(body.get("enabled", True))
                ok = app.set_mcp_tools_enabled(srv, tools, enabled)
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/subagents-overview":
                self._send_json(200, {"ok": True, **app.get_subagents_overview()})
                return
            if path == "/save-subagent":
                result = app.save_subagent(body if isinstance(body, dict) else {})
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path == "/delete-subagent":
                nm = str(body.get("name") or "")[:128]
                result = app.delete_subagent(nm)
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path == "/set-subagent-enabled":
                nm = str(body.get("name") or "")[:128]
                enabled = bool(body.get("enabled", True))
                result = app.set_subagent_enabled(nm, enabled)
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path == "/fetch-models":
                result = app.fetch_provider_models(body if isinstance(body, dict) else {})
                self._send_json(
                    200 if result.get("ok") else 400, result
                )
                return
            if path == "/shutdown":
                self._send_json(200, {"ok": True})
                app.request_shutdown()
                return
            self._send_json(404, {"error": "not found"})

        def _stream_events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self._send_cors()
            self.end_headers()
            sub = app.broadcaster.subscribe()
            try:
                # Prime the client with the current state immediately.
                self._write_sse({"event": "idle", "data": {"state": app.state()}})
                while not app._shutdown_event.is_set():
                    try:
                        message = sub.get(timeout=15.0)
                    except queue.Empty:
                        if not self._write_raw(b": ping\n\n"):
                            break
                        continue
                    if not self._write_sse(message):
                        break
            finally:
                app.broadcaster.unsubscribe(sub)

        def _write_sse(self, message: Dict[str, Any]) -> bool:
            try:
                payload = json.dumps(message, ensure_ascii=False)
            except Exception:
                return True
            chunk = f"data: {payload}\n\n".encode("utf-8")
            return self._write_raw(chunk)

        def _write_raw(self, chunk: bytes) -> bool:
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
                return True
            except Exception:
                return False

    return _Handler
