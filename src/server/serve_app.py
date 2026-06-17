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

from ..core.console_utils import GUI_FORCE_PROMPT_PREFIX

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
    """Group the active chat's history into GUI turns.

    Each turn is ``{"userText", "steps", "answer"}``. Internal command inputs
    (slash / direct shell) and command outputs are filtered out, so the GUI can
    render genuine user prompts, collapsible execution steps, and the final
    model reply as distinct blocks.
    """
    import contextlib

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
    sms = getattr(agent, "session_memory_service", None)

    def _ensure_turn() -> Dict[str, Any]:
        nonlocal current
        if current is None:
            current = {"userText": "", "steps": "", "answer": "", "elapsedSeconds": 0}
            turns.append(current)
        return current

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

    for idx, msg in enumerate(hist):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip().lower()
        content = str(msg.get("content") or "")
        if idx in genuine:
            current = {
                "userText": content,
                "steps": "",
                "answer": "",
                "elapsedSeconds": 0,
                "timestamp": str(msg.get("created_at") or ""),
            }
            turns.append(current)
            continue
        if role == "user":
            # Non-genuine user entries are internal command inputs (slash
            # commands or "!cmd" direct shell). The GUI never executes these
            # directly, so neither the command echo nor its output is shown.
            continue
        if role == "assistant":
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
            # Capture the turn's elapsed time; surfaced in the steps header
            # ("Worked for ...") rather than rendered as a step line.
            try:
                worked = agent._parse_task_worked_summary_history_content(content)
            except Exception:
                worked = None
            if worked is not None:
                # Attach to the in-progress turn only; never synthesize an
                # empty turn just to carry an elapsed value.
                if current is not None:
                    try:
                        current["elapsedSeconds"] = int(worked.get("elapsed_seconds") or 0)
                    except Exception:
                        pass
                continue

        if _is_answer(content):
            try:
                text = format_assistant_display_response(content) or ""
            except Exception:
                text = ""
            text = strip_ansi(str(text)).replace("\r\n", "\n").replace("\r", "\n").strip("\n")
            if not text.strip():
                continue
            turn = _ensure_turn()
            turn["answer"] = turn["answer"] + ("\n" if turn["answer"] else "") + text
            continue

        # Render an execution step using the agent's per-message renderer.
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                agent._render_transcript_single_message(idx, msg, hist)
        except Exception:
            pass
        text = strip_ansi_keep_sgr(buffer.getvalue()).replace("\r\n", "\n").replace("\r", "").strip("\n")
        if not text.strip():
            continue
        turn = _ensure_turn()
        turn["steps"] = turn["steps"] + text + "\n"

    for turn in turns:
        turn["steps"] = turn["steps"].rstrip("\n")
        turn["answer"] = turn["answer"].rstrip("\n")
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

    def __init__(self, broadcaster: _Broadcaster) -> None:
        self._broadcaster = broadcaster
        # Event name used for subsequent writes. The agent loop flips this to
        # "assistant" while streaming the final reply (via the GUI hooks) so
        # the GUI can render the answer separately from intermediate steps.
        self._tag = "output"
        # When set, writes are dropped instead of streamed. Used to hide the
        # output of internal slash commands the GUI runs on the user's behalf
        # (e.g. /chat rename, /workspace switch) from the message list.
        self.suppressed = False

    def set_tag(self, tag: str) -> None:
        self._tag = str(tag or "output")

    def write(self, s: Any) -> int:  # type: ignore[override]
        if s is None:
            return 0
        text = s if isinstance(s, str) else str(s)
        if not text:
            return 0
        if self.suppressed:
            # Consume silently so the command still runs but nothing streams.
            return len(text)
        # Keep SGR color runs (so the GUI can theme step output like the
        # terminal) but drop cursor/erase control sequences a non-TTY SSE
        # sink cannot honor. Normalize carriage returns to plain newlines.
        cleaned = strip_ansi_keep_sgr(text).replace("\r\n", "\n").replace("\r", "")
        if cleaned:
            self._broadcaster.publish(self._tag, {"text": cleaned})
        return len(text)

    def writable(self) -> bool:  # type: ignore[override]
        return True

    def flush(self) -> None:  # type: ignore[override]
        return None

    def isatty(self) -> bool:  # type: ignore[override]
        return False


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
    try:
        active_chat_id = str(getattr(agent, "active_chat_id", "") or "")
        for i, c in enumerate(agent._chat_entries(), start=1):
            if not isinstance(c, dict):
                continue
            chats.append(
                {
                    "index": i,
                    "id": str(c.get("id") or ""),
                    "name": str(c.get("name") or ""),
                    "messageCount": len(c.get("messages") or []),
                    "updatedAt": str(c.get("updated_at") or ""),
                    "active": str(c.get("id") or "") == active_chat_id,
                }
            )
    except Exception:
        pass

    model_current = ""
    model_available: List[str] = []
    try:
        model_current = str(agent._current_model_selector() or "")
    except Exception:
        pass
    try:
        model_available = [str(s) for s in (agent._get_configured_model_selectors() or []) if str(s)]
    except Exception:
        pass

    try:
        language = get_display_language(agent)
    except Exception:
        language = "en"

    theme = ""
    ui_prefs: Dict[str, Any] = {}
    try:
        cfg = agent._load_runtime_config_data()
        theme = str(cfg.get("theme") or "")
        raw_prefs = cfg.get("guiUiPrefs")
        if isinstance(raw_prefs, dict):
            ui_prefs = raw_prefs
    except Exception:
        theme = ""

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
        "activeChatId": str(getattr(agent, "active_chat_id", "") or ""),
        "model": {"current": model_current, "available": model_available},
        "language": language,
        "theme": theme,
        "uiPrefs": ui_prefs,
        "executionPolicy": str(getattr(agent, "execution_policy", "") or ""),
    }


class ServeApp:
    """Owns the agent loop, the event broadcaster, and the HTTP server."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.broadcaster = _Broadcaster()
        self._input_queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._confirms: Dict[str, "queue.Queue[str]"] = {}
        self._confirms_lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._token = secrets.token_urlsafe(32)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._busy = threading.Event()
        # Per-turn wall-clock tracking so the elapsed "Worked for" time is
        # persisted to chat history and survives a reload.
        self._turn_started_at: Optional[float] = None
        self._turn_record_pending = False
        self._bridge: Optional["_OutputBridge"] = None

    # ----- agent loop hooks ------------------------------------------------
    def _record_turn_elapsed(self) -> None:
        """Append the just-finished turn's elapsed time to chat history.

        Called when the loop returns to ask for the next input, at which
        point the previous turn's messages are already appended/persisted.
        """
        started = self._turn_started_at
        pending = self._turn_record_pending
        self._turn_started_at = None
        self._turn_record_pending = False
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
        """Replacement for ``agent._get_user_input_with_history``."""
        self._record_turn_elapsed()
        self._busy.clear()
        self.broadcaster.publish("idle", {"state": _build_state(self.agent)})
        text = self._input_queue.get()
        if text is None:
            # Shutdown sentinel: ask the loop to exit cleanly.
            return "/exit"
        self._busy.set()
        self._turn_started_at = time.monotonic()
        # Composer input carries a force-prompt sentinel; strip it from the
        # displayed/broadcast text but keep it on the line the loop consumes.
        forced = str(text).startswith(GUI_FORCE_PROMPT_PREFIX)
        display = str(text)[len(GUI_FORCE_PROMPT_PREFIX):] if forced else str(text)
        # Composer input is always forced to a prompt, so any bare slash line is
        # an internal command the GUI issued (rename, switch, ...). Hide its
        # echo and output from the message list and skip turn bookkeeping.
        is_internal_command = (not forced) and display.lstrip().startswith("/")
        if self._bridge is not None:
            self._bridge.suppressed = is_internal_command
        self._turn_record_pending = not is_internal_command
        if not is_internal_command:
            self.broadcaster.publish("turn_start", {"text": display})
        return text

    def _confirm_provider(self, prompt: str = "") -> str:
        """Replacement for ``agent._suspended_input`` (y/n + elicitation)."""
        cid = secrets.token_hex(8)
        reply: "queue.Queue[str]" = queue.Queue()
        with self._confirms_lock:
            self._confirms[cid] = reply
        self.broadcaster.publish(
            "confirm", {"id": cid, "prompt": strip_ansi(str(prompt or ""))}
        )
        try:
            answer = reply.get()
        finally:
            with self._confirms_lock:
                self._confirms.pop(cid, None)
        return str(answer or "")

    # ----- API surface used by the HTTP handler ---------------------------
    @property
    def token(self) -> str:
        return self._token

    def submit_input(self, text: str, as_prompt: bool = False) -> None:
        line = str(text or "")
        # Composer input is forced to a model prompt: prefix a sentinel the
        # runtime loop strips so "/foo" / "!bar" never run as command/shell.
        if as_prompt and line:
            line = GUI_FORCE_PROMPT_PREFIX + line
        self._input_queue.put(line)

    def answer_confirm(self, cid: str, answer: str) -> bool:
        with self._confirms_lock:
            reply = self._confirms.get(str(cid or ""))
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
        try:
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
        """Silently switch workspace and/or chat (no echo / no history replay).

        Refused while a task is running to avoid racing the agent loop, which
        owns the conversation history during a turn. ``chat_id`` may be empty to
        only switch workspace (its active chat is kept).
        """
        import contextlib

        cid = str(chat_id or "").strip()
        wsid = str(workspace_id or "").strip()
        if self._busy.is_set() or (not cid and not wsid):
            return False
        agent = self.agent
        try:
            if wsid and wsid != str(getattr(agent, "workspace_id", "") or ""):
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
                result = agent._activate_chat(
                    rid, announce=False, clear_screen=False, print_history=False
                )
                if result:
                    return False
        except Exception:
            return False
        self.broadcaster.publish("idle", {"state": _build_state(agent)})
        return True

    def set_theme(self, theme: str) -> bool:
        """Persist the GUI theme preference to config.jsonc."""
        value = str(theme or "").strip().lower()
        if value not in ("light", "dark", "system"):
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
            cfg_data["theme"] = value
            save_config_jsonc(cfg_path, cfg_data)
            cache = getattr(agent, "_resolved_config_data", None)
            if isinstance(cache, dict):
                cache["theme"] = value
        except Exception:
            return False
        return True

    def set_ui_prefs(self, prefs: Dict[str, Any]) -> bool:
        """Persist GUI presentation prefs (pin/archive) to config.jsonc.

        These are GUI-only and never affect the TUI; storing them server-side
        keeps them across restarts even when the webview clears localStorage.
        """
        if not isinstance(prefs, dict):
            return False

        def _str_ids(value: Any) -> List[str]:
            out: List[str] = []
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item:
                        out.append(item[:512])
            return out[:2000]

        normalized = {
            "pinnedWorkspaceIds": _str_ids(prefs.get("pinnedWorkspaceIds")),
            "pinnedChatIds": _str_ids(prefs.get("pinnedChatIds")),
            "archivedChatIds": _str_ids(prefs.get("archivedChatIds")),
        }
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
            cfg_data["guiUiPrefs"] = normalized
            save_config_jsonc(cfg_path, cfg_data)
            cache = getattr(agent, "_resolved_config_data", None)
            if isinstance(cache, dict):
                cache["guiUiPrefs"] = normalized
        except Exception:
            return False
        return True

    def new_chat(self) -> Optional[str]:
        """Silently create and activate a new chat; return its id."""
        if self._busy.is_set():
            return None
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
        self.broadcaster.publish("idle", {"state": _build_state(agent)})
        return cid

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
        self._input_queue.put(None)
        # Unblock any pending confirmation so the loop can drain.
        with self._confirms_lock:
            pending = list(self._confirms.values())
        for reply in pending:
            try:
                reply.put_nowait("n")
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

        # Install agent loop hooks before swapping stdout so output is captured.
        self.agent._get_user_input_with_history = self._input_provider  # type: ignore[assignment]
        self.agent._suspended_input = self._confirm_provider  # type: ignore[assignment]

        bridge = _OutputBridge(self.broadcaster)
        self._bridge = bridge
        # GUI streaming mode: the runtime emits clean append-only deltas and
        # brackets the assistant reply with these hooks so the bridge can tag
        # those writes as "assistant" (vs "output" steps).
        self.agent._gui_plain_stream = True  # type: ignore[attr-defined]
        self.agent._gui_assistant_begin = lambda: bridge.set_tag("assistant")  # type: ignore[attr-defined]
        self.agent._gui_assistant_end = lambda: bridge.set_tag("output")  # type: ignore[attr-defined]
        # The GUI renders its own layout, so disable terminal hard-wrapping and
        # force SGR color emission (stdout is not a TTY here). The bridge keeps
        # the SGR runs so the GUI can color step output like the terminal.
        self.agent._gui_no_wrap = True  # type: ignore[attr-defined]
        os.environ["FORCE_COLOR"] = "1"
        os.environ.pop("NO_COLOR", None)
        prev_stdout, prev_stderr = sys.stdout, sys.stderr
        sys.stdout = bridge
        sys.stderr = bridge

        loop_thread = threading.Thread(
            target=self._run_agent_loop, name="codewood-agent-loop", daemon=True
        )
        loop_thread.start()

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

    def _run_agent_loop(self) -> None:
        try:
            from ..runtime.runtime_loop import run_agent_loop

            run_agent_loop(self.agent)
        except Exception:
            # Best-effort: surface fatal loop errors to subscribers.
            try:
                self.broadcaster.publish("output", {"text": "\n[agent loop terminated]\n"})
            except Exception:
                pass
        finally:
            self.request_shutdown()


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
                app.submit_input(text, as_prompt=bool(body.get("asPrompt")))
                self._send_json(200, {"ok": True})
                return
            if path == "/confirm":
                cid = str(body.get("id") or "")
                answer = str(body.get("answer") or "")[:_MAX_CONFIRM_ANSWER_CHARS]
                ok = app.answer_confirm(cid, answer)
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
            if path == "/set-theme":
                theme = str(body.get("theme") or "")[:16]
                ok = app.set_theme(theme)
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/set-ui-prefs":
                prefs = body.get("prefs")
                ok = app.set_ui_prefs(prefs if isinstance(prefs, dict) else {})
                self._send_json(200 if ok else 400, {"ok": ok})
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
