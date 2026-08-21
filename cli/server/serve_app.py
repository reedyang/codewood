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

import base64
import contextlib
import datetime
import io
import json
import logging
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

from ..ai.ai_provider_clients import _is_internal_response_format_error
from ..core.console_utils import (
    GUI_CMD_OUTPUT_BEGIN,
    GUI_CMD_PROMPT_BEGIN,
    GUI_DIFF_BEGIN,
    GUI_FORCE_PROMPT_PREFIX,
)
from ..config.app_info import get_app_logger_root, get_app_slug_snake
from ..config.app_info import get_app_global_config_dir
from ..services.session_memory_service import (
    CONTEXT_COMPACTION_SUMMARY_PREFIX,
    _assistant_display_view,
)

_MCP_LOGGER_NAME = f"{get_app_slug_snake()}.mcp"
_WORKSPACE_ROUTE_LOGGER = logging.getLogger(
    f"{get_app_logger_root()}.workspace_routing"
)
_SSE_LOGGER = logging.getLogger(
    f"{get_app_logger_root()}.sse"
)

# Matches CSI / SGR and most other ANSI escape sequences.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")

# Upper bounds to reject oversized/abusive payloads (input guarding).
_MAX_INPUT_CHARS = 200_000
_MAX_CONFIRM_ANSWER_CHARS = 4096
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


def _strip_bom(text: str) -> str:
    """Strip UTF-8 BOM prefix (``\ufeff``) for safe content comparison."""
    if isinstance(text, str) and text.startswith("\ufeff"):
        return text[1:]
    return text


def _format_console_read(r: Dict[str, Any]) -> Dict[str, Any]:
    """Render a ``ConsoleSession.read_lines`` result into a compact text summary.

    Returns the original dict plus a human-readable ``output`` field used as
    the model-facing text for ``console_read`` / ``console_wait``.
    """
    out_lines = list(r.get("lines") or [])
    pending = str(r.get("pending") or "")
    total_lines = r.get("totalLines", 0)
    truncated = r.get("truncated", False)
    timed_out = r.get("timedOut", False)
    if truncated:
        out_lines.insert(0, "(truncated: oldest lines dropped)")
    if out_lines:
        output_text = "\n".join(out_lines)
    elif pending:
        output_text = f"[partial] {pending}"
    else:
        output_text = "(no output yet)"
    if pending and out_lines:
        output_text += f"\n[pending] {pending}"
    if total_lines:
        clip_start = r.get("start", 0)
        clip_end = clip_start + len(out_lines) - (1 if truncated else 0)
        output_text += f"\n(totalLines: {total_lines}, showing lines {clip_start}-{clip_end})"
    else:
        output_text += "\n(totalLines: 0)"
    if timed_out:
        output_text += " [timed out waiting for new output]"
    # Cap output at 12 000 characters to avoid blowing the context window.
    if len(output_text) > 12000:
        output_text = output_text[:11980] + "\n... (output truncated at 12000 chars) ...\n"
    r["output"] = output_text
    return r


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


def _read_workspace_chat_index(ws_id: str) -> List[Dict[str, Any]]:
    """Read chat summaries from a workspace's on-disk chat index.

    Returns ``[{id, name, updatedAt}]`` (possibly empty). The path is derived
    from trusted agent state, not from any client input.
    """
    from ..config.app_info import get_app_global_config_dir

    out: List[Dict[str, Any]] = []
    try:
        index_path = get_app_global_config_dir() / "chats" / f"{ws_id}.json"
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
                "archived": bool(c.get("archived", False)),
                "hasUnread": bool(c.get("has_unread", False)),
                "model": _chat_model_selector(c),
                "reasoning": str(c.get("reasoning_level") or ""),
            }
        )
    return out


def _chat_model_selector(chat: Any) -> str:
    """Return the ``provider/model_name`` selector recorded on a chat record,
    or ``""`` when the chat has no usable model. Shared by the workspace chat
    summaries so the GUI can prefill a fresh New Chat with the model its chat
    will inherit."""
    try:
        provider = str(chat.get("model_provider") or "").strip()
        model_name = str(chat.get("model_name") or "").strip()
    except Exception:
        return ""
    return f"{provider}/{model_name}" if provider and model_name else ""


# ---- File-change payload sizing ----------------------------------------
# A chat's ``file_changes.json`` sidecar can reach tens of MB (every diff row
# of every apply_patch ever made). Bundling the full merged content into the
# GUI ``state`` snapshot was the dominant cost of opening *any* chat in a
# workspace with many chats: every ``_build_state`` re-merged every chat's
# sidecar and shipped a state event many MB large, which the frontend had to
# JSON-parse and re-render on every open. The GUI only ever renders these
# diffs lazily per turn, so we keep the sidecar intact on disk (undo/reapply
# still read the full store) but cap what gets serialized to the frontend.
_FC_MAX_FILES = 300
_FC_MAX_PATCH_ROWS_PER_FILE = 400
_FC_TRUNCATED_KEY = "truncated"


def _cap_patch_rows(
    rows: List[Dict[str, Any]],
    max_rows: int,
) -> List[Dict[str, Any]]:
    """Cap a file's ``patch`` row list to *max_rows* without losing changes.

    ``FileChangeTracker.get_summary`` builds full-file diffs (every
    unchanged line becomes a ``context`` row), so a naive head-slice can
    cut off the actual add/del rows whenever the edit sits past the cap —
    the on-demand diff viewer then shows no modifications at all.  Keep
    every change row and fill the remaining budget with the context rows
    nearest to a change, so the rendered diff still frames the edits.
    """
    if len(rows) <= max_rows:
        return rows

    change_kinds = {"add", "del", "change"}
    change_idx = [
        i
        for i, r in enumerate(rows)
        if isinstance(r, dict) and r.get("type") in change_kinds
    ]
    if not change_idx:
        return rows[:max_rows]
    if len(change_idx) >= max_rows:
        return [rows[i] for i in change_idx[:max_rows]]

    keep = set(change_idx)
    budget = max_rows - len(change_idx)

    # Distance from each row index to the nearest change row.
    n = len(rows)
    dist = [n] * n
    last = -(10**9)
    for i in range(n):
        if i in keep:
            last = i
        dist[i] = i - last
    last = 10**9
    for i in range(n - 1, -1, -1):
        if i in keep:
            last = i
        dist[i] = min(dist[i], last - i)

    # Prefer context rows closest to a change (ties broken by index) so the
    # surviving rows frame every hunk instead of only the file's head.
    candidates = [i for i in range(n) if i not in keep]
    candidates.sort(key=lambda i: (dist[i], i))
    for i in candidates:
        if budget <= 0:
            break
        keep.add(i)
        budget -= 1

    return [rows[i] for i in sorted(keep)]


def _truncate_file_changes(payload: Any) -> Any:
    """Cap the diff rows/files in a (merged) file-change summary.

    Accepts a single summary dict or a list of summaries (the shape the
    ``state``/history endpoints use). Totals (``totalFiles``/``totalAdded``/
    ``totalDeleted`` and per-file line counts) are computed over the FULL
    change set and kept accurate; only the ``patch`` row arrays (used for the
    on-demand diff viewer) and the ``files`` list are trimmed, and a
    ``truncated`` flag is set so clients know a summary was capped.
    """
    summaries = payload if isinstance(payload, list) else [payload]
    truncated = False
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        files = summary.get("files")
        if not isinstance(files, list):
            continue
        if len(files) > _FC_MAX_FILES:
            truncated = True
            summary["files"] = files[:_FC_MAX_FILES]
        for f in summary["files"]:
            if not isinstance(f, dict):
                continue
            patch = f.get("patch")
            if isinstance(patch, list) and len(patch) > _FC_MAX_PATCH_ROWS_PER_FILE:
                truncated = True
                f["patch"] = _cap_patch_rows(patch, _FC_MAX_PATCH_ROWS_PER_FILE)
    if truncated:
        for summary in summaries:
            if isinstance(summary, dict):
                summary[_FC_TRUNCATED_KEY] = True
    return payload


def _load_file_changes_store(agent: Any, scope_key: str, data_dir: Any) -> Dict[str, Any]:
    """Load a chat's ``file_changes.json`` sidecar into a ref-keyed store dict.

    Handles the new (dict keyed by hashcode ref) and legacy (list or single
    summary) on-disk formats. Returns ``{}`` when the sidecar is missing or
    unreadable so callers can cache the "nothing" result and avoid re-reading.
    """
    store: Dict[str, Any] = {}
    if data_dir is None:
        return store
    path = Path(str(data_dir)) / "file_changes.json"
    if not path.exists() or not path.is_file():
        return store
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return store
    if isinstance(raw, dict):
        first_val = next(iter(raw.values()), None)
        if isinstance(first_val, dict) and "ref" in first_val:
            store = raw
        elif raw.get("totalFiles", 0) > 0:
            store["_legacy"] = raw
    elif isinstance(raw, list):
        store["_legacy"] = raw
    if store and agent is not None:
        try:
            fc_map = dict(getattr(agent, "_file_changes_by_chat", {}) or {})
            fc_map[scope_key] = store
            setattr(agent, "_file_changes_by_chat", fc_map)
        except Exception:
            pass
    return store


def _ensure_scope_file_changes_loaded(agent: Any, scope_key: str) -> None:
    """Best-effort disk fallback for the structured-turn builder.

    ``scope_key`` is the workspace-qualified composite (``ws::chat`` or bare
    ``chat``) that keys the in-memory sidecar store. If it is not cached yet,
    resolve the chat's side-data dir and load its ``file_changes.json`` so
    ``[FILE_CHANGE_REF]`` markers resolve even on paths that skipped the
    explicit ``_ensure_chat_file_changes_loaded`` warm-up.
    """
    if not scope_key:
        return
    try:
        fc_map = dict(getattr(agent, "_file_changes_by_chat", {}) or {})
        if scope_key in fc_map:
            return
    except Exception:
        return
    try:
        mgr = getattr(agent, "_chat_state_manager", None)
        if mgr is None:
            return
        cid = scope_key.split("::", 1)[1] if "::" in scope_key else scope_key
        data_dir = mgr.chat_data_dir_for_chat(cid)
    except Exception:
        return
    if data_dir is None:
        return
    _load_file_changes_store(agent, scope_key, data_dir)


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
    from ..core.text_output_renderer import (
        format_assistant_display_response,
        format_assistant_display_response_plain,
    )

    hist = list(getattr(agent, "conversation_history", None) or [])
    hist = [msg for msg in hist if not (msg.get("_internal") and str(msg.get("content", "") or "").startswith("[Key constraints]"))]
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
        rnd = {
            "waitSeconds": max(0, int(round(wait_seconds))),
            "text": "",
            "tools": "",
            "thinking": "",
        }
        turn["rounds"].append(rnd)
        return rnd

    def _extract_thinking(msg: Dict[str, Any], rnd: Dict[str, Any]) -> None:
        """Copy the ``_thinking`` field from *msg* into *rnd* if present,
        without overwriting an existing thinking value (first message wins
        within the same round)."""
        thinking_text = str(msg.get("_thinking") or "").strip()
        if thinking_text and not rnd.get("thinking"):
            rnd["thinking"] = thinking_text

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
                if sms.parse_context_compaction_summary_content(content) is not None:
                    return False
            if agent._parse_conversation_interrupted_history_content(content) is not None:
                return False
            if agent._parse_model_call_error_history_content(content) is not None:
                return False
            if agent._parse_direct_shell_result_history_content(content) is not None:
                return False
            if agent._parse_task_worked_summary_history_content(content) is not None:
                return False
            if agent._parse_model_tool_plan_history_content(content) is not None:
                return False
        except Exception:
            return False
        try:
            return bool(format_assistant_display_response(content))
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

    def _format_history_answer_text(source_text: Any) -> str:
        """Format assistant reply text for persisted GUI history."""
        try:
            formatted = format_assistant_display_response_plain(str(source_text or "")) or ""
        except Exception:
            formatted = ""
        return (
            strip_ansi(str(formatted))
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .strip("\n")
        )

    def _is_meaningless_answer_fragment(text: str) -> bool:
        """True for cleanup residue that should not replace the real answer."""
        stripped = str(text or "").strip()
        if not stripped:
            return True
        return bool(re.fullmatch(r"[\\/]+", stripped))

    for idx, msg in enumerate(hist):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip().lower()
        if role == "assistant":
            # Flatten model-reply blocks (_reply_records) into the display
            # message (split raw node skipped; nodes render in order).
            msg = _assistant_display_view(msg)
        content = str(msg.get("content") or "")
        clean_content = content
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
            # Check for [FILE_CHANGE_REF:<hashcode>] internal messages that
            # carry file-change references for the preceding turn.
            if content.startswith("[FILE_CHANGE_REF:"):
                _ref = content[len("[FILE_CHANGE_REF:"):].rstrip("]")
                if _ref:
                    # The bound session key is already the workspace-qualified
                    # composite (``workspace_id::chat_id``) that the sidecar store
                    # is keyed by, so a same-id chat in another workspace can't
                    # pull the wrong file-changes record.
                    try:
                        _scope_key = str(agent._current_session_chat_key() or "")
                    except Exception:
                        _scope_key = str(getattr(agent, "active_chat_id", "") or "")
                    _ensure_scope_file_changes_loaded(agent, _scope_key)
                    _fc_store = getattr(agent, "_file_changes_by_chat", {}) or {}
                    if not isinstance(_fc_store, dict):
                        _fc_store = {}
                    _chat_store = _fc_store.get(_scope_key, {})
                    if isinstance(_chat_store, dict):
                        _fc = _chat_store.get(_ref)
                        if isinstance(_fc, dict) and _fc.get("totalFiles", 0) > 0:
                            if current:
                                current["fileChanges"] = _truncate_file_changes(dict(_fc))
            continue
        if role == "assistant":
            # A recorded request_user_input selection: render it as a left-side
            # "selection" bubble (a reply to the agent's question, distinct
            # from a user-initiated right-side turn).
            try:
                ami_answer = agent._parse_request_user_input_answer_history_content(content)
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
                    # Make the answer a hard boundary.  Otherwise the next
                    # tool plan keeps appending to the earlier Ask round,
                    # while this selection remains after it in the round
                    # list and is rendered below that next tool call.
                    current_round = sel_round
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
                if sms is not None:
                    compact_summary = sms.parse_context_compaction_summary_content(content)
                    if compact_summary is not None:
                        compact_display = None
                        try:
                            formatter = getattr(sms, "build_context_compaction_display_payload", None)
                            if callable(formatter):
                                compact_display = formatter(compact_summary)
                        except Exception:
                            compact_display = None
                        if not isinstance(compact_display, dict):
                            body = str(compact_summary.get("summary") or "").strip()
                            title = "Context compacted"
                            compact_display = {
                                "title": title,
                                "body": body,
                                "text": title if not body else f"{title}\n\n{body}",
                            }
                        turns.append(
                            {
                                "userText": "",
                                "timestamp": str(msg.get("created_at") or ""),
                                "rounds": [
                                    {
                                        "waitSeconds": 0,
                                        "text": "",
                                        "tools": "",
                                        "selection": "",
                                        "thinking": "",
                                        # When the compaction summary IS the
                                        # chat's first message (e.g. a chat
                                        # seeded from a summary), drop the
                                        # "Context compacted" banner line —
                                        # there is no prior context to have
                                        # been compacted, so the banner would
                                        # be meaningless.
                                        "compactNoticeTitle": (
                                            ""
                                            if idx == 0
                                            else str(compact_display.get("title") or "")
                                        ),
                                        "compactNoticeBody": str(compact_display.get("body") or ""),
                                    }
                                ],
                            }
                        )
                        current = None
                        current_round = None
                        continue
            except Exception:
                pass
            # The per-turn worked summary is superseded by per-round timers.
            try:
                if agent._parse_task_worked_summary_history_content(content) is not None:
                    continue
            except Exception:
                pass
            # The "task interrupted" banner is TUI-only: the GUI intentionally
            # does not render an Interrupted message after a task is cancelled,
            # so the marker is dropped from the structured turn payload while
            # staying in history for the TUI replay.
            try:
                if agent._parse_conversation_interrupted_history_content(content) is not None:
                    continue
            except Exception:
                pass
            # Parser/shape diagnostics belong in logs only; drop them from
            # the GUI turn payload so they never render as a red banner.
            try:
                model_error_payload = agent._parse_model_call_error_history_content(content)
                if model_error_payload is not None:
                    error_message = str(model_error_payload.get("error_message") or "").strip()
                    if _is_internal_response_format_error(error_message):
                        continue
            except Exception:
                pass

        # New-format role:tool messages.
        if role == "tool":
            turn = _ensure_turn()
            if current_round is None:
                current_round = _new_round(turn, 0)
            if ts is not None and prev_ts is not None:
                current_round["waitSeconds"] += max(0, int(round(ts - prev_ts)))
            # Emit apply_patch diff block from previews.json sidecar
            # unless diffs are already inline in the tool round text.
            if not current_round.get("_has_inline_diffs"):
                try:
                    if str(msg.get("name") or "").strip().lower() == "apply_patch":
                        _parsed = json.loads(str(msg.get("content") or "{}"))
                        if isinstance(_parsed, dict) and bool(_parsed.get("success", True)):
                            _buf = io.StringIO()
                            with contextlib.redirect_stdout(_buf):
                                agent._replay_apply_patch_gui_diff_block(_parsed)
                            _diff = _buf.getvalue().strip()
                            if _diff:
                                current_round["tools"] = current_round["tools"] + _diff + "\n"
                except Exception:
                    pass
            rendered = _render_step(idx, msg)
            if rendered.strip():
                current_round["tools"] = current_round["tools"] + rendered + "\n"
            if ts is not None:
                prev_ts = ts
            continue

        # A pure tool-call model message (no natural-language reply) still
        # represents a distinct model pass. Merge it into the previous tool
        # round when that round already has tools, so the GUI shows one
        # collapsible "Called N tools" group instead of separate groups.
        # Messages that carry their own _thinking start a new round so each
        # "Thought for" block maps to its own tool-call group.
        if _is_tool_plan(content):
            turn = _ensure_turn()
            wait = (ts - prev_ts) if (ts is not None and prev_ts is not None) else 0
            has_own_thinking = bool(str(msg.get("_thinking") or "").strip()) if isinstance(msg, dict) else False
            if current_round is None or not current_round.get("tools", "").strip() or has_own_thinking:
                current_round = _new_round(turn, wait)
            else:
                current_round["waitSeconds"] += max(0, int(round(wait)))
            # Prefer the pre-rendered tool rounds (which already include the
            # full call plus any payload, e.g. a read's file content, so the GUI
            # can syntax-highlight it). Only fall back to re-rendering the plan
            # directly when no pre-rendered rounds exist. Rendering BOTH would
            # duplicate the call: the direct render shows only the prompt line
            # (no payload, no highlight) while the pre-rendered one shows the
            # complete call.
            raw_rounds = msg.get("_tool_rounds_raw") if isinstance(msg, dict) else None
            if isinstance(raw_rounds, list) and raw_rounds:
                try:
                    tool_rounds = agent._rerender_tool_rounds(raw_rounds)
                except Exception:
                    tool_rounds = msg.get("tool_rounds") if isinstance(msg, dict) else None
                if isinstance(raw_rounds, list) and any(
                    isinstance(r, dict) and r.get("previewRef") for r in raw_rounds
                ):
                    current_round["_has_inline_diffs"] = True
            else:
                tool_rounds = msg.get("tool_rounds") if isinstance(msg, dict) else None
            if not (isinstance(tool_rounds, list) and tool_rounds):
                # No pre-rendered rounds: render the plan directly. The matching
                # tool-result message normally renders the "Ran <tool>" feedback
                # line, so only render here when the per-message renderer
                # recognizes the plan (stays silent otherwise); for a blob the
                # strict parser misses, skip rendering entirely rather than
                # letting the raw JSON leak as text.
                tool_plan = None
                try:
                    tool_plan = agent._parse_model_tool_plan_history_content(content)
                except Exception:
                    tool_plan = None
                recognized_plan = tool_plan is not None
                if recognized_plan and str((tool_plan.get("tool") or "")).strip().lower() == "request_skill_prompt":
                    recognized_plan = False
                if recognized_plan:
                    rendered = _render_step(idx, msg)
                    if rendered.strip():
                        current_round["tools"] = current_round["tools"] + rendered + "\n"
            if isinstance(tool_rounds, list) and tool_rounds:
                current_round["tools"] = current_round["tools"] + "\n".join(tool_rounds) + "\n"
            # Extract _thinking even when the assistant message is a pure
            # tool-call plan (no natural-language reply). The thinking panel
            # must survive a chat reload.
            _extract_thinking(msg, current_round)
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
            # GUI history must preserve the raw ``<proposed_plan>`` block so
            # the Markdown card and the plan chooser re-appear after a
            # restart; the terminal-oriented formatter reframes/strips those
            # tags, so use the GUI-plain variant here.
            answer_text = _format_history_answer_text(clean_content)
            if _is_meaningless_answer_fragment(answer_text):
                raw_answer_text = _format_history_answer_text(content)
                if not _is_meaningless_answer_fragment(raw_answer_text):
                    answer_text = raw_answer_text
                else:
                    answer_text = ""
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
            thinking_text = str(msg.get("_thinking") or "").strip()
            if thinking_text:
                current_round["thinking"] = thinking_text
            # Emit tool_rounds when the assistant message carries both text and tools
            raw_rounds = msg.get("_tool_rounds_raw") if isinstance(msg, dict) else None
            if isinstance(raw_rounds, list) and raw_rounds:
                try:
                    tool_rounds = agent._rerender_tool_rounds(raw_rounds)
                except Exception:
                    tool_rounds = msg.get("tool_rounds") if isinstance(msg, dict) else None
                # If any raw round has a previewRef, diffs are already
                # inline in the tool round text — skip separate emission.
                if isinstance(raw_rounds, list) and any(
                    isinstance(r, dict) and r.get("previewRef") for r in raw_rounds
                ):
                    current_round["_has_inline_diffs"] = True
            else:
                tool_rounds = msg.get("tool_rounds") if isinstance(msg, dict) else None
            if isinstance(tool_rounds, list) and tool_rounds:
                current_round["tools"] = "\n".join(tool_rounds) + "\n"
        else:
            rendered = _render_step(idx, msg)
            # Model-call-error banners are rendered as a separate field so the
            # frontend can display them outside the collapsible "Worked for"
            # section — they are status messages, not tool steps.
            model_error_payload = agent._parse_model_call_error_history_content(content)
            if model_error_payload is not None:
                error_message = str((model_error_payload or {}).get("error_message") or "").strip()
                if not error_message:
                    error_message = rendered.strip()
                if _is_internal_response_format_error(error_message):
                    if ts is not None:
                        prev_ts = ts
                    continue
                if current_round is None or current_round.get("text") or current_round.get("modelError"):
                    current_round = _new_round(turn, wait)
                current_round["modelError"] = error_message
                # Also record the rendered banner so CLI keeps seeing it.
                if rendered.strip():
                    current_round["interrupted"] = strip_ansi(rendered)
            else:
                has_own_thinking = bool(str(msg.get("_thinking") or "").strip()) if isinstance(msg, dict) else False
                if current_round is None or current_round.get("text") or has_own_thinking:
                    current_round = _new_round(turn, wait)
                else:
                    current_round["waitSeconds"] += max(0, int(round(wait)))
                # Emit tool_rounds for new-format assistant messages
                raw_rounds = msg.get("_tool_rounds_raw") if isinstance(msg, dict) else None
                if isinstance(raw_rounds, list) and raw_rounds:
                    try:
                        tool_rounds = agent._rerender_tool_rounds(raw_rounds)
                    except Exception:
                        tool_rounds = msg.get("tool_rounds") if isinstance(msg, dict) else None
                    if isinstance(raw_rounds, list) and any(
                        isinstance(r, dict) and r.get("previewRef") for r in raw_rounds
                    ):
                        current_round["_has_inline_diffs"] = True
                else:
                    tool_rounds = msg.get("tool_rounds") if isinstance(msg, dict) else None
                if isinstance(tool_rounds, list) and tool_rounds:
                    current_round["tools"] = current_round["tools"] + "\n".join(tool_rounds) + "\n"
                elif rendered.strip():
                    current_round["tools"] = current_round["tools"] + rendered + "\n"
                _extract_thinking(msg, current_round)
        if ts is not None:
            prev_ts = ts

    for turn in turns:
        rounds = [
            {
                "waitSeconds": int(r.get("waitSeconds") or 0),
                "text": str(r.get("text") or "").rstrip("\n"),
                "tools": str(r.get("tools") or "").rstrip("\n"),
                "selection": str(r.get("selection") or "").strip(),
                "thinking": str(r.get("thinking") or "").strip(),
                "compactNoticeTitle": str(r.get("compactNoticeTitle") or "").strip(),
                "compactNoticeBody": str(r.get("compactNoticeBody") or "").strip(),
                "interrupted": str(r.get("interrupted") or "").strip(),
                "modelError": str(r.get("modelError") or "").strip(),
            }
            for r in turn.get("rounds", [])
        ]
        # Drop rounds that produced nothing renderable (e.g. an empty model
        # response) so we don't show a stray timer with no content. A round
        # carrying an request_user_input selection is always renderable.
        turn["rounds"] = [
            r
            for r in rounds
            if (
                r["text"].strip()
                or r["tools"].strip()
                or r["selection"].strip()
                or r["thinking"].strip()
                or r["compactNoticeTitle"].strip()
                or r["compactNoticeBody"].strip()
                or r["interrupted"].strip()
                or r["modelError"].strip()
            )
        ]
    # Attach per-turn file-change summaries from the sidecar.
    # New format: hashcode-ref dict loaded via [FILE_CHANGE_REF] messages
    # (already handled above).  Legacy fallback: turnIndex-based list.
    try:
        # The bound session key is already the workspace-qualified composite
        # (``workspace_id::chat_id``) the sidecar store is keyed by, so a
        # same-id chat in another workspace reads its own in-memory record.
        try:
            _scope_key = str(agent._current_session_chat_key() or "")
        except Exception:
            _scope_key = str(getattr(agent, "active_chat_id", "") or "")
        if _scope_key:
            _fc_by_chat = getattr(agent, "_file_changes_by_chat", {}) or {}
            _fc_store = _fc_by_chat.get(_scope_key, {})
            if not _fc_store:
                _mgr = getattr(agent, "_chat_state_manager", None)
                if _mgr is not None:
                    # Disk fallback (legacy back-compat; the new-format refs are
                    # handled via the in-memory cache above).
                    _disk = _mgr.load_file_changes(str(_scope_key.split("::", 1)[-1] or ""))
                    if isinstance(_disk, list):
                        # Legacy turnIndex format: attach by position
                        for i, turn in enumerate(turns):
                            _fc = _disk[i] if i < len(_disk) else None
                            if isinstance(_fc, dict) and _fc.get("totalFiles", 0) > 0:
                                turn["fileChanges"] = _truncate_file_changes(dict(_fc))
                    elif isinstance(_disk, dict):
                        _fc_store = _disk
            if isinstance(_fc_store, dict):
                for turn in turns:
                    if turn.get("fileChanges") is None:
                        # Try to match by turnIndex (legacy) or ref
                        _fc = next(
                            (v for v in _fc_store.values()
                             if isinstance(v, dict) and v.get("turnIndex") == turn.get("_turnIndex")),
                            None
                        )
                        if _fc and isinstance(_fc, dict) and _fc.get("totalFiles", 0) > 0:
                            turn["fileChanges"] = _truncate_file_changes(dict(_fc))
    except Exception:
        pass
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

    def __init__(
        self,
        broadcaster: _Broadcaster,
        chat_id_getter: Any = None,
        workspace_id_getter: Any = None,
    ) -> None:
        self._broadcaster = broadcaster
        # Returns the chat id that output is currently attributed to, so the
        # GUI can route streamed text to the correct chat when several chats
        # run concurrently. Falls back to "" when unknown.
        self._chat_id_getter = chat_id_getter
        # Returns the workspace id the output belongs to. Chat ids repeat
        # across workspaces, so the GUI gates per-chat events by workspace to
        # avoid a background chat's stream landing in a same-id chat the user
        # has since focused in another workspace.
        self._workspace_id_getter = workspace_id_getter
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

    def _workspace_id(self) -> str:
        getter = self._workspace_id_getter
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

    def write_tagged(self, tag: str, s: Any) -> int:
        if s is None:
            return 0
        text = s if isinstance(s, str) else str(s)
        if not text:
            return 0
        if bool(getattr(self._tls, "suppressed", False)):
            return len(text)
        cleaned = strip_ansi_keep_sgr(text).replace("\r\n", "\n")
        if cleaned:
            self._broadcaster.publish(
                str(tag or "output"),
                {
                    "text": cleaned,
                    "chatId": self._chat_id(),
                    "workspaceId": self._workspace_id(),
                },
            )
        return len(text)

    def write(self, s: Any) -> int:  # type: ignore[override]
        # Keep SGR color runs (so the GUI can theme step output like the
        # terminal) but drop cursor/erase control sequences a non-TTY SSE
        # sink cannot honor. Normalize carriage returns to plain newlines.
        text = s if isinstance(s, str) else str(s or "")
        # Tool envelopes are protocol-level output, even if an interrupted or
        # exceptional model stream left this thread's assistant tag set.  If
        # they were published as ``assistant`` events, the frontend would skip
        # StepsView and expose the raw terminal-formatted rows indefinitely.
        tag = str(getattr(self._tls, "tag", "output") or "output")
        if any(marker in text for marker in (
            GUI_CMD_PROMPT_BEGIN,
            GUI_CMD_OUTPUT_BEGIN,
            GUI_DIFF_BEGIN,
        )):
            tag = "output"
        return self.write_tagged(tag, text)

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


def _safe_pending_request_user_input(agent: Any) -> Optional[Dict[str, Any]]:
    """Return the active chat's pending ``request_user_input`` request, if any.

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
            payload = agent._peek_pending_request_user_input()
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

    Scan the chat's message list directly from its disk record so the plan
    panel renders correctly on initial load (no session binding yet) and after
    focus changes — no dependency on ``_session_scope`` or in-memory state.
    """
    items = []
    explanation = ""
    try:
        cid = _primary_active_chat_id(agent)
        if cid:
            chat = agent._chat_state_manager.find_chat_by_id(cid)
            if chat:
                messages = chat.get("messages")
                if isinstance(messages, list):
                    from ..managers.chat_state_manager import _normalize_plan_items
                    for msg in reversed(messages):
                        if not isinstance(msg, dict):
                            continue
                        raw_plan = msg.get("plan")
                        items = _normalize_plan_items(raw_plan)
                        if items:
                            explanation = str(msg.get("plan_explanation") or "")
                            break
                        if msg.get("role") == "user" and not msg.get("_internal"):
                            break
    except Exception:
        pass
    steps = []
    for item in items:
        if not isinstance(item, dict):
            continue
        steps.append({"step": str(item.get("step") or ""), "status": str(item.get("status") or "")})
    return {"plan": steps, "explanation": explanation}


def _stats_source_for_active_chat(agent: Any) -> tuple:
    """Return ``(agent_key, hist)`` for the GUI's FOCUSED (active) chat.

    The dashboard must describe the chat the user is currently viewing, not the
    agent's ambient session. After a focus switch onto a chat whose agent loop
    is still running (busy), ``select_chat`` only moves the focus pointer — it
    does NOT rebind the calling thread's session, so ``agent.conversation_history``
    (and the shared ``provider``/``model_name`` globals) still belong to the
    previously activated chat. Aggregating from those would surface another
    chat's model + numbers on the focused chat's dashboard.

    The authoritative per-chat model and history live on the active chat's
    persisted record. The in-memory ``conversation_history`` is preferred only
    when the calling thread is bound to that exact chat (e.g. the focused chat's
    own loop emitting a ``round_end``), so live in-progress round stats are kept.
    """
    try:
        cid = _primary_active_chat_id(agent)
        if not cid:
            return "", []
        chat = agent._find_chat_by_id(cid)
        if not isinstance(chat, dict):
            return "", []
        provider = str(chat.get("model_provider") or "").strip()
        model_name = str(chat.get("model_name") or "").strip()
        agent_key = f"{provider}/{model_name}" if provider and model_name else ""
        hist = list(chat.get("messages") or [])
        try:
            sess_key = str(agent._current_session_chat_key() or "")
            active_key = str(agent._session_registry_key(cid) or "")
            if sess_key and sess_key == active_key:
                live = list(getattr(agent, "conversation_history", None) or [])
                if live:
                    hist = live
        except Exception:
            pass
        return agent_key, hist
    except Exception:
        return "", []


def _compute_chat_cache_stats(agent: Any) -> Dict[str, Any]:
    """Aggregate cache-hit statistics for the active chat's current model.

    Scans the focused chat's messages (from its persisted record, or its live
    session when the calling thread is bound to it) for messages sent with the
    chat's recorded model, summing recorded prompt_cache_hit_tokens and
    prompt_cache_miss_tokens. When the API only provides root-level
    input_tokens (no cache breakdown), those are accumulated as totalTokens
    with hasBreakdown=False.
    """
    agent_key, hist = _stats_source_for_active_chat(agent)
    model_name = agent_key.rsplit("/", 1)[-1] if "/" in agent_key else agent_key
    result: Dict[str, Any] = {
        "totalTokens": 0,
        "hitTokens": 0,
        "missTokens": 0,
        "hitRate": 0.0,
        "model": model_name,
        "supported": False,
        "hasBreakdown": True,
    }
    if not agent_key:
        return result

    total_hit = 0
    total_miss = 0
    for msg in hist:
        if not isinstance(msg, dict):
            continue
        msg_model = str(msg.get("_model") or "").strip()
        if msg_model != agent_key:
            continue
        cs = msg.get("_cache_stats")
        if isinstance(cs, dict):
            result["supported"] = True
            if "input_tokens" in cs:
                result["hasBreakdown"] = False
                total_miss += int(cs["input_tokens"] or 0)
            else:
                total_hit += int(cs.get("prompt_cache_hit_tokens") or 0)
                total_miss += int(cs.get("prompt_cache_miss_tokens") or 0)
    total = total_hit + total_miss
    if total > 0:
        result["totalTokens"] = total
        result["hitTokens"] = total_hit
        result["missTokens"] = total_miss
        result["hitRate"] = round(total_hit * 100.0 / max(1, total), 1)
    return result


def _compute_chat_token_stats(agent: Any) -> Dict[str, Any]:
    """Aggregate output/reasoning token stats for the active chat's current model.

    Scans the focused chat's messages (from its persisted record, or its live
    session when the calling thread is bound to it) for ``_output_tokens`` and
    ``_reasoning_tokens`` fields, summing them per model. When
    ``_token_count_includes_reasoning`` is True (e.g. DeepSeek), the effective
    output tokens = ``_output_tokens`` - ``_reasoning_tokens``.
    """
    agent_key, hist = _stats_source_for_active_chat(agent)
    result: Dict[str, Any] = {
        "outputTokens": 0,
        "reasoningTokens": 0,
        "hasOutputTokens": False,
        "hasReasoningTokens": False,
        "includesReasoning": False,
    }
    if not agent_key:
        return result

    total_output = 0
    total_reasoning = 0
    has_output = False
    has_reasoning = False
    includes_reasoning = False

    for msg in hist:
        if not isinstance(msg, dict):
            continue
        msg_model = str(msg.get("_model") or "").strip()
        if msg_model != agent_key:
            continue
        ot = msg.get("_output_tokens")
        if isinstance(ot, int) and ot > 0:
            total_output += ot
            has_output = True
        rt = msg.get("_reasoning_tokens")
        if isinstance(rt, int) and rt > 0:
            total_reasoning += rt
            has_reasoning = True
        if msg.get("_token_count_includes_reasoning") is True:
            includes_reasoning = True

    result["outputTokens"] = total_output
    result["reasoningTokens"] = total_reasoning
    result["hasOutputTokens"] = has_output
    result["hasReasoningTokens"] = has_reasoning
    result["includesReasoning"] = includes_reasoning
    return result


def _compute_context_usage_fresh_from_messages(agent: Any, chat_record: Dict[str, Any]) -> "tuple[int, int, int]":
    """Last-resort context usage when the in-memory snapshot is unavailable.

    Usage is the single source of truth maintained by the runtime refresh in
    ``llm_context_manager`` (it accumulates system prompt + tool schemas +
    history from the latest compaction summary). When ``_last_context_*`` is not
    yet populated, we trigger that refresh synchronously to recompute it, then
    read the now-populated snapshot. This keeps exactly one token-accounting
    implementation instead of a duplicate history-only fallback.
    """
    refresh = getattr(agent, "_refresh_status_context_usage_snapshot", None)
    if callable(refresh):
        try:
            refresh()
        except Exception:
            pass
    total = int(getattr(agent, "_last_context_input_tokens", 0) or 0)
    window = int(getattr(agent, "_last_context_window", 0) or 0)
    pct = int(getattr(agent, "_last_context_usage_percent", 0) or 0)
    if window <= 0:
        from ..core.config.model_providers import DEFAULT_CONTEXT_WINDOW, parse_context_window

        window = parse_context_window(
            (getattr(agent, "params", None) or {}).get("context_window"),
            default_value=DEFAULT_CONTEXT_WINDOW,
        )
    return window, total, pct


def _safe_context_usage(agent: Any, chat_id: str, chat_record: Dict[str, Any]) -> "tuple[int, int, int]":
    """Read/recompute context usage from the active chat's own session."""
    cid = str(chat_id or "").strip()
    if not cid:
        return 0, 0, 0
    try:
        with agent._session_scope(cid):
            total = int(getattr(agent, "_last_context_input_tokens", 0) or 0)
            window = int(getattr(agent, "_last_context_window", 0) or 0)
            pct = int(getattr(agent, "_last_context_usage_percent", 0) or 0)
            if total <= 0 and pct <= 0:
                return _compute_context_usage_fresh_from_messages(agent, chat_record)
            return window, total, pct
    except Exception:
        return _compute_context_usage_fresh_from_messages(agent, chat_record)


def _safe_context_usage_parts(agent: Any) -> List[Dict[str, Any]]:
    """Read the active chat's per-component context-usage breakdown.

    The breakdown is populated by the runtime refresh in ``llm_context_manager``
    (``agent._last_context_parts``); this helper only reads it, binding to the
    primary chat's session so HTTP handler threads see the focused chat's data.
    """
    try:
        with agent._session_scope(_primary_active_chat_id(agent)):
            parts = getattr(agent, "_last_context_parts", None) or []
            if not isinstance(parts, list):
                return []
            out: List[Dict[str, Any]] = []
            for p in parts:
                if isinstance(p, dict) and str(p.get("key") or "").strip():
                    out.append({"key": str(p.get("key") or ""), "tokens": max(0, int(p.get("tokens") or 0))})
            return out
    except Exception:
        return []


def _safe_reasoning_effort(agent: Any) -> str:
    # Reasoning effort is session-scoped; bind to the active chat so HTTP
    # handler threads read the focused chat's saved selection (restored from
    # its chat record on activation), not the ambient/unbound session.
    try:
        with agent._session_scope(_primary_active_chat_id(agent)):
            return str(agent._current_reasoning_effort() or "")
    except Exception:
        return ""


def _safe_reasoning_efforts(agent: Any) -> List[str]:
    try:
        with agent._session_scope(_primary_active_chat_id(agent)):
            return [str(x) for x in (agent._current_model_reasoning_efforts() or []) if str(x)]
    except Exception:
        return []


# A tiny same-origin bridge injected into preview pages so the BrowserPanel can
# read the DOM / captured console / run an expression on OUR OWN preview pages
# (external cross-origin sites carry no such bridge and stay unreadable). The
# bridge buffers console output and answers postMessage requests from the parent
# frame. It never reaches the model directly; the parent relays results to the
# browser tools.
_PREVIEW_BRIDGE_SCRIPT = """
<script>
(function(){
  var __logs = [];
  function cap(kind){
    var orig = console[kind] ? console[kind].bind(console) : function(){};
    console[kind] = function(){
      try {
        var parts = [];
        for (var i=0;i<arguments.length;i++){
          var a = arguments[i];
          parts.push(typeof a === 'object' ? JSON.stringify(a) : String(a));
        }
        __logs.push(kind + ': ' + parts.join(' '));
        if (__logs.length > 500) __logs.shift();
      } catch(e){}
      return orig.apply(console, arguments);
    };
  }
  ['log','info','warn','error','debug'].forEach(cap);
  window.addEventListener('error', function(ev){
    try { __logs.push('error: ' + ev.message); } catch(e){}
  });
  window.addEventListener('message', function(ev){
    var d = ev.data;
    if (!d || d.__codewoodBridge !== true) return;
    var out = { __codewoodBridge: true, nonce: d.nonce, ok: true };
    try {
      if (d.action === 'read_dom') {
        out.dom = document.documentElement ? document.documentElement.outerHTML : '';
      } else if (d.action === 'read_console') {
        out.console = __logs.slice();
      } else if (d.action === 'eval') {
        // Controlled-only: runs solely inside our own sandboxed preview frame.
        out.result = String(eval(String(d.script || '')));
      } else {
        out.ok = false; out.error = 'unknown action';
      }
    } catch(e){ out.ok = false; out.error = String(e); }
    try { (ev.source || window.parent).postMessage(out, '*'); } catch(e){}
  });
})();
</script>
"""


def _wrap_preview_html(html: str) -> str:
    """Inject the preview bridge script into an HTML document so the embedded
    browser can read it. The script is appended before ``</body>`` when present,
    otherwise at the end of the document."""
    body = str(html or "")
    needle = "</body>"
    idx = body.lower().rfind(needle)
    if idx != -1:
        return body[:idx] + _PREVIEW_BRIDGE_SCRIPT + body[idx:]
    return body + _PREVIEW_BRIDGE_SCRIPT


def _build_state(agent: Any, workspace_id: str = "") -> Dict[str, Any]:
    """Serialize a read-only snapshot of agent state for the GUI.

    A background chat's loop thread may build this snapshot (e.g. when it emits
    an ``idle``/``state`` event). That thread carries a per-workspace
    PERSISTENCE override pointing at its OWN workspace; the snapshot, however,
    must describe the FOCUSED workspace (its chat list, active chat, etc.).
    Suspend the override for the whole build so chat reads (``_chat_entries``)
    resolve against the focused global index, not the background workspace's.

    ``workspace_id`` overrides the workspace recorded in the returned snapshot
    (e.g. a background loop thread passes its own workspace so the idle payload
    carries the correct ``workspace.id``, not the user's newly-focused one).
    """
    suspend = getattr(agent, "_suspend_persist_workspace_ctx", None)
    if callable(suspend):
        with suspend():
            return _build_state_inner(agent, workspace_id=workspace_id)
    return _build_state_inner(agent, workspace_id=workspace_id)


def _build_state_inner(agent: Any, workspace_id: str = "") -> Dict[str, Any]:
    from ..config.app_info import get_app_name, get_app_version
    from ..core.localization import get_display_language
    from ..managers.chat_state_manager import _chat_mode_is_plan

    # Use the override if provided; otherwise fall back to agent global.
    # A background runtime keeps its own persistence context after the user
    # focuses another workspace.  In that case the chat entries below resolve
    # to the runtime workspace, while these agent globals already describe the
    # newly focused workspace.  Resolve all workspace metadata from the same
    # explicit id so one SSE snapshot can never combine A's chats with B's
    # name/root (chat ids are only unique *within* a workspace).
    requested_ws_id = str(workspace_id or "").strip()
    _ws_id = requested_ws_id or str(getattr(agent, "workspace_id", "") or "")
    _ws_name = str(getattr(agent, "workspace_name", "") or "")
    _ws_root = str(getattr(agent, "workspace_root", "") or "")
    _ws_work_dir = str(getattr(agent, "work_directory", "") or "")
    if requested_ws_id:
        try:
            raw_workspaces = getattr(agent, "_workspaces_state", {}).get("workspaces", {})
            entry = raw_workspaces.get(requested_ws_id) if isinstance(raw_workspaces, dict) else None
            if isinstance(entry, dict):
                _ws_name = str(entry.get("name") or _ws_name)
                try:
                    _ws_root = str(agent._workspace_root_path(entry))
                except Exception:
                    _ws_root = str(entry.get("root") or _ws_root)
                try:
                    current_dir = agent._workspace_current_dir_path(entry)
                    _ws_work_dir = str(current_dir or _ws_root)
                except Exception:
                    _ws_work_dir = _ws_root
        except Exception:
            pass

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
        active_ws_id = _ws_id
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
                        "archived": bool(entry.get("archived", False)),
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
            chat_model = f"{prov}/{name}" if prov and name else ""
            cid = str(c.get("id") or "")
            if cid == active_chat_id:
                active_chat_model = chat_model
                # Pull the focused chat's context-usage snapshot so the GUI can
                # surface it next to the model selector without a separate
                # request-per-tick. These numbers are kept in sync by the
                # session manager every time the model returns input-token
                # accounting.
                # Context usage is no longer persisted on the chat record; the
                # authoritative value is the in-memory snapshot rebuilt from the
                # message history on every activate/refresh. Read it directly,
                # and fall back to a fresh history-based recompute only when the
                # snapshot is not yet available.
                active_context_window, active_context_tokens, active_context_percent = _safe_context_usage(
                    agent,
                    cid,
                    c,
                )
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
                    # Reasoning-effort level recorded on the chat ("" when none).
                    "reasoning": str(c.get("reasoning_level") or ""),
                    # True while this chat's agent loop is mid-turn, so the
                    # sidebar busy dot survives focus changes and reloads.
                    "running": cid in running_chat_ids,
                    # Sticky Plan-mode flag recorded on the chat record root
                    # (as ``mode: "plan"|"agent"``). Surfaced so the GUI can
                    # restore the per-chat compose mode after a restart instead
                    # of defaulting every chat to Agent.
                    "planMode": _chat_mode_is_plan(c),
                    "archived": bool(c.get("archived", False)),
                    # True when a task finished in this chat while the user was
                    # not viewing it. Persisted in the chat record + chats.json
                    # index (``has_unread``); the sidebar shows it as the blue
                    # dot until the chat is opened (select_chat clears it).
                    "hasUnread": bool(c.get("has_unread", False)),
                    "pendingInputs": [str(x) for x in (c.get("pending_inputs") or []) if str(x).strip()],
                    # Private: keep the on-disk record file stem so side-data
                    # (file_changes.json) can be resolved without find_chat_by_id.
                    "_recordFile": str(c.get("_record_file") or ""),
                }
            )
        # Per-chat file-change summaries are intentionally NOT bundled into the
        # ``state`` snapshot anymore: with many chats their merged diff rows made
        # every state event tens of MB, and the GUI only renders diffs lazily per
        # turn. The sidecars are instead loaded per chat on demand (see
        # ``ServeApp._ensure_chat_file_changes_loaded`` / ``chat_history``) and
        # capped when serialized (see ``_truncate_file_changes``).
    except Exception:
        pass
    if requested_ws_id and requested_ws_id != str(getattr(agent, "workspace_id", "") or ""):
        _WORKSPACE_ROUTE_LOGGER.info(
            "background-state snapshot_ws=%s snapshot_name=%r global_ws=%s "
            "global_name=%r active_chat=%s chats=%s",
            _ws_id,
            _ws_name,
            str(getattr(agent, "workspace_id", "") or ""),
            str(getattr(agent, "workspace_name", "") or ""),
            active_chat_id,
            [(str(c.get("id") or ""), str(c.get("name") or "")) for c in chats],
        )

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
    # Per-model reasoning-effort map so the GUI can swap the reasoning-effort
    # list immediately when the user picks a different model (instead of waiting
    # for the backend round-trip, which otherwise keeps the old model's efforts
    # visible in the model menu).
    model_reasoning_efforts: Dict[str, List[str]] = {}
    try:
        for choice in (agent._get_configured_model_catalog() or []):
            selector = str(choice.get("selector") or "").strip()
            if not selector:
                continue
            params = choice.get("params") or {}
            efforts = params.get("reasoning_effort") if isinstance(params, dict) else None
            if isinstance(efforts, list):
                normalized = [str(x) for x in efforts if str(x).strip()]
                if normalized:
                    model_reasoning_efforts[selector] = normalized
    except Exception:
        model_reasoning_efforts = {}

    # A model is only "ready" when the resolved config yields a usable model
    # whose values are no longer the shipped template placeholders. The startup
    # template lists a provider/model, so ``model_available`` alone is not a
    # reliable signal — reuse the same validators ``main`` uses at launch so the
    # GUI can show its "set up a model" guide until a real model is configured.
    model_ready = False
    try:
        cfg_data = agent._load_runtime_config_data()
        if isinstance(cfg_data, dict) and cfg_data.get("model_providers"):
            from ..main import (
                _extract_model_runtime_config,
                _validate_template_placeholder_values,
            )

            provider, model_name, model_config, config_error = _extract_model_runtime_config(cfg_data)
            if not config_error:
                template_issue = _validate_template_placeholder_values(
                    provider=provider,
                    model_name=model_name,
                    model_config=model_config,
                )
                model_ready = not template_issue and bool((model_config or {}).get("params"))
    except Exception:
        model_ready = False

    try:
        agent_language = get_display_language(agent)
    except Exception:
        agent_language = "en"

    theme = ""
    ui_prefs: Dict[str, Any] = {}
    gui_language = ""
    console_options: Dict[str, Any] = {"fontFamily": "", "bufferLines": 1000}
    background: Dict[str, Any] = {"hasImage": False, "fileName": "", "opacity": 85, "version": 0}
    try:
        from ..core.config.gui_config import (
            background_ext_from_filename,
            background_image_path,
            load_gui_config,
            normalize_background,
            normalize_console_options,
            normalize_gui_language,
            normalize_ui_prefs,
        )

        gui_cfg = load_gui_config(agent.config_dir)
        theme = str(gui_cfg.get("theme") or "")
        ui_prefs = normalize_ui_prefs(gui_cfg.get("uiPrefs"))
        console_options = normalize_console_options(gui_cfg.get("console"))
        gui_language = normalize_gui_language(gui_cfg.get("language"))
        bg = normalize_background(gui_cfg.get("background"))
        bg_ext = background_ext_from_filename(bg["fileName"])
        has_image = False
        version = 0
        if bg_ext:
            try:
                bg_path = background_image_path(agent.config_dir, bg_ext)
                if bg_path.exists() and bg_path.is_file():
                    has_image = True
                    version = int(bg_path.stat().st_mtime)
            except (ValueError, OSError):
                has_image = False
        background = {
            "hasImage": has_image,
            # Only advertise a file name when the file actually exists so the
            # frontend never requests a missing image.
            "fileName": bg["fileName"] if has_image else "",
            "opacity": bg["opacity"],
            "version": version,
        }
    except Exception:
        theme = ""
        ui_prefs = {}
        gui_language = ""
        console_options = {"fontFamily": "", "bufferLines": 1000}
        background = {"hasImage": False, "fileName": "", "opacity": 85, "version": 0}

    # The GUI's display language is intentionally decoupled from the agent's
    # ``display_language`` (which drives TUI prompts and model system text).
    # We override only when the GUI has its own saved preference so existing
    # configs (no GUI language set) keep following the agent's locale.
    language = gui_language or agent_language

    return {
        "app": {"name": get_app_name(), "version": get_app_version()},
        "workspace": {
            "name": _ws_name,
            "id": _ws_id,
            "root": _ws_root,
            "workDirectory": _ws_work_dir,
        },
        "workspaces": workspaces,
        "chats": chats,
        "activeChatId": _primary_active_chat_id(agent),
        "model": {
            "current": model_current,
            "available": model_available,
            "ready": model_ready,
            "reasoningEffort": _safe_reasoning_effort(agent),
            "reasoningEfforts": _safe_reasoning_efforts(agent),
            "reasoningEffortsBySelector": model_reasoning_efforts,
        },
        "contextUsage": {
            "percent": active_context_percent,
            "tokens": active_context_tokens,
            "window": active_context_window,
            "parts": _safe_context_usage_parts(agent),
        },
        "cacheStats": _compute_chat_cache_stats(agent),
        "tokenStats": _compute_chat_token_stats(agent),
        "language": language,
        "theme": theme,
        "uiPrefs": ui_prefs,
        "consoleOptions": console_options,
        "background": background,
        "plan": _safe_active_plan(agent),
        "askMoreInfo": _safe_pending_request_user_input(agent),
        "executionPolicy": str(getattr(agent, "execution_policy", "") or ""),
    }


class _ChatRuntime:
    """Per-chat execution context: its input queue, busy flag, loop thread.

    Each chat that receives input gets one long-lived ``run_agent_loop`` thread
    bound to that chat's :class:`SessionState`, so several chats can execute
    turns concurrently without sharing conversation/plan/usage state.
    """

    __slots__ = (
        "chat_id",
        "workspace_id",
        "workspace_config_dir",
        "input_queue",
        "busy",
        "thread",
        "turn_started_at",
        "turn_record_pending",
        "idle_since",
    )

    def __init__(
        self, chat_id: str, workspace_id: str = "", workspace_config_dir: str = ""
    ) -> None:
        self.chat_id = str(chat_id or "")
        # Absolute config dir (``…/.codewood`` or default ``…/workspace``) of
        # this chat's workspace, captured at spawn time when the agent globals
        # still point at it. Used to persist a background turn into ITS OWN
        # workspace after focus moves elsewhere — the workspace registry entry
        # is not always sufficient to re-derive this later.
        self.workspace_config_dir = str(workspace_config_dir or "")
        # Workspace this runtime's chat belongs to. Chat ids are only unique
        # within a workspace, so the runtime is keyed in ``_runtimes`` by the
        # workspace-qualified composite (see ``ServeApp._runtime_key``); the
        # bare ``chat_id`` is kept for SSE display routing.
        self.workspace_id = str(workspace_id or "")
        self.input_queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self.busy = threading.Event()
        self.thread: Optional[threading.Thread] = None
        # Per-turn wall-clock tracking so the elapsed "Worked for" time is
        # persisted to this chat's history and survives a reload.
        self.turn_started_at: Optional[float] = None
        self.turn_record_pending = False
        # Monotonic timestamp of when the last turn finished (busy cleared),
        # so the /chat-history handler can skip a disk roll-back while the
        # just-finished turn's persistence flush may still be in flight.
        self.idle_since: Optional[float] = None


#: Last-seen mtime of the per-config-dir ``sandbox_provisioned.flag``. The
#: elevated setup rewrites the flag when provisioning completes; comparing the
#: mtime lets the settings page drop a stale (pre-setup) credential-check
#: cache result and re-verify right after a successful setup.
_sandbox_flag_mtime: Dict[str, float] = {}

#: Last-seen mtime of the per-config-dir ``sandbox_users_ready.flag``.  The
#: elevated setup writes it as soon as the users/group/firewall phase is done
#: (the ACL phase then continues in the background), so advancing this flag
#: also means "a setup just completed" -- the settings page must re-verify the
#: credentials instead of trusting a False cached right before the elevated
#: window finished.
_sandbox_ready_mtime: Dict[str, float] = {}


class ServeApp:
    """Owns the agent loop, the event broadcaster, and the HTTP server."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.broadcaster = _Broadcaster()
        self._confirms: Dict[str, "queue.Queue[str]"] = {}
        self._confirms_lock = threading.Lock()
        # Pending ``request_user_input`` prompts: id -> reply queue. The agent
        # blocks on a per-prompt queue until the frontend POSTs the chosen
        # option (or freeform answer) to ``/answer-ask-more-info``.
        self._request_user_input: Dict[str, "queue.Queue[str]"] = {}
        self._request_user_input_lock = threading.Lock()
        # Pending browser commands: requestId -> reply queue. A browser tool
        # blocks on its queue until the frontend BrowserPanel POSTs the outcome
        # to ``/browser-result``.
        self._browser_cmds: Dict[str, "queue.Queue[Dict[str, Any]]"] = {}
        self._browser_cmds_lock = threading.Lock()
        # Embedded console (GUI): PTY sessions live here. xterm tabs receive
        # output over the SSE stream and send input via HTTP POST (WebView2's
        # file:// origin forbids ws://); the model drives the active session via
        # the ``console_*`` tools.
        from .console_manager import ConsoleManager

        self._console = ConsoleManager(
            cwd_getter=lambda: str(getattr(self.agent, "work_directory", "") or "")
        )
        self._apply_saved_console_options()
        self._shutdown_event = threading.Event()
        # Global cross-workspace chat search index (lazy; background refresher).
        self._chat_search = None
        self._chat_search_refresher_started = False
        self._chat_search_wake = threading.Event()
        self._token = secrets.token_urlsafe(32)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._mcp_reconnect_threads: Dict[str, threading.Thread] = {}
        self._mcp_reconnect_lock = threading.Lock()
        # One runtime (input queue + busy flag + loop thread + per-turn timing)
        # per chat, so multiple chats can run their agent loop concurrently. A
        # chat's runtime is created lazily the first time input is routed to it.
        self._runtimes: Dict[str, "_ChatRuntime"] = {}
        self._runtimes_lock = threading.Lock()
        # Per-workspace persistence context (config_dir + in-memory chat index +
        # RLock), keyed by workspace id. A background chat's loop thread
        # persists through the context for ITS workspace so its turns land in
        # that workspace's chats dir/index even after the user switches the
        # agent's globals to another workspace. Built lazily; the focused
        # workspace keeps using the agent globals and is not cached here.
        self._ws_persist_ctx: Dict[str, Dict[str, Any]] = {}
        self._ws_persist_lock = threading.Lock()
        # GUI focus tracking for the unread blue-dot logic. ``_focus_key`` is
        # the workspace-qualified chat the user last opened (via select_chat);
        # ``_focus_left_at`` records when the user switched AWAY from each chat
        # (monotonic seconds) and ``_focus_left_busy`` whether that chat's task
        # was still running at the moment they left. A turn that finishes right
        # after the user switched away is only suppressed as "they were
        # watching" when the chat was ALREADY idle when they left — if its task
        # was still running, the completion genuinely happened in the background
        # and is flagged unread.
        self._focus_key = ""
        self._focus_left_at: Dict[str, float] = {}
        self._focus_left_busy: Dict[str, bool] = {}
        self._focus_track_lock = threading.Lock()
        self._bridge: Optional["_OutputBridge"] = None
        # Let the chat-state saver know which chats this process owns a
        # live runtime for, so a cross-process merge-on-save never skips
        # overwriting a chat whose loop is actively streaming here.
        try:
            self.agent._active_runtime_chat_ids = self._owned_runtime_chat_ids
        except Exception:
            pass
        # Serve mode is the GUI backend: adopt the saved GUI language as a
        # session-only override so backend-produced text matches the GUI from
        # the first tick, without persisting it as the TUI's display_language.
        self._apply_saved_gui_language_override()

    def _apply_saved_gui_language_override(self) -> None:
        try:
            from ..core.config.gui_config import (
                load_gui_config,
                normalize_gui_language,
            )
            from ..core.localization import normalize_display_language

            gui_cfg = load_gui_config(self.agent.config_dir)
            value = normalize_gui_language(gui_cfg.get("language"))
            if value:
                self.agent._gui_language_override = (
                    normalize_display_language(value) or value
                )
        except Exception:
            # Best-effort: fall back to the agent's own display_language.
            pass

    def _runtime_key(self, chat_id: str, workspace_id: Optional[str] = None) -> str:
        """Workspace-qualified key for the ``_runtimes`` map.

        Mirrors ``Agent._session_registry_key`` so a runtime, its bound
        :class:`SessionState`, and the thread lookup all share one composite
        key. Chat ids repeat across workspaces (``chat-1`` … per workspace), so
        keying by the bare id would make a running chat in one workspace and a
        same-id chat in another collide on a single runtime — routing the new
        chat's input into the old loop and the old loop's output into the new
        chat. ``workspace_id`` defaults to the agent's current workspace.
        """
        cid = str(chat_id or "")
        if not cid:
            return ""
        wsid = workspace_id
        if wsid is None:
            wsid = str(getattr(self.agent, "workspace_id", "") or "").strip()
        wsid = str(wsid or "").strip()
        return f"{wsid}::{cid}" if wsid else cid

    def _owned_runtime_chat_ids(self) -> List[str]:
        """Chat ids with an ACTIVELY RUNNING turn in THIS process.

        Only a chat whose runtime is busy (a turn is mid-stream) is an
        authoritative writer whose in-memory state must not be overwritten by
        a disk merge. An idle parked runtime does not count — its chat may
        have been amended by a peer process and should be refreshable.

        Returns BARE chat ids scoped to the CURRENT workspace. Chat ids repeat
        across workspaces, and both consumers operate on the focused
        workspace's ``_chat_state``:
          * the disk-merge guard compares against that workspace's chat ids;
          * ``_build_state``'s per-chat ``running`` flag enumerates that
            workspace's chats.
        Without the workspace filter, a chat running in workspace A would mark
        a same-id chat in the focused workspace B as running — showing a phantom
        "Working…" / breathing blue dot on B's chat. Filtering by the runtime's
        own ``workspace_id`` keeps the busy state attributed to the right one.
        """
        out: List[str] = []
        try:
            cur_ws = str(getattr(self.agent, "workspace_id", "") or "").strip()
            with self._runtimes_lock:
                for rt in self._runtimes.values():
                    if rt is None or not rt.busy.is_set():
                        continue
                    rt_ws = str(getattr(rt, "workspace_id", "") or "").strip()
                    # Match same-workspace runtimes; tolerate an empty rt_ws
                    # (legacy/default workspace) against an empty current id.
                    if rt_ws == cur_ws:
                        out.append(str(rt.chat_id))
        except Exception:
            return []
        return out

    def _chat_is_busy(self, chat_id: str, workspace_id: Optional[str] = None) -> bool:
        """True iff ``chat_id`` has a runtime with a turn currently streaming."""
        cid = str(chat_id or "")
        if not cid:
            return False
        try:
            key = self._runtime_key(cid, workspace_id)
            with self._runtimes_lock:
                rt = self._runtimes.get(key)
            return bool(rt is not None and rt.busy.is_set())
        except Exception:
            return False

    def _chat_runtime_recently_finished(
        self, chat_id: str, workspace_id: Optional[str] = None,
    ) -> bool:
        """True when THIS process owns a runtime for the chat and its last
        turn finished within the recent window.

        The idle SSE event that follows a finished turn triggers an immediate
        frontend history reload.  At that instant the in-memory session is the
        complete, authoritative state, while the disk record may still lag the
        final persistence flush; refreshing from disk then would roll the
        conversation back and the reload would surface a truncated transcript.
        A long-parked runtime (window elapsed) is refreshable again so peer
        process amendments (e.g. a TUI persisting a ``request_user_input``
        prompt) are still picked up, matching ``_owned_runtime_chat_ids``.
        """
        try:
            key = self._runtime_key(chat_id, workspace_id)
            with self._runtimes_lock:
                rt = self._runtimes.get(key)
            if rt is None:
                return False
            idle_since = getattr(rt, "idle_since", None)
            if not idle_since:
                return False
            return (time.monotonic() - idle_since) < 3.0
        except Exception:
            return False

    def _active_chat_id(self) -> str:
        """Chat id the running turn / streamed output is attributed to.

        On the agent-loop thread this resolves from the thread-bound runtime
        so a background chat's output carries ITS chat id even after the
        user focuses another chat.  Falls back to the shared focused chat
        for HTTP handler threads that have no bound runtime.
        """
        rt = self._runtime_for_thread()
        if rt is not None and rt.chat_id:
            return rt.chat_id
        try:
            cid = str(getattr(self.agent, "active_chat_id", "") or "")
        except Exception:
            cid = ""
        return cid or _primary_active_chat_id(self.agent)

    def _active_chat_workspace_id(self) -> str:
        """Workspace id the running turn / streamed output belongs to.

        Resolved from the runtime bound to the calling loop thread (so a
        background chat's output carries ITS workspace, not whatever the user
        has since focused). Falls back to the agent's current workspace for
        HTTP handler threads that have no bound runtime.
        """
        rt = self._runtime_for_thread()
        if rt is not None and rt.workspace_id:
            return rt.workspace_id
        return str(getattr(self.agent, "workspace_id", "") or "")

    def _route(self, chat_id: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
        """Build a per-chat SSE payload tagged with chat + workspace id.

        The GUI gates incoming per-chat events by ``workspaceId`` (chat ids
        repeat across workspaces) and routes by ``chatId`` within the matching
        workspace. When ``chat_id`` is omitted we use the running turn's chat
        and its workspace; passing an explicit ``chat_id`` keeps the current
        workspace context (used by HTTP-thread broadcasts after a switch).
        """
        cid = self._active_chat_id() if chat_id is None else str(chat_id or "")
        payload: Dict[str, Any] = {
            "chatId": cid,
            "workspaceId": self._active_chat_workspace_id(),
        }
        payload.update(extra)
        # A state payload must be self-consistent with its SSE envelope.  The
        # renderer uses the envelope to decide which workspace cache to update;
        # if a background loop ever supplies A's chat list under B's envelope,
        # same-id chats (chat-1, chat-2, ...) visibly jump between workspaces.
        # The snapshot is the authoritative payload — it carries the chat list
        # the renderer caches — so when the envelope disagrees we RE-TAG the
        # envelope to the snapshot's workspace instead of emitting a payload
        # that would misroute. This happens when a chat record was resolved
        # through a stale/ambient index while its loop thread's runtime carries
        # a different workspace id (e.g. a workspace switch in flight), so the
        # loop thread's ``idle``/``state`` event gets the wrong envelope.
        # Keep this warning in the normal app log so a reproduction includes
        # both ids and the affected list, without logging streamed content.
        snapshot = payload.get("state")
        if isinstance(snapshot, dict):
            snapshot_ws_id = str(
                (snapshot.get("workspace") or {}).get("id") or ""
            )
            route_ws_id = str(payload.get("workspaceId") or "")
            if snapshot_ws_id and route_ws_id and snapshot_ws_id != route_ws_id:
                _WORKSPACE_ROUTE_LOGGER.warning(
                    "state-route mismatch envelope_ws=%s snapshot_ws=%s chat=%s chats=%s "
                    "-> re-routing envelope to snapshot workspace",
                    route_ws_id,
                    snapshot_ws_id,
                    cid,
                    [
                        (str(item.get("id") or ""), str(item.get("name") or ""))
                        for item in snapshot.get("chats", [])
                        if isinstance(item, dict)
                    ],
                )
                payload["workspaceId"] = snapshot_ws_id
        return payload

    def _runtime_for_thread(self) -> Optional["_ChatRuntime"]:
        """The runtime owning the calling loop thread (bound chat).

        The thread's bound session key is already the workspace-qualified
        composite (see ``Agent._bind_session``), which is exactly the key we
        store runtimes under, so a same-id chat in another workspace can never
        resolve to this thread's runtime.
        """
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
                # (e.g. on an request_user_input pause).
                if agent._parse_task_worked_summary_history_content(content) is not None:
                    return
            rec = getattr(agent, "_record_task_worked_summary_history", None)
            if callable(rec):
                rec(elapsed)
        except Exception:
            pass

    def _state_for_loop_thread(self) -> Dict[str, Any]:
        """Build GUI state for the calling loop thread's workspace.

        Unlike ``_build_state`` this does NOT suspend the persist override, so
        ``_chat_entries()`` and ``_primary_active_chat_id()`` read from the
        thread's workspace index — essential for background chats whose state
        events must describe their OWN workspace, not the focused one.
        """
        rt = self._runtime_for_thread()
        return _build_state_inner(self.agent,
            workspace_id=rt.workspace_id if rt is not None else "")

    @staticmethod
    def _chat_name_from_state(state: Any, chat_id: Any) -> str:
        """Best-effort chat display name from a GUI state snapshot.

        ``state`` is the snapshot built for the chat's OWN workspace (see
        ``_state_for_loop_thread``), so looking the name up there is correct
        even for a background chat whose workspace differs from the focused
        one. Falls back to "" (the caller drops the event) when the chat is
        not present in the snapshot.
        """
        if not isinstance(state, dict):
            return ""
        try:
            for c in state.get("chats") or []:
                if isinstance(c, dict) and str(c.get("id") or "") == str(
                    chat_id or ""
                ):
                    return str(c.get("name") or "").strip()
        except Exception:
            pass
        return ""

    @contextlib.contextmanager
    def _runtime_persistence_scope(self, rt: "_ChatRuntime"):
        """Bind an HTTP thread to *rt* before persisting its live session.

        Workspace changes are handled by a different HTTP worker from the
        chat's loop.  Its thread-local session can therefore still point at a
        chat from the workspace just left, while the agent globals already
        point at the newly selected workspace.  Syncing in that mixed state
        writes a same-id chat into the wrong index.  This scope qualifies both
        the session and the persistence override from the runtime's captured
        workspace, and restores the request thread exactly afterwards.
        """
        agent = self.agent
        tls = getattr(agent, "_session_tls", None)
        previous_chat_key = str(getattr(tls, "chat_id", "") or "") if tls else ""
        previous_session = getattr(tls, "session", None) if tls else None
        get_ctx = getattr(agent, "_persist_workspace_ctx", None)
        previous_ctx = get_ctx() if callable(get_ctx) else None
        set_ctx = getattr(agent, "_set_persist_workspace_ctx", None)
        try:
            agent._bind_session(rt.chat_id, rt.workspace_id)
            if callable(set_ctx) and rt.workspace_id:
                cfg = rt.workspace_config_dir
                set_ctx(
                    {
                        "workspace_id": rt.workspace_id,
                        "provider": (
                            lambda wsid, _cfg=cfg: self._persist_ctx_for_workspace(
                                wsid, _cfg
                            )
                        ),
                    }
                )
            yield
        finally:
            if callable(set_ctx):
                set_ctx(previous_ctx)
            if tls is not None:
                tls.chat_id = previous_chat_key
                tls.session = previous_session

    def _chat_is_busy_key(self, key: str) -> bool:
        """True iff the runtime identified by a workspace-qualified key is busy.

        ``key`` is the same composite ``_runtime_key`` produces (and the
        ``_focus_key`` marker uses), so a focus-leave snapshot can look up the
        chat being left directly without re-splitting the key.
        """
        if not key:
            return False
        try:
            with self._runtimes_lock:
                rt = self._runtimes.get(key)
            return bool(rt is not None and rt.busy.is_set())
        except Exception:
            return False

    def _track_focus(self, chat_id: str, workspace_id: str = "") -> None:
        """Record that the GUI user opened ``chat_id`` (and left the previous one).

        Drives the unread decision's "just left a busy chat" window: when the
        user switches away from a chat, its leave time AND whether its task was
        still running are snapshotted here. A turn that finishes a moment later
        is only suppressed as "the user was watching" when the chat was ALREADY
        idle when they left; if the task was still running, the completion is a
        genuine background one and the chat is flagged unread. Only explicit
        user opens (select_chat) move the focus marker; backend-internal
        ``_activate_chat`` calls (e.g. chat-history reads) never do.
        """
        key = self._runtime_key(chat_id, workspace_id)
        if not key:
            return
        with self._focus_track_lock:
            prev_key = self._focus_key
            now = time.monotonic()
            if prev_key and prev_key != key:
                self._focus_left_at[prev_key] = now
            self._focus_key = key
        if prev_key and prev_key != key:
            # Snapshot whether the chat we just left had a task still running;
            # the unread decision uses this to tell "user watched it finish"
            # (chat idle at leave) from "genuine background completion" (chat
            # still busy at leave).
            try:
                prev_busy = self._chat_is_busy_key(prev_key)
            except Exception:
                prev_busy = False
            with self._focus_track_lock:
                self._focus_left_busy[prev_key] = prev_busy

    def _mark_completed_chat_unread(self, rt: Optional["_ChatRuntime"]) -> None:
        """Set/clear the persistent unread flag when a chat's turn finishes.

        The chat whose loop just returned to waiting for input is ``rt``. It is
        "background" (unread) when it is NOT the chat the GUI user is currently
        viewing: either a different chat in the focused workspace, or any chat
        in a non-focused workspace (a loop thread whose workspace differs from
        the focused one runs with a thread-local persistence override, so the
        flag is written into ITS OWN workspace's index). Finishing while the
        user is viewing the chat clears any stale flag. The flag is decided
        here, at turn end, so late/duplicate idle events can never re-mark a
        chat the user already saw complete — including the case where the user
        switched away a moment before the loop thread got to run (the
        ``just_left`` window below only fires when the chat was already idle at
        leave time, i.e. the user had actually watched it finish).
        """
        if rt is None:
            return
        cid = str(getattr(rt, "chat_id", "") or "").strip()
        if not cid:
            return
        try:
            rt_ws = str(getattr(rt, "workspace_id", "") or "").strip()
            focused_ws = str(getattr(self.agent, "workspace_id", "") or "").strip()
            # A chat in a non-focused workspace is by definition not being
            # viewed. Otherwise the chat is "focused" when it matches the chat
            # the GUI user last opened (select_chat), falling back to the
            # workspace index's active chat before any user switch has happened
            # (startup). Using the user-driven marker instead of the raw index
            # keeps the decision immune to backend-internal _activate_chat
            # calls (chat-history reads) transiently moving the active pointer.
            background = bool(rt_ws) and rt_ws != focused_ws
            key = self._runtime_key(cid, rt_ws)
            lock = getattr(self, "_focus_track_lock", None)
            if lock is not None:
                with lock:
                    current = str(getattr(self, "_focus_key", "") or "")
                    left_at = float(getattr(self, "_focus_left_at", {}).get(key, 0.0) or 0.0)
                    left_busy = bool(getattr(self, "_focus_left_busy", {}).get(key, False))
            else:
                current = str(getattr(self, "_focus_key", "") or "")
                left_at = float(getattr(self, "_focus_left_at", {}).get(key, 0.0) or 0.0)
                left_busy = bool(getattr(self, "_focus_left_busy", {}).get(key, False))
            if background:
                focused = False
            elif current:
                focused = key == current
            else:
                focused = cid == _primary_active_chat_id(self.agent)
            # The user was watching the completion only if they left this chat
            # very recently (a few seconds) AND its task was ALREADY done when
            # they left — otherwise the task was still running and the finish is
            # a genuine background completion. Mirrors the legacy frontend
            # ``wasWatching`` heuristic, now decided server-side and persisted.
            just_left = (
                left_at > 0
                and (time.monotonic() - left_at) < 3.0
                and not left_busy
            )
            unread = (not focused) and (not just_left)
            setter = getattr(self.agent, "_set_chat_unread", None)
            if callable(setter):
                setter(cid, unread)
            else:
                manager = getattr(self.agent, "_chat_state_manager", None)
                setter2 = getattr(manager, "set_chat_unread", None)
                if callable(setter2):
                    setter2(cid, unread)
        except Exception:
            pass

    def _input_provider(self) -> str:
        """Replacement for ``agent._get_user_input_with_history``.

        Runs on a chat's dedicated loop thread (bound to that chat's session),
        so it reads input from that chat's queue and tags all events with that
        chat's id.
        """
        rt = self._runtime_for_thread()
        # A turn has just finished only when the runtime was marked busy — the
        # loop also calls this once at spawn and after every park, when there
        # is no turn to account for. Decide the unread flag immediately (before
        # any slower post-turn work such as the elapsed-time summary), so the
        # user's focus at completion time is still accurate: waiting longer
        # would let them switch to another chat and falsely flag this one.
        was_busy = rt is not None and rt.busy.is_set()
        turn_finished = False
        if was_busy:
            rt.busy.clear()
            # Remember when this turn finished so the /chat-history handler
            # can tell a JUST-finished turn (in-memory session is complete
            # and authoritative while the disk record may still lag the
            # final persistence flush) from a long-parked runtime (disk is
            # authoritative for peer-process amendments).
            rt.idle_since = time.monotonic()
            # Only a GENUINE model turn leaves an unread marker. The loop also
            # returns here after GUI-internal commands (rename, edit, fork,
            # execution-policy, and drained request_user_input prompts), which
            # set ``busy`` but produce no new task output — marking those would
            # surface spurious blue dots on chats with no real activity.
            # ``turn_record_pending`` is exactly the "this was a real user
            # prompt" flag the elapsed-time recorder uses for the same reason.
            pending = bool(getattr(rt, "turn_record_pending", False))
            started = getattr(rt, "turn_started_at", None) is not None
            if pending and started:
                turn_finished = True
                # If the chat that ran is NOT the one the user is currently
                # viewing, leave a persistent unread flag (blue dot) on it —
                # decided deterministically here, at the moment the turn ends,
                # instead of by a frontend heuristic that raced with focus
                # switches. Opening the chat (select_chat) clears the flag.
                self._mark_completed_chat_unread(rt)
        # Wall-clock time this turn took, read before ``_record_turn_elapsed``
        # clears the per-turn start marker.
        elapsed_seconds = 0
        if rt is not None and rt.turn_started_at is not None:
            elapsed_seconds = int(max(0, time.monotonic() - rt.turn_started_at))
        self._record_turn_elapsed(rt)
        state = self._state_for_loop_thread()
        self.broadcaster.publish("idle", self._route(state=state))
        # A genuine user turn finished. A separate lightweight event lets the
        # desktop host raise a native "task finished" notification when the
        # window is hidden, without the host having to watch every idle
        # snapshot. GUI-internal commands (rename/edit/switch/...) never fire it.
        if turn_finished and rt is not None:
            self.broadcaster.publish(
                "task_finished",
                {
                    "chatId": str(getattr(rt, "chat_id", "") or ""),
                    "workspaceId": str(getattr(rt, "workspace_id", "") or ""),
                    "chatName": self._chat_name_from_state(
                        state, getattr(rt, "chat_id", "")
                    ),
                    "elapsedSeconds": elapsed_seconds,
                },
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
        rt.idle_since = None
        rt.turn_started_at = time.monotonic()
        # Install this loop thread's per-workspace persistence override for the
        # whole turn. We MUST install it even though this chat is (usually)
        # focused right now: the user may switch to another workspace mid-turn,
        # after which the agent globals point elsewhere and this background
        # turn must persist into ITS OWN workspace. The override carries only
        # the workspace id + a lazy provider; the manager activates it (and
        # resolves the concrete index/dir/lock from disk) only once focus has
        # moved away, so a still-focused turn keeps using the globals.
        try:
            setter = getattr(self.agent, "_set_persist_workspace_ctx", None)
            if callable(setter):
                if str(rt.workspace_id or "").strip():
                    rt_cfg = rt.workspace_config_dir
                    setter(
                        {
                            "workspace_id": rt.workspace_id,
                            "provider": (
                                lambda wsid, _cfg=rt_cfg: self._persist_ctx_for_workspace(
                                    wsid, _cfg
                                )
                            ),
                        }
                    )
                else:
                    setter(None)
        except Exception:
            pass
        # Composer input carries a force-prompt sentinel; strip it from the
        # displayed/broadcast text but keep it on the line the loop consumes.
        forced = str(text).startswith(GUI_FORCE_PROMPT_PREFIX)
        if forced:
            display = str(text)[len(GUI_FORCE_PROMPT_PREFIX):]
        else:
            display = str(text)
        # Defensive: a bare slash line that somehow reaches the loop without
        # the force-prompt sentinel is treated as an internal command — hide
        # its echo/output and skip turn bookkeeping so it never leaks into the
        # model prompt or persisted history.
        is_internal_command = (not forced) and display.lstrip().startswith("/")
        if self._bridge is not None:
            # Per-thread: only silences this loop thread's writes.
            self._bridge.suppressed = is_internal_command
        rt.turn_record_pending = not is_internal_command
        if not is_internal_command:
            self.broadcaster.publish(
                "turn_start", self._route(text=display)
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
                "workspaceId": self._active_chat_workspace_id(),
            },
        )
        try:
            answer = reply.get()
        finally:
            with self._confirms_lock:
                self._confirms.pop(cid, None)
        raw_answer = str(answer or "").strip()
        # A "reject & supplement info" answer arrives as a JSON payload; store
        # the supplementary text and report a plain "no" to the legacy y/n
        # caller (the text is picked up by the next confirm result builder).
        if raw_answer.startswith("{"):
            try:
                payload = json.loads(raw_answer)
                supp = str(payload.get("reject_with_supplement") or "").strip()
                if supp:
                    try:
                        from ..services.execution_policy_service import set_confirm_supplement

                        set_confirm_supplement(self.agent, supp)
                    except Exception:
                        pass
                    return "n"
            except Exception:
                pass
        return str(answer or "")

    def _confirm_choice_provider(
        self,
        prompt: str,
        options: List[str],
        offer_always: bool = False,
        command: Optional[str] = None,
        preview_segments: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Structured confirmation prompt for the execution-policy gate.

        Broadcasts a ``confirm`` SSE event carrying the prompt plus a fixed
        list of option labels (Yes / No / optionally Always) plus a
        ``rejectSupplement`` flag and blocks until the frontend POSTs the
        chosen answer to ``/confirm``. The frontend renders an inline
        single-choice panel (same style as the ``request_user_input`` panel)
        below the message area; the user's pick is posted back as the option
        **index** and mapped here to ``\"y\" | \"n\" | \"a\"`` locally — the
        choice is never sent to the model. When the user chooses
        "reject & supplement info" the frontend posts a JSON payload
        ``{\"reject_with_supplement\": \"<text>\"}``; the text is stored on the
        agent and ``\"n_supplement\"`` is returned so the caller keeps the task
        running with the user's feedback.

        Returns ``"n"`` (cancel) when the prompt is dismissed without a pick.
        """
        cid = secrets.token_hex(8)
        reply: "queue.Queue[str]" = queue.Queue()
        with self._confirms_lock:
            self._confirms[cid] = reply
        safe_options: List[str] = []
        for raw in options or []:
            label = strip_ansi(str(raw or ""))[:120]
            if label:
                safe_options.append(label)
        # Convert the structured change-preview segments into JSON-serializable
        # diff rows so the frontend can render the preview natively (responsive
        # side-by-side / inline) with syntax highlighting, instead of relying on
        # pre-rendered ANSI text.
        diff_rows: List[Dict[str, Any]] = []
        if preview_segments:
            try:
                from ..core.change_preview_formatter import ChangePreviewFormatter

                diff_rows = ChangePreviewFormatter.format_segments_structured(
                    preview_segments
                )
            except Exception:
                diff_rows = []
        self.broadcaster.publish(
            "confirm",
            {
                "id": cid,
                "prompt": strip_ansi(str(prompt or "")),
                "command": strip_ansi(str(command or "")),
                "options": safe_options,
                "offerAlways": bool(offer_always),
                "rejectSupplement": True,
                "diffRows": diff_rows,
                "chatId": self._active_chat_id(),
                "workspaceId": self._active_chat_workspace_id(),
            },
        )
        try:
            answer = reply.get()
        finally:
            with self._confirms_lock:
                self._confirms.pop(cid, None)
        raw_answer = str(answer or "").strip()
        # "Reject & supplement info": the frontend posts the user's free text
        # as a JSON payload. Store it on the agent so the caller can attach it
        # to the role:tool result and continue the task with the feedback.
        if raw_answer.startswith("{"):
            try:
                payload = json.loads(raw_answer)
                supp = str(payload.get("reject_with_supplement") or "").strip()
                if supp:
                    try:
                        from ..services.execution_policy_service import set_confirm_supplement

                        set_confirm_supplement(self.agent, supp)
                    except Exception:
                        pass
                    return "n_supplement"
            except Exception:
                pass
        # The frontend posts either the option index (preferred) or a direct
        # y/n/a token. Map both to the canonical y/n/a the policy gate expects.
        ans = raw_answer.lower()
        if ans.isdigit():
            idx = int(ans)
            if 0 <= idx < len(safe_options):
                if idx == 0:
                    return "y"
                if offer_always and idx == len(safe_options) - 1 and len(safe_options) >= 3:
                    return "a"
                return "n"
            return "n"
        if ans in ("y", "yes"):
            return "y"
        if ans in ("a", "always") and offer_always:
            return "a"
        return "n"

    def _request_user_input_provider(
        self,
        question: str,
        options: List[str],
        multi_select: bool = False,
    ) -> str:
        """Replacement for the TUI ``request_user_input`` prompt.

        Broadcasts an ``request_user_input`` SSE event carrying the question,
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
        with self._request_user_input_lock:
            self._request_user_input[pid] = reply
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
            setter = getattr(self.agent, "_set_pending_request_user_input", None)
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
            "request_user_input",
            {
                "id": pid,
                "question": safe_question,
                "options": safe_options,
                "multiSelect": bool(multi_select),
                "chatId": self._active_chat_id(),
                "workspaceId": self._active_chat_workspace_id(),
            },
        )
        try:
            answer = reply.get()
        finally:
            with self._request_user_input_lock:
                self._request_user_input.pop(pid, None)
        return str(answer or "")

    # ----- API surface used by the HTTP handler ---------------------------
    @property
    def token(self) -> str:
        return self._token

    def _diagnose_server_health(self) -> None:
        """Emit a diagnostic snapshot to the server log for ``/server-health``.

        The dump covers the things you want when the GUI shows "Working…" for
        a long time with no new output:

        * a full stack trace of every live thread (where is each one stuck?);
        * SSE client connections and how backed up each client's event queue
          is (a deep queue means the GUI is lagging behind the server);
        * every chat runtime: busy flag, pending input depth, loop thread state;
        * pending confirm / ``request_user_input`` / browser-command queues
          (a blocked interaction silently stalls a turn);
        * the HTTP server endpoint and basic agent state.

        Nothing here touches the model, the input queue, or persisted history.
        """

        def _qsize(q: Any) -> int:
            try:
                return int(q.qsize())
            except Exception:
                return -1

        lines: List[str] = []
        try:
            import platform
            import traceback

            from ..config.app_info import get_app_logger_root
            from ..core.logging.app_logging import get_log_file_path, get_logger
        except Exception:
            return
        agent = self.agent
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines.append("=" * 76)
        lines.append(
            f"[server-health] {stamp} PID={os.getpid()} "
            f"log={get_log_file_path() or 'n/a'} python={platform.python_version()}"
        )
        # --- SSE client connections -------------------------------------
        try:
            subs = list(self.broadcaster._subscribers)
            lines.append(f"SSE subscribers: {len(subs)}")
            for i, q in enumerate(subs):
                lines.append(f"  subscriber[{i}] pending_events={_qsize(q)}")
        except Exception as exc:  # pragma: no cover
            lines.append(f"SSE subscribers: <error: {exc}>")
        # --- HTTP server -------------------------------------------------
        try:
            httpd = self._httpd
            if httpd is not None:
                lines.append(
                    f"HTTP server: {getattr(httpd, 'server_address', None)} "
                    f"shutdown={self._shutdown_event.is_set()}"
                )
            else:
                lines.append("HTTP server: <not started>")
        except Exception:
            lines.append("HTTP server: <unavailable>")
        # --- chat runtimes -----------------------------------------------
        lines.append("Chat runtimes:")
        try:
            with self._runtimes_lock:
                runtimes = list(self._runtimes.values())
        except Exception:
            runtimes = []
        if not runtimes:
            lines.append("  (none)")
        for rt in runtimes:
            t = getattr(rt, "thread", None)
            busy = bool(getattr(rt, "busy", None) is not None and rt.busy.is_set())
            thread_desc = "dead"
            if t is not None:
                thread_desc = f"alive name={t.name} ident={t.ident}"
                try:
                    if not t.is_alive():
                        thread_desc = "dead"
                except Exception:
                    pass
            lines.append(
                f"  chat={getattr(rt, 'chat_id', '?')} "
                f"ws={getattr(rt, 'workspace_id', '')} "
                f"busy={busy} pending_inputs={_qsize(getattr(rt, 'input_queue', None))} "
                f"thread={thread_desc} "
                f"turn_started_at={getattr(rt, 'turn_started_at', None)}"
            )
        # --- pending interaction queues ----------------------------------
        lines.append("Pending interaction queues:")
        for label, lock, coll in (
            ("confirms", self._confirms_lock, self._confirms),
            ("request_user_input", self._request_user_input_lock, self._request_user_input),
            ("browser_cmds", self._browser_cmds_lock, self._browser_cmds),
        ):
            try:
                with lock:
                    count = len(coll)
            except Exception:
                count = -1
            lines.append(f"  {label}: {count}")
        # --- agent overview ----------------------------------------------
        lines.append("Agent state:")
        for key, value in (
            ("active_chat_id", getattr(agent, "active_chat_id", "")),
            ("active_chat_name", getattr(agent, "active_chat_name", "")),
            ("workspace_id", getattr(agent, "workspace_id", "")),
            ("workspace_name", getattr(agent, "workspace_name", "")),
            ("execution_policy", getattr(agent, "execution_policy", "")),
            ("gui_plain_stream", getattr(agent, "_gui_plain_stream", False)),
        ):
            lines.append(f"  {key}={value}")
        try:
            lines.append(f"  busy_chats={self._owned_runtime_chat_ids()}")
        except Exception:
            pass
        # --- thread stacks ------------------------------------------------
        lines.append("Thread stacks:")
        try:
            frames = sys._current_frames()  # type: ignore[attr-defined]
        except Exception:
            frames = {}
        alive = [t for t in threading.enumerate() if t.is_alive()]
        if not alive:
            lines.append("  (no live threads)")
        for t in alive:
            name = getattr(t, "name", "?")
            ident = getattr(t, "ident", None)
            daemon = bool(getattr(t, "daemon", False))
            lines.append(f"  [{name}] ident={ident} daemon={daemon} alive=True")
            frame = frames.get(ident) if ident is not None else None
            if frame is None:
                lines.append("      <no current frame>")
                continue
            try:
                for raw in traceback.format_stack(frame):
                    lines.append("      " + raw.strip())
            except Exception as exc:  # pragma: no cover
                lines.append(f"      <stack unavailable: {exc}>")
        lines.append("=" * 76)
        try:
            logger = get_logger(f"{get_app_logger_root()}.server")
            logger.info("%s", "\n".join(lines))
        except Exception:  # pragma: no cover
            pass

    def submit_input(
        self,
        text: str,
        chat_id: str = "",
        as_prompt: bool = False,
        workspace_id: str = "",
    ) -> None:
        line = str(text or "")
        # Diagnostic hook: ``/server-health`` dumps thread stacks and
        # connection state to the server log. It is swallowed entirely —
        # never queued, never sent to the model, and never persisted — so it
        # can be fired even while a turn looks stuck.
        if line.strip() == "/server-health":
            self._diagnose_server_health()
            return
        # Validate the (workspace_id, chat_id) pair so a same-id chat in the
        # wrong workspace is never dispatched to; invalid pairs fall back to the
        # focused active chat.
        cid, wsid = self._resolve_chat_scope(chat_id, workspace_id)
        # Composer input is forced to a model prompt: prefix a sentinel the
        # runtime loop strips so "/foo" / "!bar" never run as command/shell.
        if as_prompt and line:
            line = GUI_FORCE_PROMPT_PREFIX + line
        rt = self._get_or_spawn_runtime(cid, wsid)
        rt.input_queue.put(line)

    def _get_or_spawn_runtime(
        self, chat_id: str, workspace_id: Optional[str] = None
    ) -> "_ChatRuntime":
        """Return the chat's runtime, starting its loop thread on first use.

        Keyed by the workspace-qualified composite so a chat in a newly-focused
        workspace never reuses a same-id chat's runtime from another workspace.
        ``workspace_id`` defaults to the agent's current workspace.
        """
        cid = str(chat_id or "")
        if workspace_id is not None and str(workspace_id or "").strip():
            wsid = str(workspace_id or "").strip()
        else:
            wsid = str(getattr(self.agent, "workspace_id", "") or "").strip()
        key = self._runtime_key(cid, wsid)
        with self._runtimes_lock:
            rt = self._runtimes.get(key)
            if rt is not None:
                return rt
            try:
                ws_cfg = str(getattr(self.agent, "workspace_config_dir", "") or "")
            except Exception:
                ws_cfg = ""
            rt = _ChatRuntime(cid, wsid, ws_cfg)
            self._runtimes[key] = rt
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

    def answer_request_user_input(self, pid: str, answer: str) -> bool:
        """Resolve a pending ``request_user_input`` prompt with the user's answer.

        Returns ``False`` when no such pending prompt exists (e.g. the
        request was cancelled, already answered, or the chat was reset
        between rendering and answering); the frontend treats that as a
        no-op and just dismisses the panel.
        """
        with self._request_user_input_lock:
            reply = self._request_user_input.get(str(pid or ""))
        if reply is None:
            return False
        reply.put(str(answer or ""))
        return True

    @contextlib.contextmanager
    def _session_scope_for_chat(self, chat_id: str, workspace_id: str = ""):
        """Temporarily bind the HTTP thread to a specific chat session key."""
        agent = self.agent
        wsid = str(workspace_id or "").strip()
        tls = agent.__dict__.get("_session_tls")
        prev_chat = str(getattr(tls, "chat_id", "") or "") if tls is not None else ""
        prev_session = getattr(tls, "session", None) if tls is not None else None
        agent._bind_session(chat_id, wsid if wsid else None)
        try:
            yield
        finally:
            tls2 = agent.__dict__.get("_session_tls")
            if tls2 is not None:
                tls2.chat_id = prev_chat
                tls2.session = prev_session

    def set_chat_model(self, chat_id: str, model: str, workspace_id: str = "") -> bool:
        """Persist and apply a model selection for one GUI chat directly."""
        agent = self.agent
        cid = str(chat_id or "").strip()
        selector = str(model or "").strip()
        wsid = str(workspace_id or "").strip()
        if not cid or not selector:
            return False
        original_wsid = str(getattr(agent, "workspace_id", "") or "").strip()
        switched = False
        try:
            if wsid and wsid != original_wsid:
                from ..controllers.workspace_command_controller import (
                    workspace_switch_command,
                )

                with contextlib.redirect_stdout(io.StringIO()):
                    workspace_switch_command(agent, wsid, lazy_records=True)
                switched = True
        except Exception:
            if switched:
                try:
                    from ..controllers.workspace_command_controller import (
                        workspace_switch_command,
                    )

                    with contextlib.redirect_stdout(io.StringIO()):
                        workspace_switch_command(agent, original_wsid, lazy_records=True)
                except Exception:
                    pass
            return False
        try:
            with self._session_scope_for_chat(cid, wsid or str(getattr(agent, "workspace_id", "") or "")):
                agent.active_chat_id = cid
                refresh = getattr(agent, "_refresh_chat_record_from_disk", None)
                if callable(refresh):
                    refresh(cid)
                target = agent._find_chat_by_id(cid)
                if not target:
                    return False
                choice = agent._find_configured_model_choice(selector)
                if not choice:
                    return False
                agent._apply_chat_model_from_entry(target, persist_if_missing=True)
                agent._switch_model_by_selector(selector)
                _mark_dirty = getattr(agent, "_mark_chat_dirty", None)
                if callable(_mark_dirty):
                    _mark_dirty(cid)
                try:
                    agent._save_chat_state()
                except Exception:
                    pass
        except Exception:
            return False
        finally:
            if switched:
                try:
                    from ..controllers.workspace_command_controller import (
                        workspace_switch_command,
                    )

                    with contextlib.redirect_stdout(io.StringIO()):
                        workspace_switch_command(agent, original_wsid)
                except Exception:
                    pass
        self.broadcaster.publish(
            "state",
            self._route(chat_id=cid, state=_build_state(agent)),
        )
        return True

    def set_chat_reasoning(self, chat_id: str, reasoning: str, workspace_id: str = "") -> bool:
        """Persist and apply a reasoning-effort selection for one GUI chat."""
        agent = self.agent
        cid = str(chat_id or "").strip()
        wsid = str(workspace_id or "").strip()
        if not cid:
            return False
        original_wsid = str(getattr(agent, "workspace_id", "") or "").strip()
        switched = False
        try:
            if wsid and wsid != original_wsid:
                from ..controllers.workspace_command_controller import (
                    workspace_switch_command,
                )

                with contextlib.redirect_stdout(io.StringIO()):
                    workspace_switch_command(agent, wsid)
                switched = True
            with self._session_scope_for_chat(cid, wsid or str(getattr(agent, "workspace_id", "") or "")):
                agent.active_chat_id = cid
                refresh = getattr(agent, "_refresh_chat_record_from_disk", None)
                if callable(refresh):
                    refresh(cid)
                target = agent._find_chat_by_id(cid)
                if not target:
                    return False
                agent._apply_chat_model_from_entry(target, persist_if_missing=True)
                agent._set_reasoning_effort(str(reasoning or "").strip())
                _mark_dirty = getattr(agent, "_mark_chat_dirty", None)
                if callable(_mark_dirty):
                    _mark_dirty(cid)
                try:
                    agent._save_chat_state()
                except Exception:
                    pass
        except Exception:
            return False
        finally:
            if switched:
                try:
                    from ..controllers.workspace_command_controller import (
                        workspace_switch_command,
                    )

                    with contextlib.redirect_stdout(io.StringIO()):
                        workspace_switch_command(agent, original_wsid)
                except Exception:
                    pass
        self.broadcaster.publish(
            "state",
            self._route(chat_id=cid, state=_build_state(agent)),
        )
        return True

    def rename_chat(self, chat_id: str, name: str, workspace_id: str = "") -> bool:
        """Persist a chat name for one GUI chat directly (no slash command).

        Unlike the ``/chat rename`` slash-command path this endpoint is
        workspace-scoped (chat ids repeat across workspaces) and executes
        immediately even while another chat's task is running, so the rename
        can never be routed to a same-id chat in the focused workspace.
        """
        agent = self.agent
        cid = str(chat_id or "").strip()
        new_name = str(name or "").strip()
        wsid = str(workspace_id or "").strip()
        if not cid or not new_name:
            return False
        original_wsid = str(getattr(agent, "workspace_id", "") or "").strip()
        switched = False
        try:
            if wsid and wsid != original_wsid:
                from ..controllers.workspace_command_controller import (
                    workspace_switch_command,
                )

                with contextlib.redirect_stdout(io.StringIO()):
                    workspace_switch_command(agent, wsid)
                switched = True
        except Exception:
            if switched:
                try:
                    from ..controllers.workspace_command_controller import (
                        workspace_switch_command,
                    )

                    with contextlib.redirect_stdout(io.StringIO()):
                        workspace_switch_command(agent, original_wsid)
                except Exception:
                    pass
            return False
        try:
            with self._session_scope_for_chat(
                cid, wsid or str(getattr(agent, "workspace_id", "") or "")
            ):
                agent.active_chat_id = cid
                target = agent._find_chat_by_id(cid)
                if not target:
                    return False
                target["name"] = new_name
                target["name_source"] = "manual"
                target["updated_at"] = datetime.datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if str(target.get("id") or "") == agent.active_chat_id:
                    agent.active_chat_name = new_name
                _mark_dirty = getattr(agent, "_mark_chat_dirty", None)
                if callable(_mark_dirty):
                    _mark_dirty(cid)
                try:
                    agent._save_chat_state()
                except Exception:
                    pass
        except Exception:
            return False
        finally:
            if switched:
                try:
                    from ..controllers.workspace_command_controller import (
                        workspace_switch_command,
                    )

                    with contextlib.redirect_stdout(io.StringIO()):
                        workspace_switch_command(agent, original_wsid)
                except Exception:
                    pass
        self.broadcaster.publish(
            "state",
            self._route(chat_id=cid, state=_build_state(agent)),
        )
        return True

    def _switch_to_workspace_safe(self, workspace_id: str) -> bool:
        """Switch the agent's focused workspace to ``workspace_id``.

        Returns ``True`` when the switch succeeded or was a no-op (same
        workspace already focused). GUI endpoints that must address a chat in
        a specific workspace (chat ids repeat across workspaces) switch here
        first, then restore with :meth:`_restore_workspace`.
        """
        agent = self.agent
        target = str(workspace_id or "").strip()
        if not target:
            return True
        try:
            current = str(getattr(agent, "workspace_id", "") or "").strip()
        except Exception:
            return False
        if target == current:
            return True
        try:
            from ..controllers.workspace_command_controller import (
                workspace_switch_command,
            )

            with contextlib.redirect_stdout(io.StringIO()):
                workspace_switch_command(agent, target)
        except Exception:
            return False
        return True

    def _restore_workspace(self, workspace_id: str) -> None:
        """Best-effort restore of the previously focused workspace."""
        agent = self.agent
        target = str(workspace_id or "").strip()
        if not target:
            return
        try:
            from ..controllers.workspace_command_controller import (
                workspace_switch_command,
            )

            with contextlib.redirect_stdout(io.StringIO()):
                workspace_switch_command(agent, target)
        except Exception:
            pass

    def chat_fork(
        self, chat_id: str = "", workspace_id: str = "", index: int = -1
    ) -> Dict[str, Any]:
        """Fork a chat at a user-message index (GUI "Fork" button).

        Dedicated equivalent of the TUI ``/chat fork`` slash command: it runs
        the same controller logic but is workspace-scoped (chat ids repeat
        across workspaces) and executes immediately on the HTTP thread instead
        of being queued through the slash-command input path. The active chat
        is switched to the new fork so the GUI's state event reloads its
        transcript.
        """
        agent = self.agent
        cid, wsid = self._resolve_chat_scope(chat_id, workspace_id)
        if not cid:
            return {"ok": False}
        original_wsid = str(getattr(agent, "workspace_id", "") or "").strip()
        if not self._switch_to_workspace_safe(wsid or original_wsid):
            return {"ok": False}
        new_id = ""
        try:
            with self._session_scope_for_chat(cid, wsid or original_wsid):
                agent.active_chat_id = cid
                refresh = getattr(agent, "_refresh_chat_record_from_disk", None)
                if callable(refresh):
                    refresh(cid)
                from ..controllers.chat_command_controller import (
                    handle_chat_fork_command,
                )

                with contextlib.redirect_stdout(io.StringIO()):
                    handle_chat_fork_command(
                        agent, str(index if index is not None else -1)
                    )
                try:
                    new_id = str(agent._chat_state.get("active") or "")
                except Exception:
                    pass
        except Exception:
            return {"ok": False}
        finally:
            self._restore_workspace(original_wsid)
        if not new_id or new_id == cid:
            return {"ok": False}
        self.broadcaster.publish(
            "state", self._route(state=_build_state(agent))
        )
        return {"ok": True, "chatId": new_id}

    def chat_new_from_compact(
        self,
        chat_id: str = "",
        workspace_id: str = "",
        first_message: str = "",
    ) -> Dict[str, Any]:
        """Create a new chat seeded with a context-compaction summary (GUI
        "new chat from compact summary" button).

        Mirrors :meth:`chat_fork` (workspace-scoped, runs on the HTTP thread)
        but the new chat is EMPTY of prior turns: only ``first_message`` (the
        compact-summary body) is recorded as its first message in the SAME
        wire format a real compaction produces: an ``assistant`` message whose
        content is the ``[CONTEXT_COMPACTION_SUMMARY]`` JSON payload (role and
        format preserved "as-is" so the GUI renders it as a compact-notice
        turn and the model later receives it as an ``assistant``
        ``[Context summary]`` message — never as a user prompt). The model is
        intentionally NOT invoked — the user continues the conversation from
        there. The new chat is named after the source chat with a unique
        numeric suffix (``name (2)``, ``name (3)``, ...) via the same
        ``_unique_fork_chat_name`` helper the fork command uses, so repeated
        clicks never collide. The active chat is switched to the new chat so
        the GUI's state event reloads its transcript.
        """
        agent = self.agent
        cid, wsid = self._resolve_chat_scope(chat_id, workspace_id)
        if not cid:
            return {"ok": False}
        original_wsid = str(getattr(agent, "workspace_id", "") or "").strip()
        if not self._switch_to_workspace_safe(wsid or original_wsid):
            return {"ok": False}
        new_id = ""
        try:
            with self._session_scope_for_chat(cid, wsid or original_wsid):
                agent.active_chat_id = cid
                refresh = getattr(agent, "_refresh_chat_record_from_disk", None)
                if callable(refresh):
                    refresh(cid)
                from ..controllers.chat_command_controller import (
                    _unique_fork_chat_name,
                )

                source = agent._find_chat_by_id(cid)
                if not source:
                    return {"ok": False}
                base_name = str(
                    source.get("name")
                    or getattr(agent, "active_chat_name", "")
                    or ""
                )
                if not base_name:
                    from ..core.localization import (
                        get_display_language,
                        translate,
                    )

                    base_name = translate(
                        "chat.new.default_name", get_display_language(agent)
                    )
                new_name = _unique_fork_chat_name(agent, base_name)
                new_id = agent._next_chat_id()
                entry = agent._new_chat_entry(new_id, name=new_name)
                entry["name_source"] = "manual"
                message_text = str(first_message or "").strip()
                if message_text:
                    created_at = datetime.datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    # Inherit the compaction mode from the source chat's most
                    # recent summary (best-effort) so the banner title in the
                    # new chat matches what the user saw; defaults to manual.
                    mode = "manual"
                    sms = getattr(agent, "session_memory_service", None)
                    try:
                        if sms is not None:
                            parse = getattr(
                                sms, "parse_context_compaction_summary_content", None
                            )
                            if callable(parse):
                                for _m in reversed(list(source.get("messages") or [])):
                                    if not isinstance(_m, dict):
                                        continue
                                    _p = parse(str(_m.get("content") or ""))
                                    if isinstance(_p, dict):
                                        mode = str(_p.get("mode") or "") or "manual"
                                        break
                    except Exception:
                        mode = "manual"
                    payload = {
                        "kind": "context_compaction_summary",
                        "summary": message_text,
                        "mode": mode,
                        "created_at": created_at,
                    }
                    entry["messages"] = [
                        {
                            "role": "assistant",
                            "content": CONTEXT_COMPACTION_SUMMARY_PREFIX
                            + json.dumps(payload, ensure_ascii=False),
                            "created_at": created_at,
                        }
                    ]
                entry["model_provider"] = str(source.get("model_provider") or "")
                entry["model_name"] = str(source.get("model_name") or "")
                entry["reasoning_level"] = str(source.get("reasoning_level") or "")
                agent._chat_entries().append(entry)
                agent._chat_state["active"] = new_id
                agent._save_chat_state()
                # The user is now composing in this new chat, so record it as
                # the focused chat (mirrors new_chat): without this the focus
                # marker still points at the previously opened chat and the
                # new chat's first completed turn is misclassified as a
                # background completion, leaving a persistent unread dot.
                track = getattr(self, "_track_focus", None)
                if callable(track):
                    track(new_id, wsid or None)
        except Exception:
            return {"ok": False}
        finally:
            self._restore_workspace(original_wsid)
        if not new_id or new_id == cid:
            return {"ok": False}
        self.broadcaster.publish(
            "state", self._route(state=_build_state(agent))
        )
        return {"ok": True, "chatId": new_id}

    def chat_edit(
        self, chat_id: str = "", workspace_id: str = "", index: int = -1
    ) -> bool:
        """Truncate a chat at a user-message index (GUI "Edit" button).

        Dedicated equivalent of the TUI ``/chat edit`` slash command. The
        target chat is interrupted first (scoped to that chat only, exactly
        like the slash path in ``submit_input``), then the history is rewound
        to just before the selected user message.
        """
        agent = self.agent
        cid, wsid = self._resolve_chat_scope(chat_id, workspace_id)
        if not cid:
            return False
        original_wsid = str(getattr(agent, "workspace_id", "") or "").strip()
        if not self._switch_to_workspace_safe(wsid or original_wsid):
            return False
        self.interrupt(chat_id=cid, workspace_id=wsid or original_wsid)
        try:
            with self._session_scope_for_chat(cid, wsid or original_wsid):
                agent.active_chat_id = cid
                refresh = getattr(agent, "_refresh_chat_record_from_disk", None)
                if callable(refresh):
                    refresh(cid)
                from ..controllers.chat_command_controller import (
                    handle_chat_edit_command,
                )

                with contextlib.redirect_stdout(io.StringIO()):
                    handle_chat_edit_command(
                        agent, str(index if index is not None else -1)
                    )
        except Exception:
            return False
        finally:
            self._restore_workspace(original_wsid)
        # The chat id is unchanged by an edit, so the frontend cannot rely on
        # its active-chat history effect; it flags the next idle event to
        # reload the (now shorter) transcript (see ``editChat`` in AppContext).
        self.broadcaster.publish(
            "idle", self._route(state=_build_state(agent))
        )
        return True

    def set_execution_policy(self, policy: str) -> bool:
        """Apply an execution-policy change immediately (GUI security dropdown).

        Dedicated equivalent of the ``/execution-policy`` slash command; runs
        on the HTTP thread so the new policy takes effect even while a
        multi-round task is executing.
        """
        agent = self.agent
        value = str(policy or "").strip().lower()
        if value not in ("unlimited", "moderate", "confirmation"):
            return False
        try:
            if value != str(getattr(agent, "execution_policy", "")).lower():
                agent.execution_policy = value
                save = getattr(agent, "_save_execution_policy_to_config", None)
                if callable(save):
                    save()
        except Exception:
            return False
        self.broadcaster.publish(
            "state", self._route(state=_build_state(agent))
        )
        return True

    def workspace_create(self, path: str) -> Dict[str, Any]:
        """Create + switch to a workspace from a directory path (GUI picker).

        Dedicated equivalent of the TUI ``/workspace create`` slash command.
        The controller is invoked directly (not through the slash-command
        queue) and the new workspace id is returned so the frontend can focus
        it immediately.
        """
        agent = self.agent
        raw = str(path or "").strip()
        if not raw:
            return {"ok": False}
        try:
            before = set(agent._workspaces_state.get("workspaces", {}).keys())
        except Exception:
            before = set()
        quoted = '"' + str(raw).replace('"', '\\"') + '"'
        text = ""
        try:
            from ..controllers.workspace_command_controller import (
                workspace_create_command,
            )

            with contextlib.redirect_stdout(io.StringIO()):
                text = workspace_create_command(agent, quoted)
        except Exception:
            return {"ok": False, "text": text}
        try:
            after = set(agent._workspaces_state.get("workspaces", {}).keys())
        except Exception:
            after = set()
        new_ids = after - before
        if not new_ids:
            return {"ok": False, "text": text}
        new_id = sorted(new_ids)[0]
        # Publish an idle event (not just state) exactly like the proven
        # ``open_folder`` path: the frontend consumes it (once the pending
        # focus is set) to enter draft mode and refresh the workspace list,
        # independent of the createWorkspace fetch round-trip.
        self.broadcaster.publish(
            "idle", self._route(state=_build_state(agent))
        )
        return {"ok": True, "id": new_id, "text": text}

    def workspace_rename(self, workspace_id: str, name: str) -> Dict[str, Any]:
        """Rename a workspace (GUI sidebar rename).

        Dedicated equivalent of the TUI ``/workspace rename`` slash command,
        invoked directly against the workspace controller so the change
        persists immediately without waiting for the slash-command queue.
        """
        agent = self.agent
        wid = str(workspace_id or "").strip()
        new_name = str(name or "").strip()
        if not wid or not new_name:
            return {"ok": False}
        quoted = (
            '"' + wid.replace('"', '\\"') + '" --name "'
            + new_name.replace('"', '\\"') + '"'
        )
        text = ""
        try:
            from ..controllers.workspace_command_controller import (
                workspace_update_command,
            )

            with contextlib.redirect_stdout(io.StringIO()):
                text = workspace_update_command(agent, quoted)
        except Exception:
            return {"ok": False, "text": text}
        try:
            entry = agent._workspace_entry_by_selector(wid)
        except Exception:
            entry = None
        if not entry or str(entry.get("name") or "") != new_name:
            return {"ok": False, "text": text}
        self.broadcaster.publish(
            "state", self._route(state=_build_state(agent))
        )
        return {"ok": True, "text": text}

    def _publish_idle_if_chat_parked(self, chat_id: str, workspace_id: str = "") -> None:
        """Publish an idle snapshot when a chat has no in-flight turn to stop.

        ``Stop`` / ``pause`` only set the cooperative interrupt flag; when the
        target chat's loop is already parked (waiting for the next input) that
        flag is never consumed and the loop emits no terminal ``idle`` event.
        A GUI that still believes the chat is running — e.g. the closing event
        of the last turn was lost over SSE, so its live turn never settled —
        would keep the spinner forever and make Stop appear unresponsive.
        Publishing the current state as an idle snapshot lets the frontend
        settle the stale live turn and reload history right away.

        When a real turn IS streaming, its loop emits the terminal idle itself
        once the interrupt unwinds the turn, so nothing is published here (an
        early idle with ``running=True`` would be ignored by the frontend's
        still-running guard anyway).
        """
        try:
            if self._chat_is_busy(str(chat_id or ""), workspace_id):
                return
            self.broadcaster.publish(
                "idle",
                self._route(chat_id=str(chat_id or ""), state=self.state()),
            )
        except Exception:
            pass

    def interrupt(self, chat_id: str = "", workspace_id: str = "") -> None:
        """Cancel an in-flight turn for the GUI's "stop" button / message edit.

        The agent loops run on per-chat worker threads, so the CLI's
        ``_thread.interrupt_main()`` path is unusable here (it would raise in
        the unrelated HTTP/main thread). Instead we set the cooperative task
        interrupt flag the loop polls on every model-stream chunk, tool-round
        boundary, and tool dispatch (see ``runtime_loop``), and terminate any
        running interruptible subprocess so the loop unwinds promptly.

        When ``chat_id`` is given the request is scoped to that single chat: it
        lands on the chat's :class:`SessionState` and its own process bucket, so
        only that chat's loop thread consumes it and only its subprocesses are
        terminated. Stopping / editing in one chat therefore never aborts a
        different chat's running task. Without ``chat_id`` the legacy
        agent-global path is used (TUI-era stop / no chat context).
        """
        agent = self.agent
        cid = str(chat_id or "").strip()
        if cid:
            # Validate the pair so a same-id chat in the wrong workspace is never
            # interrupted; an unresolvable pair is a safe no-op (never falls
            # back to the agent-global interrupt, which would abort other chats).
            cid, wsid = self._resolve_chat_scope(cid, workspace_id)
            if cid:
                try:
                    agent._request_chat_interrupt(cid, wsid)
                except Exception:
                    pass
                _parked_publish = getattr(self, "_publish_idle_if_chat_parked", None)
                if callable(_parked_publish):
                    _parked_publish(cid, wsid)
            return
        try:
            lock = getattr(agent, "_interrupt_state_lock", None)
            if lock is not None:
                with lock:
                    agent._task_interrupt_requested = True
            else:
                agent._task_interrupt_requested = True
        except Exception:
            pass
        for name in ("_mark_process_interrupt_requested", "_terminate_interruptible_processes"):
            try:
                fn = getattr(agent, name, None)
                if callable(fn):
                    fn()
            except Exception:
                pass

    def pause(self, chat_id: str = "", workspace_id: str = "") -> None:
        """Pause an in-flight turn for the GUI's "send immediately" queue-jump.

        Semantically identical to :meth:`interrupt` — the cooperative task
        interrupt flag is set and any running interruptible subprocess is
        terminated so the loop unwinds promptly — except that the terminated
        subprocesses are additionally marked as *paused*. The shell tool then
        reports that the user interrupted the call to supplement more
        information instead of the plain "command aborted by user" cancel
        notice (see ``_request_chat_pause`` / ``_shell_abort_notice``).
        """
        agent = self.agent
        cid = str(chat_id or "").strip()
        if cid:
            # Validate the pair so a same-id chat in the wrong workspace is
            # never paused; an unresolvable pair is a safe no-op.
            cid, wsid = self._resolve_chat_scope(cid, workspace_id)
            if cid:
                try:
                    agent._request_chat_pause(cid, wsid)
                except Exception:
                    pass
                _parked_publish = getattr(self, "_publish_idle_if_chat_parked", None)
                if callable(_parked_publish):
                    _parked_publish(cid, wsid)
            return
        try:
            lock = getattr(agent, "_interrupt_state_lock", None)
            if lock is not None:
                with lock:
                    agent._task_interrupt_requested = True
            else:
                agent._task_interrupt_requested = True
        except Exception:
            pass
        for name in ("_mark_process_interrupt_requested", "_terminate_interruptible_processes"):
            try:
                fn = getattr(agent, name, None)
                if callable(fn):
                    fn()
            except Exception:
                pass

    def save_pending_inputs(self, chat_id: str, ws_id: str, inputs: List[str]) -> bool:
        """Persist pending input queue for a chat so it survives a restart."""
        cid = str(chat_id or "").strip()
        if not cid:
            return False
        mgr = getattr(self.agent, "_chat_state_manager", None)
        if mgr is None:
            return False
        ws = str(ws_id or "").strip()
        if ws and ws != str(getattr(self.agent, "workspace_id", "") or "").strip():
            ctx = self._persist_ctx_for_workspace(ws)
            if ctx:
                return mgr.save_pending_inputs(cid, inputs)
            return False
        return mgr.save_pending_inputs(cid, inputs)

    def compact_context(
        self, chat_id: str = "", workspace_id: str = ""
    ) -> Dict[str, Any]:
        """Trigger manual context compaction via the session memory service.

        Bridge output on the HTTP thread is suppressed so the compaction's
        TUI-oriented printed banners do not create spurious SSE ``output`` events.
        The localized ``compaction.no_context`` message is returned in the response
        so the frontend can render it as a sidebar-style notification (no turn
        lifecycle, no history interference).

        ``chat_id`` + ``workspace_id`` identify the chat to compact explicitly
        (chat ids repeat across workspaces); empty values fall back to the
        focused chat.
        """
        from ..core.localization import get_display_language, translate

        agent = self.agent
        svc = getattr(agent, "session_memory_service", None)
        compact_fn = getattr(svc, "compact_context", None) if svc else None
        focus_chat, wsid = self._resolve_chat_scope(chat_id, workspace_id)
        if callable(compact_fn):
            bridge = getattr(self, "_bridge", None)
            prev_suppressed = getattr(bridge, "suppressed", False) if bridge is not None else False
            if bridge is not None:
                bridge.suppressed = True
            try:
                if focus_chat:
                    with self._session_scope_for_chat(focus_chat, wsid):
                        ok = compact_fn(mode="manual")
                else:
                    ok = compact_fn(mode="manual")
            finally:
                if bridge is not None:
                    bridge.suppressed = prev_suppressed
            if not ok:
                if focus_chat:
                    with self._session_scope_for_chat(focus_chat, wsid):
                        lang = get_display_language(agent)
                        msg = translate("compaction.no_context", lang)
                else:
                    lang = get_display_language(agent)
                    msg = translate("compaction.no_context", lang)
                return {"ok": False, "text": msg}
            return {"ok": True}
        return {"ok": False, "error": "compaction unavailable"}

    def state(self) -> Dict[str, Any]:
        return _build_state(self.agent)

    def index_status(self) -> Dict[str, Any]:
        agent = self.agent
        try:
            from ..config.rg_downloader import (
                RG_STATUS_IDLE,
                get_rg_status,
                get_rg_status_message,
            )

            rg_status = get_rg_status()
            rg_data: Dict[str, Any] = {"rg_status": rg_status}

            if rg_status == RG_STATUS_IDLE:
                rg_data["rg_message"] = ""
            else:
                rg_data["rg_message"] = get_rg_status_message()

            idx = getattr(agent, "_project_context_index", None)
            if idx is None:
                result = {"hidden": True}
                result.update(rg_data)
                return result

            # Aggregate across every workspace's index.  ``status()`` sums the
            # file counts and reports the most active refresh phase, so the GUI
            # status bar reflects all workspaces instead of only the focused
            # one.
            st = idx.status()
            per_workspace: List[Dict[str, Any]] = []
            try:
                raw = agent._workspaces_state.get("workspaces", {})
                default_ws_id = str(getattr(agent, "workspace_id", ""))
            except Exception:
                raw = {}
                default_ws_id = ""
            if isinstance(raw, dict):
                for entry in raw.values():
                    if not isinstance(entry, dict):
                        continue
                    ws_id = str(entry.get("id") or "")
                    try:
                        root = str(agent._workspace_root_path(entry))
                        storage = agent._workspace_storage_path(entry) / "indexes"
                    except Exception:
                        root = str(entry.get("root") or "")
                        storage = None
                    is_default = (
                        str(entry.get("kind") or "").lower() == "default"
                        or (bool(default_ws_id) and ws_id == default_ws_id)
                    )
                    ws_st = idx.status_for_storage(storage) if storage is not None else None
                    per_workspace.append(
                        {
                            "id": ws_id,
                            "name": str(entry.get("name") or ""),
                            "root": root,
                            "is_default": is_default,
                            "files_total": int((ws_st or {}).get("files_total", 0) or 0),
                            "refresh_phase": str((ws_st or {}).get("refresh_phase", "") or ""),
                            "refresh_progress_total": int((ws_st or {}).get("refresh_progress_total", 0) or 0),
                            "refresh_progress_done": int((ws_st or {}).get("refresh_progress_done", 0) or 0),
                            "refresh_progress_percent": int((ws_st or {}).get("refresh_progress_percent", 0) or 0),
                        }
                    )
            result = {
                "hidden": False,
                "files_total": int(st.get("files_total", 0)),
                "workspace_name": str(getattr(agent, "workspace_name", "") or ""),
                "is_default_workspace": str(getattr(agent, "workspace_id", "")) == "default",
                "refresh_phase": str(st.get("refresh_phase", "") or ""),
                "refresh_progress_total": int(st.get("refresh_progress_total", 0)),
                "refresh_progress_done": int(st.get("refresh_progress_done", 0)),
                "refresh_progress_percent": int(st.get("refresh_progress_percent", 0)),
                "workspaces": per_workspace,
            }
            result.update(rg_data)
            return result
        except Exception:
            return {"hidden": True, "rg_status": "", "rg_message": ""}

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

    def chat_history(
        self,
        before: Optional[int],
        limit: int,
        chat_id: str = "",
        workspace_id: str = "",
    ) -> Dict[str, Any]:
        """Return a paginated slice of structured turns for a chat.

        ``before`` is the exclusive end index (0-based among turns); ``None``
        means "from the end". The newest ``limit`` turns up to ``before`` are
        returned along with ``start`` (the index of the first returned turn) and
        the overall ``total`` count, so the GUI can lazily load older turns.

        ``chat_id`` + ``workspace_id`` identify the chat explicitly (chat ids
        repeat across workspaces); empty values fall back to the focused chat.
        """
        # Reading history happens on an HTTP handler thread; bind it to the
        # requested chat so the per-session conversation_history resolves to
        # that chat's live session (workspace-qualified). The pair is validated
        # so a same-id chat in the wrong workspace is never read.
        focus_chat, wsid = self._resolve_chat_scope(chat_id, workspace_id)
        try:
            try:
                idx = getattr(self.agent, "_project_context_index", None)
                if idx is not None and hasattr(idx, "request_yield"):
                    idx.request_yield()
            except Exception:
                pass
            # When the chat is not actively streaming a turn here (no busy
            # runtime), pull the latest record from disk before building turns
            # so messages, ``pending_request_user_input`` markers and plan
            # updates written by a peer process (e.g. another codewood TUI) are
            # reflected. An idle parked runtime no longer blocks the refresh,
            # which is what lets the GUI pick up a changed history on
            # switch/reload. Only refresh when the requested workspace matches
            # the focused one (the common case) — a background workspace's
            # record lives under its own index and would not resolve here.
            #
            # EXCEPTION: a turn that JUST finished in this process keeps its
            # in-memory session authoritative for a short window. The idle SSE
            # event triggers an immediate history reload, and the disk record
            # may still lag the final persistence flush — rolling the session
            # back to disk there would make the reload surface a truncated
            # transcript (the task's messages vanish until the chat is
            # clicked again, when the flush has landed). A long-parked
            # runtime (window elapsed) is refreshable again.
            is_busy = self._chat_is_busy(focus_chat, wsid or None)
            recent_finish = self._chat_runtime_recently_finished(
                focus_chat, wsid or None,
            )
            _t_h0 = time.perf_counter()
            current_ws = str(getattr(self.agent, "workspace_id", "") or "").strip()
            same_ws = (not wsid) or (wsid == current_ws)
            if focus_chat and not is_busy and not recent_finish and same_ws:
                try:
                    refresh = getattr(self.agent, "_refresh_chat_record_from_disk", None)
                    if callable(refresh):
                        refresh(focus_chat)
                        _WORKSPACE_ROUTE_LOGGER.debug(
                            "ws-switch-timing get_chat_history_refresh=%.3fs chat=%s",
                            time.perf_counter() - _t_h0,
                            str(focus_chat or ""),
                        )
                        # Rebind the session so conversation_history reflects
                        # the freshly re-validated chat dict. Skip the rebind
                        # when this chat is already the active one (e.g. the
                        # GUI just switched to it via select_chat, which
                        # activated it): re-activating a large chat re-runs
                        # history reconciliation and plan scanning, which can
                        # cost ~1s per loadChatHistory request.
                        active_id = str(
                            getattr(self.agent, "active_chat_id", "") or ""
                        ).strip()
                        if active_id != str(focus_chat or "").strip():
                            self.agent._activate_chat(
                                focus_chat,
                                announce=False,
                                clear_screen=False,
                                print_history=False,
                                persist=False,
                            )
                except Exception:
                    pass
            # Lazily load this chat's file_changes.json sidecar so the
            # [FILE_CHANGE_REF] markers in the history resolve to real diff
            # summaries — done per chat, only for the chat being opened.
            if focus_chat:
                _t_h1 = time.perf_counter()
                try:
                    self._ensure_chat_file_changes_loaded(focus_chat, wsid)
                except Exception:
                    pass
                _WORKSPACE_ROUTE_LOGGER.debug(
                    "ws-switch-timing get_chat_history_file_changes=%.3fs chat=%s",
                    time.perf_counter() - _t_h1,
                    str(focus_chat or ""),
                )
            _t_h2 = time.perf_counter()
            with self._session_scope_for_chat(focus_chat, wsid):
                turns = self._cached_structured_turns(focus_chat)
            _WORKSPACE_ROUTE_LOGGER.debug(
                "ws-switch-timing get_chat_history_turns=%.3fs chat=%s",
                time.perf_counter() - _t_h2,
                str(focus_chat or ""),
            )
        except Exception:
            turns = []
        _WORKSPACE_ROUTE_LOGGER.debug(
            "ws-switch-timing get_chat_history chat=%s ws=%s msgs=%d turns=%d",
            str(focus_chat or ""),
            str(wsid or ""),
            len(list(getattr(self.agent, "conversation_history", None) or [])),
            len(turns),
        )
        total = len(turns)
        if limit <= 0:
            limit = 12
        if before is None or before < 0 or before > total:
            end = total
        else:
            end = before
        start = max(0, end - limit)
        return {"turns": turns[start:end], "start": start, "total": total}

    def _cached_structured_turns(self, chat_id: str) -> List[Dict[str, Any]]:
        """Return structured turns for the focused chat, cached by content fingerprint.

        Building turns runs per-message regex rendering over the whole history,
        which for a multi-thousand-message chat costs ~1s per call. The GUI
        history endpoint re-enters this on every ``loadChatHistory`` request
        (initial load, pagination, repeated focus events), so cache the result
        keyed by the chat identity plus a cheap content fingerprint (message
        count, chat ``updated_at``, last message timestamp). Any edit or new
        message bumps one of these and rebuilds the cache.

        Safety: this is a read-only memoization — it never writes to disk and
        never mutates chat records. A lazy placeholder (not yet hydrated) is
        deliberately NOT cached, so a transient empty history can never be
        memoized under a real chat's fingerprint.
        """
        agent = self.agent
        try:
            with agent._chat_state_lock:
                chat = agent._chat_state_manager.find_chat_by_id(chat_id)
                if chat is None:
                    return _build_structured_turns(agent)
                placeholder = bool(chat.get("_lazy_placeholder", False))
                updated_at = str(chat.get("updated_at") or "")
        except Exception:
            placeholder = True
            updated_at = ""
        history = list(getattr(agent, "conversation_history", None) or [])
        last_ts = ""
        if history:
            last = history[-1]
            if isinstance(last, dict):
                last_ts = str(last.get("created_at") or "")
        key = (
            str(getattr(agent, "workspace_id", "") or "").strip(),
            str(chat_id or "").strip(),
            len(history),
            updated_at,
            last_ts,
        )
        cache = getattr(self, "_turns_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._turns_cache = cache
        if not placeholder:
            cached = cache.get(key)
            if cached is not None:
                return cached
        # Share an in-flight build for the same key: the GUI's first
        # loadChatHistory races the background pre-warm started by
        # select_chat. Without this, both threads re-render the whole
        # history (~1s) instead of one building and the other waiting.
        inflight = getattr(self, "_turns_build_inflight", None)
        if not isinstance(inflight, dict):
            inflight = {}
            self._turns_build_inflight = inflight
        evt = inflight.get(key)
        if evt is not None:
            evt.wait(timeout=5.0)
            cached = cache.get(key)
            if cached is not None:
                return cached
        if placeholder:
            return _build_structured_turns(agent)
        evt = threading.Event()
        inflight[key] = evt
        try:
            turns = _build_structured_turns(agent)
            if len(cache) >= 64:
                cache.clear()
            cache[key] = turns
            return turns
        finally:
            inflight.pop(key, None)
            evt.set()

    def export_chat(
        self, chat_id: str, file_path: str, workspace_id: str = ""
    ) -> bool:
        """Export a chat transcript as plain-text markdown to ``file_path``.
        Only includes content that is always visible in the GUI — user messages,
        assistant text replies, and user selections — filtering out collapsed
        sections (tool calls/results, thinking blocks) and internal bookkeeping.
        """
        import io
        from datetime import datetime

        agent = self.agent
        try:
            wsid = str(workspace_id or "").strip()
            tls = agent.__dict__.get("_session_tls")
            prev_chat = str(getattr(tls, "chat_id", "") or "") if tls is not None else ""
            prev_session = getattr(tls, "session", None) if tls is not None else None
            agent._bind_session(chat_id, wsid if wsid else None)
            try:
                turns = _build_structured_turns(agent)
            finally:
                tls2 = agent.__dict__.get("_session_tls")
                if tls2 is not None:
                    tls2.chat_id = prev_chat
                    tls2.session = prev_session
            if not turns:
                return False

            buf = io.StringIO()
            buf.write("# Chat Transcript\n\n")
            buf.write(
                f"*Exported on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n\n"
            )
            buf.write("---\n\n")

            for turn in turns:
                user_text = str(turn.get("userText") or "")
                ts = str(turn.get("timestamp") or "")

                if user_text:
                    header = "**User**"
                    if ts:
                        header += f" · {ts}"
                    buf.write(f"{header}\n\n{user_text}\n\n---\n\n")

                for rnd in turn.get("rounds", []):
                    rnd_text = str(rnd.get("text") or "").strip()
                    rnd_selection = str(rnd.get("selection") or "").strip()

                    if rnd_selection:
                        buf.write(f"**Selection**\n\n{rnd_selection}\n\n---\n\n")
                    elif rnd_text:
                        buf.write(f"**Assistant**\n\n{rnd_text}\n\n---\n\n")

            Path(file_path).write_text(buf.getvalue(), encoding="utf-8")
            return True
        except Exception:
            return False

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
        import time as _time

        _t0 = _time.perf_counter()

        cid = str(chat_id or "").strip()
        wsid = str(workspace_id or "").strip()
        if not cid and not wsid:
            return False
        agent = self.agent
        _WORKSPACE_ROUTE_LOGGER.debug(
            "select-chat request target_ws=%s target_chat=%s current_ws=%s current_chat=%s",
            wsid,
            cid,
            str(getattr(agent, "workspace_id", "") or ""),
            str(getattr(agent, "active_chat_id", "") or ""),
        )
        # Ask the project-context indexer to yield the GIL so this HTTP
        # handler can acquire it promptly (cooperative yield).
        try:
            idx = getattr(agent, "_project_context_index", None)
            if idx is not None and hasattr(idx, "request_yield"):
                idx.request_yield()
        except Exception:
            pass
        try:
            if wsid and wsid != str(getattr(agent, "workspace_id", "") or ""):
                try:
                    # Do not call sync/save in this HTTP request's ambient
                    # session.  It may retain a background chat id while the
                    # agent globals already identify another workspace, which
                    # writes a same-id chat into that other workspace's index.
                    # Persist every running runtime through its captured
                    # workspace-qualified scope instead.
                    with self._runtimes_lock:
                        running = [
                            rt for rt in self._runtimes.values()
                            if rt.busy.is_set()
                        ]
                    for runtime in running:
                        with self._runtime_persistence_scope(runtime):
                            sync = getattr(agent, "_sync_active_chat_messages", None)
                            if callable(sync):
                                sync()
                            save = getattr(agent, "_save_chat_state", None)
                            if callable(save):
                                save()
                except Exception:
                    pass
                _t1 = _time.perf_counter()
                _WORKSPACE_ROUTE_LOGGER.debug("ws-switch-timing select_chat_busy_save=%.3fs", _t1 - _t0)

                from ..controllers.workspace_command_controller import (
                    workspace_switch_command,
                )

                with contextlib.redirect_stdout(io.StringIO()):
                    # The GUI is switching to a known chat (or intentionally
                    # to an empty workspace draft).  Do not manufacture a
                    # default ``chat-1`` while loading the target workspace:
                    # ids repeat per workspace, and that transient default
                    # becomes a persistent stray chat before the requested
                    # chat is activated.
                    workspace_switch_command(
                        agent, wsid, create_default_chat=False, lazy_records=True
                    )
                _t2 = _time.perf_counter()
                _WORKSPACE_ROUTE_LOGGER.debug("ws-switch-timing select_chat_switch=%.3fs", _t2 - _t1)

                _WORKSPACE_ROUTE_LOGGER.debug(
                    "select-chat switched target_ws=%s actual_ws=%s active_chat=%s chats=%s",
                    wsid,
                    str(getattr(agent, "workspace_id", "") or ""),
                    str(getattr(agent, "active_chat_id", "") or ""),
                    [
                        (str(entry.get("id") or ""), str(entry.get("name") or ""))
                        for entry in (getattr(agent, "_chat_entries", lambda: [])() or [])
                        if isinstance(entry, dict)
                    ],
                )

                _t_ws0 = _time.perf_counter()
                with self._ws_persist_lock:
                    self._ws_persist_ctx.clear()
                _WORKSPACE_ROUTE_LOGGER.debug(
                    "ws-switch-timing select_chat_ws_ctx_clear=%.3fs",
                    _time.perf_counter() - _t_ws0,
                )
            if cid:
                _t_cid = _time.perf_counter()
                _t_lock0 = _time.perf_counter()
                with agent._chat_state_lock:
                    target = agent._resolve_chat_selector(cid)
                    rid = str(target.get("id") or "") if target else ""
                _WORKSPACE_ROUTE_LOGGER.debug(
                    "ws-switch-timing select_chat_resolve_lock=%.3fs",
                    _time.perf_counter() - _t_lock0,
                )
                if not rid:
                    _WORKSPACE_ROUTE_LOGGER.warning(
                        "select-chat missing target target_ws=%s target_chat=%s actual_ws=%s",
                        wsid,
                        cid,
                        str(getattr(agent, "workspace_id", "") or ""),
                    )
                    return False
                _WORKSPACE_ROUTE_LOGGER.debug(
                    "select-chat resolved target_ws=%s requested_chat=%s resolved_chat=%s name=%r busy=%s",
                    wsid,
                    cid,
                    rid,
                    str((target or {}).get("name") or ""),
                    self._chat_is_busy(rid),
                )
                track = getattr(self, "_track_focus", None)
                if callable(track):
                    track(rid, wsid)
                if self._chat_is_busy(rid):
                    with agent._chat_state_lock:
                        agent._chat_state["active"] = rid
                        agent.active_chat_id = rid
                        agent.active_chat_name = str(
                            (target or {}).get("name") or "New Chat"
                        )
                        agent._save_chat_state()
                else:
                    try:
                        refresh = getattr(agent, "_refresh_chat_record_from_disk", None)
                        if callable(refresh):
                            refresh(rid)
                    except Exception:
                        pass
                    # Pre-warm the structured-turns memo in the background as
                    # soon as the record is in memory (the record now carries
                    # the full history) so the GUI's first loadChatHistory —
                    # which races select_chat and cold-builds ~1s of regex
                    # rendering over a multi-thousand-message history — hits
                    # the memoized cache instead of waiting on a fresh build.
                    try:
                        threading.Thread(
                            target=self._prewarm_turns_cache,
                            args=(
                                rid,
                                wsid or str(getattr(agent, "workspace_id", "") or ""),
                            ),
                            daemon=True,
                        ).start()
                    except Exception:
                        pass
                    try:
                        _prev_index_active = str(
                            agent._chat_state.get("active") or ""
                        ).strip() if isinstance(getattr(agent, "_chat_state", None), dict) else ""
                    except Exception:
                        _prev_index_active = ""
                    result = agent._activate_chat(
                        rid, announce=False, clear_screen=False, print_history=False, persist=False
                    )
                    if result:
                        return False
                    _t3 = _time.perf_counter()
                    _WORKSPACE_ROUTE_LOGGER.debug("ws-switch-timing select_chat_activate=%.3fs", _t3 - _t_cid)
                    try:
                        # The activate just bound this chat's session. When the
                        # index already pointed at it (``_prev_index_active``)
                        # and nothing was marked dirty, the record + index are
                        # already current on disk (refresh() reloaded the
                        # record above), so the full save is redundant — it
                        # re-serializes multi-MB active-chat records
                        # (~0.4-0.6s) just to compare byte-identical content.
                        # Any real change goes through a tracked path (sync
                        # bumps ``updated_at``/dirty, unread/edits mark dirty),
                        # and those paths persist immediately.
                        _needs_save = True
                        try:
                            _mgr = getattr(agent, "_chat_state_manager", None)
                            _dirty = _mgr is not None and (
                                _mgr._dirty_key(rid) in _mgr._dirty_ids()
                            )
                            _needs_save = (_prev_index_active != rid) or _dirty
                        except Exception:
                            _needs_save = True
                        if _needs_save:
                            _t_save = _time.perf_counter()
                            save = getattr(agent, "_save_chat_state", None)
                            if callable(save):
                                save()
                            _WORKSPACE_ROUTE_LOGGER.debug(
                                "ws-switch-timing select_chat_post_activate_save=%.3fs",
                                _time.perf_counter() - _t_save,
                            )
                        else:
                            _WORKSPACE_ROUTE_LOGGER.debug(
                                "ws-switch-timing select_chat_post_activate_save=skipped"
                            )
                    except Exception:
                        pass
                # The user opened this chat — its unread blue dot is cleared.
                # Done for both the busy (focus-only) and idle (full activate)
                # branches; the next state broadcast carries the cleared flag.
                try:
                    setter = getattr(agent, "_set_chat_unread", None)
                    if callable(setter):
                        setter(rid, False)
                    else:
                        manager = getattr(agent, "_chat_state_manager", None)
                        if manager is not None:
                            mset = getattr(manager, "set_chat_unread", None)
                            if callable(mset):
                                mset(rid, False)
                except Exception:
                    pass
        except Exception:
            return False
        _t_end = _time.perf_counter()
        _WORKSPACE_ROUTE_LOGGER.debug("ws-switch-timing select_chat_total=%.3fs", _t_end - _t0)
        threading.Thread(
            target=lambda: self.broadcaster.publish(
                "state", self._route(state=_build_state(agent))
            ),
            daemon=True,
        ).start()
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
            # Apply as a runtime override so backend-produced text (tool-call
            # labels, confirm prompts, system prompt) immediately follows the
            # GUI locale. This is session-only and never persisted as the TUI's
            # display_language.
            try:
                from ..core.localization import normalize_display_language

                agent._gui_language_override = normalize_display_language(value) or value
            except Exception:
                agent._gui_language_override = value
        except Exception:
            return False
        # Push a fresh state snapshot so the webview immediately re-renders
        # with the new locale without waiting for the next agent tick.
        try:
            self.broadcaster.publish(
                "idle", self._route(state=_build_state(agent))
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

    # Background image (GUI-only presentation) -----------------------------
    # The chosen image is copied into the config dir as ``bg.<ext>``; only the
    # extension + opacity are persisted in the GUI config. Every operation runs
    # inside a trust boundary: the source path is untrusted user input and is
    # validated (existence, real file, extension allowlist, size bound) before
    # any copy. The destination filename is fixed, so there is no path
    # traversal on write.
    _MAX_BACKGROUND_BYTES = 25 * 1024 * 1024

    def set_background_image(self, source_path: str) -> bool:
        agent = self.agent
        try:
            from ..core.config.gui_config import (
                background_image_path,
                load_gui_config,
                normalize_background,
                normalize_background_ext,
                remove_background_image_files,
                save_gui_config,
            )

            raw = str(source_path or "").strip()
            if not raw:
                return False
            src = Path(raw).expanduser()
            # Resolve to an absolute, canonical path and reject anything that is
            # not an existing regular file (also blocks directories / symlink
            # targets that don't resolve to a real file).
            try:
                src = src.resolve(strict=True)
            except (OSError, RuntimeError):
                return False
            if not src.is_file():
                return False
            ext = normalize_background_ext(src.suffix)
            if not ext:
                return False
            try:
                size = src.stat().st_size
            except OSError:
                return False
            if size <= 0 or size > self._MAX_BACKGROUND_BYTES:
                return False

            dest = background_image_path(agent.config_dir, ext)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                return False
            # Remove any existing bg.* first so a different extension can't
            # leave a stale file behind, then copy the new image in.
            remove_background_image_files(agent.config_dir)
            import shutil

            shutil.copyfile(str(src), str(dest))

            data = load_gui_config(agent.config_dir)
            bg = normalize_background(data.get("background"))
            # Persist only the concrete file name (e.g. "bg.png"); the ext is
            # always derived from it.
            bg["fileName"] = dest.name
            data["background"] = bg
            save_gui_config(agent.config_dir, data)
        except Exception:
            return False
        self._publish_state()
        return True

    def clear_background_image(self) -> bool:
        agent = self.agent
        try:
            from ..core.config.gui_config import (
                load_gui_config,
                normalize_background,
                remove_background_image_files,
                save_gui_config,
            )

            remove_background_image_files(agent.config_dir)
            data = load_gui_config(agent.config_dir)
            bg = normalize_background(data.get("background"))
            bg["fileName"] = ""
            data["background"] = bg
            save_gui_config(agent.config_dir, data)
        except Exception:
            return False
        self._publish_state()
        return True

    def set_background_opacity(self, opacity: Any) -> bool:
        agent = self.agent
        try:
            from ..core.config.gui_config import (
                load_gui_config,
                normalize_background,
                normalize_background_opacity,
                save_gui_config,
            )

            data = load_gui_config(agent.config_dir)
            bg = normalize_background(data.get("background"))
            bg["opacity"] = normalize_background_opacity(opacity)
            data["background"] = bg
            save_gui_config(agent.config_dir, data)
        except Exception:
            return False
        self._publish_state()
        return True

    def read_background_image(self) -> Optional[tuple]:
        """Return ``(bytes, content_type)`` for the current background, or ``None``."""
        agent = self.agent
        try:
            from ..core.config.gui_config import (
                background_ext_from_filename,
                background_image_path,
                load_gui_config,
                normalize_background,
            )

            bg = normalize_background(load_gui_config(agent.config_dir).get("background"))
            ext = background_ext_from_filename(bg["fileName"])
            if not ext:
                return None
            path = background_image_path(agent.config_dir, ext)
            if not path.exists() or not path.is_file():
                return None
            data = path.read_bytes()
            content_types = {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "webp": "image/webp",
                "gif": "image/gif",
                "bmp": "image/bmp",
            }
            return data, content_types.get(ext, "application/octet-stream")
        except Exception:
            return None

    # Pasted clipboard bitmaps
    # ----------------------------------------------------------------------
    # Allowed image mime types and their on-disk extension. Deny-by-default:
    # anything not in this map is rejected.
    _PASTE_IMAGE_EXT = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
        "image/bmp": "bmp",
    }
    # Hard cap on a decoded pasted image (10 MB).
    _PASTE_IMAGE_MAX_BYTES = 10 * 1024 * 1024
    # MIME-to-extension mapping for drag-dropped files. Falls back to the
    # source filename's extension or "bin" for unknown types.
    _DROPPED_FILE_EXT = {
        "text/plain": "txt",
        "text/html": "html",
        "text/css": "css",
        "text/javascript": "js",
        "text/x-python": "py",
        "text/xml": "xml",
        "text/csv": "csv",
        "text/markdown": "md",
        "application/json": "json",
        "application/pdf": "pdf",
        "application/zip": "zip",
        "application/gzip": "gz",
        "application/x-tar": "tar",
        "application/x-7z-compressed": "7z",
        "application/x-rar-compressed": "rar",
        "image/svg+xml": "svg",
    }
    _MCP_ICON_MAX_BYTES = 2 * 1024 * 1024

    @staticmethod
    def _file_changes_scope_key(chat_id: str, workspace_id: str = "") -> str:
        """Workspace-qualified key for per-chat sidecar state.

        Chat ids repeat across workspaces, so the in-memory file-changes store
        is keyed by the composite ``workspace_id::chat_id`` to keep a chat in
        workspace A from reading/writing a same-id chat's record in workspace B.
        """
        cid = str(chat_id or "").strip()
        wsid = str(workspace_id or "").strip()
        return f"{wsid}::{cid}" if wsid else cid

    def _resolve_chat_record(
        self, chat_id: str, workspace_id: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Return the chat record for ``(workspace_id, chat_id)`` from that
        workspace's chat index, or ``None`` when the chat does not exist there.

        This is the receiving-end validation for every chat-scoped request: chat
        ids repeat across workspaces, so a request may only be dispatched when
        its ``workspace_id`` + ``chat_id`` pair actually resolves in that
        workspace.
        """
        agent = self.agent
        cid = str(chat_id or "").strip()
        if not cid:
            return None
        wsid = str(workspace_id or "").strip()
        focused_wsid = str(getattr(agent, "workspace_id", "") or "").strip()
        if not wsid or wsid == focused_wsid:
            finder = getattr(agent, "_find_chat_by_id", None)
            if callable(finder):
                try:
                    return finder(cid)
                except Exception:
                    return None
            return None
        ctx = self._persist_ctx_for_workspace(wsid)
        if not isinstance(ctx, dict):
            return None
        state = ctx.get("chat_state")
        chats = state.get("chats") if isinstance(state, dict) else None
        if not isinstance(chats, list):
            return None
        for item in chats:
            if isinstance(item, dict) and str(item.get("id") or "").strip() == cid:
                return item
        return None

    def _resolve_chat_scope(
        self, chat_id: str = "", workspace_id: str = ""
    ) -> "tuple[str, str]":
        """Validate a ``(workspace_id, chat_id)`` pair and return the effective
        ``(chat_id, workspace_id)`` to dispatch to.

        Chat ids repeat across workspaces, so a request whose pair does not
        resolve (stale id, or an id that belongs to a different workspace than
        the one supplied) is corrected to the focused workspace's active chat
        rather than silently acting on a same-id chat in the wrong workspace.
        """
        agent = self.agent
        cid = str(chat_id or "").strip()
        wsid = str(workspace_id or "").strip()
        current_ws = str(getattr(agent, "workspace_id", "") or "").strip()
        if cid:
            record = self._resolve_chat_record(cid, wsid)
            if record is not None:
                return cid, wsid or current_ws
            try:
                from ..core.logging.app_logging import get_logger, get_app_logger_root

                get_logger(f"{get_app_logger_root()}.server").warning(
                    "chat-scoped request for unknown (ws=%r, chat=%r); "
                    "falling back to the active chat",
                    wsid,
                    cid,
                )
            except Exception:
                pass
        return _primary_active_chat_id(agent), current_ws

    def _chat_data_dir_for(self, chat_id: str, workspace_id: str = "") -> Optional[Path]:
        """Resolve a chat side-data dir without relying on the focused workspace.

        ``chat_id`` values repeat across workspaces, so endpoints that persist
        per-chat artifacts must qualify the lookup with ``workspace_id`` when
        the target chat may live outside the currently focused workspace.
        """
        cid = str(chat_id or "").strip()
        if not cid:
            return None
        mgr = getattr(self.agent, "_chat_state_manager", None)
        if mgr is None:
            return None
        wsid = str(workspace_id or "").strip()
        focused_wsid = str(getattr(self.agent, "workspace_id", "") or "").strip()
        if not wsid or wsid == focused_wsid:
            return mgr.chat_data_dir_for_chat(cid)
        ctx = self._persist_ctx_for_workspace(wsid)
        if not isinstance(ctx, dict):
            return None
        state = ctx.get("chat_state")
        chats = state.get("chats") if isinstance(state, dict) else None
        if not isinstance(chats, list):
            return None
        chat = next(
            (
                item
                for item in chats
                if isinstance(item, dict)
                and str(item.get("id") or "").strip() == cid
            ),
            None,
        )
        if not isinstance(chat, dict):
            return None
        record_file = str(chat.get("_record_file") or "").strip()
        if not record_file:
            return None
        # Side data now lives in the global chats root under the record's
        # date directory; the manager resolves it from the record path.
        return mgr.chat_data_dir(record_file)

    def _ensure_chat_file_changes_loaded(self, chat_id: str, workspace_id: str = "") -> None:
        """Lazily load one chat's ``file_changes.json`` sidecar into memory.

        The ``state`` snapshot no longer bundles every chat's file changes, so
        the per-turn ``[FILE_CHANGE_REF]`` resolution in ``_build_structured_turns``
        must pull a chat's sidecar on demand when that chat is opened. The loaded
        store is cached in ``agent._file_changes_by_chat`` so repeat ``/chat-history``
        calls and undo/reapply reads hit memory instead of re-parsing the file.
        """
        cid = str(chat_id or "").strip()
        if not cid:
            return
        scope_key = ServeApp._file_changes_scope_key(cid, workspace_id)
        try:
            fc_map = dict(getattr(self.agent, "_file_changes_by_chat", {}) or {})
            if scope_key in fc_map:
                return
        except Exception:
            return
        try:
            data_dir = self._chat_data_dir_for(cid, workspace_id)
        except Exception:
            data_dir = None
        if data_dir is None:
            return
        _load_file_changes_store(self.agent, scope_key, data_dir)

    def _prewarm_turns_cache(self, chat_id: str, workspace_id: str = "") -> None:
        """Best-effort background build of the structured-turns memo for a chat.

        select_chat spawns this right after refreshing the focused chat's
        record (before activate finishes) so the GUI's first
        ``loadChatHistory`` request — which cold-builds ~1s of regex rendering
        over a multi-thousand-message history — hits the memoized cache
        instead. Mirrors the chat_history handler's order: the chat's
        file-changes sidecar is loaded first so the ``[FILE_CHANGE_REF]``
        markers resolve the same way. ``conversation_history`` is a
        thread-bound session property, so setting it here only affects this
        thread's session. Read-only; never writes to disk.
        """
        try:
            with self._session_scope_for_chat(chat_id, workspace_id):
                self._ensure_chat_file_changes_loaded(chat_id, workspace_id)
                chat = self.agent._chat_state_manager.find_chat_by_id(chat_id)
                if chat is not None:
                    self.agent.conversation_history = list(
                        chat.get("messages") or []
                    )
                self._cached_structured_turns(chat_id)
        except Exception:
            pass

    def save_pasted_image(
        self, chat_id: str, data_url: str, workspace_id: str = ""
    ) -> Dict[str, Any]:
        """Validate and persist a clipboard bitmap (``data:image/...;base64,``)
        under the active chat's side-data dir. Returns ``{ok, path, name}`` or
        ``{ok: False, error}``. Deny-by-default on bad mime / oversize."""
        import base64
        import re

        cid = str(chat_id or "").strip()
        if not cid:
            return {"ok": False, "error": "missing chatId"}
        m = re.match(
            r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$",
            str(data_url or ""),
            re.DOTALL,
        )
        if not m:
            return {"ok": False, "error": "invalid data url"}
        mime = m.group(1).lower()
        ext = self._PASTE_IMAGE_EXT.get(mime)
        if not ext:
            return {"ok": False, "error": "unsupported image type"}
        try:
            raw = base64.b64decode(m.group(2), validate=True)
        except Exception:
            return {"ok": False, "error": "invalid base64"}
        if not raw:
            return {"ok": False, "error": "empty image"}
        if len(raw) > self._PASTE_IMAGE_MAX_BYTES:
            return {"ok": False, "error": "image too large"}
        try:
            data_dir = self._chat_data_dir_for(cid, workspace_id)
            if data_dir is None:
                return {"ok": False, "error": "unknown chat"}
            import secrets

            data_dir.mkdir(parents=True, exist_ok=True)
            name = f"img_{secrets.token_hex(8)}.{ext}"
            target = data_dir / name
            target.write_bytes(raw)
            return {"ok": True, "path": str(target.resolve()), "name": name}
        except Exception:
            return {"ok": False, "error": "save failed"}

    def save_dropped_file(
        self, chat_id: str, data_url: str, file_name: str = "", workspace_id: str = ""
    ) -> Dict[str, Any]:
        """Persist a drag-dropped file under the active chat's side-data dir.
        Returns ``{ok, path, name}`` or ``{ok: False, error}``."""
        import base64
        import os
        import re

        cid = str(chat_id or "").strip()
        if not cid:
            return {"ok": False, "error": "missing chatId"}
        m = re.match(
            r"^data:([a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+);base64,(.+)$",
            str(data_url or ""),
            re.DOTALL,
        )
        if not m:
            return {"ok": False, "error": "invalid data url"}
        mime = m.group(1).lower()
        ext = self._DROPPED_FILE_EXT.get(mime)
        if not ext:
            # Image types also work through _PASTE_IMAGE_EXT.
            ext = self._PASTE_IMAGE_EXT.get(mime)
        if not ext:
            # Fall back to the source file's extension.
            base = os.path.basename(str(file_name or ""))
            _, dot_ext = os.path.splitext(base)
            ext = (dot_ext.lstrip(".") or "").lower() or "bin"
        try:
            raw = base64.b64decode(m.group(2), validate=True)
        except Exception:
            return {"ok": False, "error": "invalid base64"}
        if not raw:
            return {"ok": False, "error": "empty file"}
        max_bytes = 50 * 1024 * 1024
        if len(raw) > max_bytes:
            return {"ok": False, "error": "file too large"}
        try:
            data_dir = self._chat_data_dir_for(cid, workspace_id)
            if data_dir is None:
                return {"ok": False, "error": "unknown chat"}
            import secrets

            data_dir.mkdir(parents=True, exist_ok=True)
            # Preserve the original filename for display; prepend a random
            # token to avoid collisions between identically-named drops.
            orig_base = os.path.basename(str(file_name or ""))
            if orig_base:
                stem, orig_ext = os.path.splitext(orig_base)
                name = f"{stem}_{secrets.token_hex(4)}{orig_ext or ('.' + ext)}"
            else:
                name = f"drop_{secrets.token_hex(8)}.{ext}"
            target = data_dir / name
            target.write_bytes(raw)
            return {"ok": True, "path": str(target.resolve()), "name": name}
        except Exception:
            return {"ok": False, "error": "save failed"}

    # Draft (pre-chat) attachments -------------------------------------------
    # Pasting an image or dropping a file while the GUI is composing a brand
    # new chat (draft mode) must NOT materialize a chat yet. Files are staged
    # under the workspace cache dir instead, then moved into the chat's data
    # dir when the user actually sends (see ``materialize_draft_attachments``).

    def _draft_attachment_dir(self, workspace_id: str = "") -> Optional[Path]:
        """Resolve the cache dir that stages draft attachments for a workspace."""
        try:
            cfg = self._workspace_config_dir_for(str(workspace_id or "").strip())
            if cfg is None:
                cfg = Path(str(getattr(self.agent, "workspace_config_dir", "") or ""))
            if not str(cfg):
                return None
            return (cfg / "cache" / "draft-attachments").resolve()
        except Exception:
            return None

    def save_draft_attachment(
        self, workspace_id: str, data_url: str, file_name: str = ""
    ) -> Dict[str, Any]:
        """Validate + persist a pasted image or dropped file into the workspace
        cache dir. No chat is created — the GUI stages attachments here while
        composing a brand-new chat. Returns ``{ok, path, name}`` or
        ``{ok: False, error}``."""
        import base64
        import os
        import re

        m = re.match(
            r"^data:([a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+);base64,(.+)$",
            str(data_url or ""),
            re.DOTALL,
        )
        if not m:
            return {"ok": False, "error": "invalid data url"}
        mime = m.group(1).lower()
        ext = self._DROPPED_FILE_EXT.get(mime) or self._PASTE_IMAGE_EXT.get(mime)
        if not ext:
            # Fall back to the source file's extension.
            base = os.path.basename(str(file_name or ""))
            _, dot_ext = os.path.splitext(base)
            ext = (dot_ext.lstrip(".") or "").lower() or "bin"
        try:
            raw = base64.b64decode(m.group(2), validate=True)
        except Exception:
            return {"ok": False, "error": "invalid base64"}
        if not raw:
            return {"ok": False, "error": "empty file"}
        if len(raw) > 50 * 1024 * 1024:
            return {"ok": False, "error": "file too large"}
        try:
            stage_dir = self._draft_attachment_dir(workspace_id)
            if stage_dir is None:
                return {"ok": False, "error": "unknown workspace"}
            import secrets

            stage_dir.mkdir(parents=True, exist_ok=True)
            orig_base = os.path.basename(str(file_name or ""))
            if orig_base:
                stem, orig_ext = os.path.splitext(orig_base)
                name = f"{stem}_{secrets.token_hex(4)}{orig_ext or ('.' + ext)}"
            else:
                name = f"draft_{secrets.token_hex(8)}.{ext}"
            target = stage_dir / name
            target.write_bytes(raw)
            return {"ok": True, "path": str(target.resolve()), "name": name}
        except Exception:
            return {"ok": False, "error": "save failed"}

    def materialize_draft_attachments(
        self, chat_id: str, workspace_id: str, paths: Any
    ) -> Dict[str, Any]:
        """Move staged draft attachments (under the workspace cache
        ``draft-attachments`` dir) into the chat's side-data dir. Paths that
        don't live in the staging dir are returned unchanged. Returns
        ``{ok, mapping: {old_path: new_path}}``."""
        import shutil

        cid = str(chat_id or "").strip()
        if not cid:
            return {"ok": False, "error": "missing chatId"}
        data_dir = self._chat_data_dir_for(cid, workspace_id)
        if data_dir is None:
            return {"ok": False, "error": "unknown chat"}
        stage_dir = self._draft_attachment_dir(workspace_id)
        if stage_dir is None:
            return {"ok": False, "error": "unknown workspace"}
        stage_dir_res = stage_dir.resolve()
        mapping: Dict[str, str] = {}
        if not isinstance(paths, list):
            paths = []
        try:
            for p in paths:
                old = str(p or "").strip()
                if not old:
                    continue
                src = Path(old).resolve()
                try:
                    src.relative_to(stage_dir_res)
                except ValueError:
                    mapping[old] = old
                    continue
                if not src.is_file():
                    mapping[old] = old
                    continue
                data_dir.mkdir(parents=True, exist_ok=True)
                dest = data_dir / src.name
                if dest.exists():
                    import secrets

                    dest = data_dir / f"{src.stem}_{secrets.token_hex(4)}{src.suffix}"
                shutil.move(str(src), str(dest))
                mapping[old] = str(dest.resolve())
            return {"ok": True, "mapping": mapping}
        except Exception:
            return {"ok": False, "error": "move failed"}

    def read_chat_image(self, path: str, workspace_id: str = "") -> Optional[tuple]:
        """Return ``(bytes, content_type)`` for a pasted image, but ONLY when
        ``path`` resolves to a file inside the chats/data directory OR inside
        the workspace cache ``draft-attachments`` dir (draft-mode previews
        before a chat exists). Returns ``None`` otherwise (path traversal /
        not found)."""
        try:
            mgr = getattr(self.agent, "_chat_state_manager", None)
            if mgr is None:
                return None
            target = Path(str(path or "")).resolve()
            # Containment check: target must live under a chat's side-data
            # directory (``chats/<date>/data/<stem>/``), or under the
            # workspace cache draft-attachments dir.
            under_data = mgr.is_path_under_chat_data(target)
            if not under_data:
                stage = self._draft_attachment_dir(workspace_id)
                if stage is None:
                    return None
                try:
                    target.relative_to(stage)
                except ValueError:
                    return None
            if not target.exists() or not target.is_file():
                return None
            ext = target.suffix.lower().lstrip(".")
            content_types = {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "webp": "image/webp",
                "gif": "image/gif",
                "bmp": "image/bmp",
            }
            ct = content_types.get(ext)
            if ct is None:
                return None
            return target.read_bytes(), ct
        except Exception:
            return None

    def read_mcp_icon(self, server: str, src: str) -> Optional[tuple]:
        """Fetch a remote MCP icon over HTTP(S) and return ``(bytes, content_type)``.

        The GUI cannot load arbitrary remote ``img`` URLs directly because its
        CSP only allows loopback/data images, so remote icon URLs are proxied
        through this local authenticated endpoint instead.
        """
        server_name = str(server or "").strip()
        raw = str(src or "").strip()
        if not raw or not server_name:
            logging.getLogger(_MCP_LOGGER_NAME).info(
                f"[ICON_PROXY] skip server={server_name or '?'} empty_input={not bool(raw)}"
            )
            return None

        # Check disk cache first.
        cached = self._get_cached_mcp_icon(server_name, raw)
        if cached is not None:
            logging.getLogger(_MCP_LOGGER_NAME).info(
                f"[ICON_PROXY] cache_hit server={server_name} src={raw[:200]}"
            )
            return cached

        def _same_origin(a: str, b: str) -> bool:
            try:
                pa = urlparse(a)
                pb = urlparse(b)
                return (
                    pa.scheme in {"http", "https"}
                    and pb.scheme == pa.scheme
                    and pb.netloc == pa.netloc
                )
            except Exception:
                return False

        def _favicon_fallback_urls(url: str) -> List[str]:
            try:
                parsed_url = urlparse(url)
                host = str(parsed_url.hostname or "").strip(".")
                if parsed_url.path != "/favicon.ico" or not host:
                    return []
                parts = [p for p in host.split(".") if p]
                if len(parts) < 2:
                    return []
                apex = ".".join(parts[-2:])
                out: List[str] = []
                for fallback_host in (apex, f"www.{apex}"):
                    if fallback_host == host:
                        continue
                    candidate = f"{parsed_url.scheme}://{fallback_host}/favicon.ico"
                    if candidate != url and candidate not in out:
                        out.append(candidate)
                return out
            except Exception:
                return []

        def _fetch_icon_url(url: str, extra_headers: Dict[str, str], auth_enabled: bool) -> Optional[tuple]:
            parsed_url = urlparse(url)
            if parsed_url.scheme not in {"http", "https"}:
                logging.getLogger(_MCP_LOGGER_NAME).info(
                    f"[ICON_PROXY] skip server={server_name} unsupported_scheme src={url[:200]}"
                )
                return None
            try:
                from ..config.app_info import get_app_name, get_app_version

                user_agent = f"{get_app_name()}/{get_app_version()}"
            except Exception:
                user_agent = "CodeWood/unknown"
            try:
                req_headers = {
                    "User-Agent": user_agent,
                    "Accept": "image/*,*/*;q=0.8",
                }
                req_headers.update(extra_headers)
                logging.getLogger(_MCP_LOGGER_NAME).info(
                    f"[ICON_PROXY] fetch server={server_name} auth_headers={1 if auth_enabled else 0} src={url[:200]}"
                )
                req = urllib.request.Request(
                    url=url,
                    headers=req_headers,
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    data = resp.read(self._MCP_ICON_MAX_BYTES + 1)
                    if not data or len(data) > self._MCP_ICON_MAX_BYTES:
                        logging.getLogger(_MCP_LOGGER_NAME).info(
                            f"[ICON_PROXY] reject server={server_name} reason=size bytes={len(data) if data else 0}"
                        )
                        return None
                    header_ct = str(resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                    guessed_ct = {
                        ".ico": "image/x-icon",
                        ".png": "image/png",
                        ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg",
                        ".svg": "image/svg+xml",
                        ".webp": "image/webp",
                        ".gif": "image/gif",
                        ".bmp": "image/bmp",
                    }.get(Path(parsed_url.path).suffix.lower(), "")
                    content_type = header_ct or guessed_ct
                    if not content_type:
                        logging.getLogger(_MCP_LOGGER_NAME).info(
                            f"[ICON_PROXY] reject server={server_name} reason=missing_content_type src={url[:200]}"
                        )
                        return None
                    if not (content_type.startswith("image/") or content_type == "image/vnd.microsoft.icon"):
                        logging.getLogger(_MCP_LOGGER_NAME).info(
                            f"[ICON_PROXY] reject server={server_name} reason=bad_content_type ct={content_type} src={url[:200]}"
                        )
                        return None
                    logging.getLogger(_MCP_LOGGER_NAME).info(
                        f"[ICON_PROXY] ok server={server_name} ct={content_type} bytes={len(data)} src={url[:200]}"
                    )
                    return data, content_type
            except Exception as e:
                logging.getLogger(_MCP_LOGGER_NAME).info(
                    f"[ICON_PROXY] fail server={server_name} err={str(e)[:200]} src={url[:200]}"
                )
                return None

        headers: Dict[str, str] = {}
        target_url = raw
        used_auth_headers = False
        try:
            cfg = self._mcp_load_jsonc()
            servers_cfg = cfg.get("mcpServers") if isinstance(cfg, dict) else {}
            conf = servers_cfg.get(server_name) if isinstance(servers_cfg, dict) else None
            if isinstance(conf, dict):
                base_url = str(conf.get("url") or "").strip()
                if base_url:
                    target_url = urljoin(base_url, raw)
                    if _same_origin(base_url, target_url):
                        raw_headers = conf.get("headers")
                        if isinstance(raw_headers, dict):
                            for hk, hv in raw_headers.items():
                                key = str(hk or "").strip()
                                val = str(hv or "").strip()
                                if key and val:
                                    headers[key] = val
                        used_auth_headers = bool(headers)
        except Exception:
            target_url = raw
            headers = {}
        result = _fetch_icon_url(target_url, headers, used_auth_headers)
        if result is not None:
            self._save_cached_mcp_icon(server_name, raw, result[0], result[1])
            return result
        for fallback_url in _favicon_fallback_urls(target_url):
            logging.getLogger(_MCP_LOGGER_NAME).info(
                f"[ICON_PROXY] fallback server={server_name} src={fallback_url[:200]}"
            )
            result = _fetch_icon_url(fallback_url, {}, False)
            if result is not None:
                self._save_cached_mcp_icon(server_name, raw, result[0], result[1])
                return result
        return None

    # ------------------------------------------------------------------
    # MCP icon disk cache
    # ------------------------------------------------------------------

    def _mcp_icon_cache_dir(self) -> Path:
        """Return the directory for cached MCP icon data."""
        d = self.agent.config_dir / "cache" / "mcp_icons"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return d

    def _get_cached_mcp_icon(self, server_name: str, src: str) -> Optional[tuple]:
        """Return ``(bytes, content_type)`` from disk cache if still valid.

        The cache entry is considered valid only when its stored ``src``
        matches the caller's ``src`` — if the icon URL has changed the
        cached blob is discarded.
        """
        if not server_name or not src:
            return None
        try:
            path = self._mcp_icon_cache_dir() / f"{server_name}.json"
            if not path.is_file():
                return None
            raw = path.read_bytes()
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                return None
            if str(obj.get("src") or "") != src:
                path.unlink(missing_ok=True)
                return None
            data_b64 = obj.get("data")
            if not isinstance(data_b64, str):
                return None
            data = base64.b64decode(data_b64)
            content_type = str(obj.get("content_type") or "")
            if not data or not content_type:
                return None
            return data, content_type
        except Exception:
            return None

    def _save_cached_mcp_icon(
        self, server_name: str, src: str, data: bytes, content_type: str
    ) -> None:
        """Persist icon bytes to the disk cache."""
        if not server_name or not src or not data or not content_type:
            return
        try:
            payload = {
                "src": src,
                "content_type": content_type,
                "data": base64.b64encode(data).decode("ascii"),
                "cached_at": time.time(),
            }
            path = self._mcp_icon_cache_dir() / f"{server_name}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
        except Exception:
            pass

    # Embedded browser command channel
    # ----------------------------------------------------------------------
    # Embedded console: PTY sessions managed by ``self._console``. Tabs stream
    # output via SSE and send input via HTTP POST; the model drives the active
    # session through the ``console_*`` tools.
    def _apply_saved_console_options(self) -> None:
        try:
            from ..core.config.gui_config import (
                load_gui_config,
                normalize_console_options,
            )

            cfg = load_gui_config(self.agent.config_dir)
            opts = normalize_console_options(cfg.get("console"))
            self._console.set_buffer_lines(opts.get("bufferLines") or 1000)
        except Exception:
            pass

    def open_console(self, kind: str) -> Dict[str, Any]:
        result = self._console.open(str(kind or ""))
        if result.get("success"):
            self._attach_console_stream(str(result.get("id") or ""))
            self._broadcast_console_list()
        return result

    def _attach_console_stream(self, session_id: str) -> None:
        """Forward a session's live PTY output to SSE subscribers.

        WebView2 loads the GUI from a ``file://`` origin, which Chromium treats
        as opaque and therefore forbids ``ws://`` connections from. So console
        output rides the existing SSE event stream (as base64) and input/resize
        travel over plain HTTP POSTs instead of a dedicated WebSocket.
        """
        session = self._console.get(session_id)
        if session is None:
            return
        broadcaster = self.broadcaster

        def _on_chunk(chunk: bytes) -> None:
            try:
                payload = base64.b64encode(chunk or b"").decode("ascii")
                broadcaster.publish(
                    "console_output",
                    {"id": session_id, "b64": payload, "end": not chunk},
                )
            except Exception:
                pass

        # add_subscriber replays the retained buffer to this callback, but that
        # would broadcast a session's scrollback to *every* SSE client. The
        # per-terminal replay is delivered on demand via /console-attach, so we
        # skip the implicit replay here by attaching without it.
        session.add_live_subscriber(_on_chunk)

    def attach_console(self, session_id: str) -> Dict[str, Any]:
        """Return the retained raw output so a (re)mounted terminal can repaint."""
        session = self._console.get(str(session_id or ""))
        if session is None:
            return {"ok": False, "error": "no such console"}
        raw = session.snapshot_raw()
        return {"ok": True, "b64": base64.b64encode(raw).decode("ascii")}

    def console_input(self, session_id: str, data: str) -> bool:
        session = self._console.get(str(session_id or ""))
        if session is None:
            return False
        session.write(str(data or ""))
        return True

    def console_resize(self, session_id: str, cols: Any, rows: Any) -> bool:
        session = self._console.get(str(session_id or ""))
        if session is None:
            return False
        session.resize(cols, rows)
        return True

    def close_console(self, session_id: str) -> bool:
        ok = self._console.close(str(session_id or ""))
        if ok:
            self._broadcast_console_list()
        return ok

    def set_active_console(self, session_id: str) -> bool:
        return self._console.set_active(str(session_id or ""))

    def list_consoles(self) -> List[Dict[str, Any]]:
        return self._console.list()

    def _broadcast_console_list(self) -> None:
        try:
            self.broadcaster.publish("console_list", {"consoles": self._console.list()})
        except Exception:
            pass

    def set_console_options(self, options: Dict[str, Any]) -> bool:
        """Persist GUI-only console options (font + buffer lines)."""
        if not isinstance(options, dict):
            return False
        agent = self.agent
        try:
            from ..core.config.gui_config import (
                load_gui_config,
                normalize_console_options,
                save_gui_config,
            )

            opts = normalize_console_options(options)
            data = load_gui_config(agent.config_dir)
            data["console"] = opts
            save_gui_config(agent.config_dir, data)
            self._console.set_buffer_lines(opts.get("bufferLines") or 1000)
        except Exception:
            return False
        try:
            self.broadcaster.publish("idle", self._route(state=_build_state(agent)))
        except Exception:
            pass
        return True

    def dispatch_console_command(
        self, action: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Drive the ACTIVE console session on behalf of the model."""
        act = str(action or "")
        data = payload or {}
        session = self._console.active()
        if session is None:
            if act == "exec":
                default_kind = "powershell" if os.name == "nt" else "shell"
                result = self.open_console(default_kind)
                if not result.get("success"):
                    return {
                        "success": False,
                        "error": f"could not auto-open console: {result.get('error', 'unknown error')}",
                    }
                session = self._console.active()
                if session is None:
                    return {
                        "success": False,
                        "error": "no active console (auto-open failed)",
                    }
                self.broadcaster.publish(
                    "console_open",
                    {
                        "id": session.id,
                        "title": session.title,
                        "kind": session.kind,
                    },
                )
            else:
                return {"success": False, "error": "no active console (open one in the GUI)"}
        if act == "exec":
            command = str(data.get("command") or "")
            if not command.strip():
                return {"success": False, "error": "missing command"}
            # Send into the live interactive shell, terminated with a newline.
            session.write(command.rstrip("\n") + "\r")
            return {"success": True, "id": session.id}
        if act == "send":
            text = str(data.get("data") or "")
            if not text:
                return {"success": False, "error": "missing data"}
            # Raw interactive input (e.g. a gdb command, a y/n answer, control
            # keys). Nothing is appended; escape decoding happens tool-side.
            session.write(text)
            return {"success": True, "id": session.id, "sent": text}
        if act == "interrupt":
            session.interrupt()
            return {"success": True, "id": session.id}
        if act == "resize":
            session.resize(data.get("cols"), data.get("rows"))
            return {
                "success": True,
                "cols": session.cols,
                "rows": session.rows,
            }
        if act == "read":
            try:
                start = int(data.get("start") or 0)
            except (TypeError, ValueError):
                start = 0
            try:
                count = int(data.get("count") or 200)
            except (TypeError, ValueError):
                count = 200
            r = session.read_lines(start, count)
            return {"success": True, **_format_console_read(r)}
        if act == "wait":
            raw_start = data.get("start")
            if raw_start is None:
                start = None
            else:
                try:
                    start = int(raw_start)
                except (TypeError, ValueError):
                    start = None
            try:
                count = int(data.get("count") or 200)
            except (TypeError, ValueError):
                count = 200
            try:
                timeout = float(data.get("timeout") or 5)
            except (TypeError, ValueError):
                timeout = 5
            stable = bool(data.get("stable"))
            r = session.wait_lines(start, count, timeout, stable=stable)
            return {"success": True, **_format_console_read(r)}
        if act == "info":
            r = session.info()
            r["output"] = (
                f"Shell: {r.get('kind', '?')}, CWD: {r.get('cwd', '?')}, "
                f"Size: {r.get('cols', '?')}x{r.get('rows', '?')}, "
                f"totalLines: {r.get('totalLines', 0)}, "
                f"alive: {r.get('alive', False)}"
            )
            return {"success": True, **r}
        return {"success": False, "error": f"unknown console action: {action}"}

    # ----------------------------------------------------------------------
    # Browser tools run on the backend but the browser lives in the frontend
    # WebView. A tool publishes a ``browser_command`` SSE event and blocks on a
    # per-request queue until the BrowserPanel posts the outcome to
    # ``/browser-result``.
    _BROWSER_CMD_TIMEOUT_S = 15.0

    def dispatch_browser_command(
        self, action: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Send a browser command to the frontend and wait for its result.

        Returns the frontend's result dict, or an error dict on timeout / when
        no GUI client is connected to handle it."""
        request_id = secrets.token_hex(8)
        reply: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        with self._browser_cmds_lock:
            self._browser_cmds[request_id] = reply
        event_data: Dict[str, Any] = {"action": str(action), "requestId": request_id}
        if payload:
            for key in ("url", "script"):
                if key in payload and payload[key] is not None:
                    event_data[key] = payload[key]
        self.broadcaster.publish("browser_command", event_data)
        try:
            result = reply.get(timeout=self._BROWSER_CMD_TIMEOUT_S)
        except queue.Empty:
            return {
                "success": False,
                "error": "browser did not respond (is the GUI open?)",
            }
        finally:
            with self._browser_cmds_lock:
                self._browser_cmds.pop(request_id, None)
        return result if isinstance(result, dict) else {"success": False, "error": "bad result"}

    def answer_browser_result(self, request_id: str, result: Dict[str, Any]) -> bool:
        """Deliver a browser command result from the frontend to the waiting
        tool. Returns True when a matching pending request was found."""
        rid = str(request_id or "").strip()
        if not rid:
            return False
        with self._browser_cmds_lock:
            reply = self._browser_cmds.get(rid)
        if reply is None:
            return False
        reply.put(result if isinstance(result, dict) else {})
        return True

    # Browser HTML preview
    # ----------------------------------------------------------------------
    # The user can preview an HTML code block in the embedded browser. The HTML
    # is wrapped with a small same-origin bridge script (so DOM/console reads
    # work on the preview page) and saved under the chat's data dir, then served
    # by ``/chat-file``.
    _PREVIEW_HTML_MAX_BYTES = 2 * 1024 * 1024

    def save_preview_html(
        self, chat_id: str, html: str, workspace_id: str = ""
    ) -> Dict[str, Any]:
        """Persist an HTML snippet (wrapped with the preview bridge) under the
        chat data dir. Returns ``{ok, path}`` or ``{ok: False, error}``.

        ``workspace_id`` scopes the chat (chat ids repeat across workspaces).
        """
        cid = str(chat_id or "").strip()
        if not cid:
            return {"ok": False, "error": "missing chatId"}
        raw = str(html or "")
        if not raw.strip():
            return {"ok": False, "error": "empty html"}
        if len(raw.encode("utf-8", "ignore")) > self._PREVIEW_HTML_MAX_BYTES:
            return {"ok": False, "error": "html too large"}
        try:
            data_dir = self._chat_data_dir_for(cid, workspace_id)
            if data_dir is None:
                return {"ok": False, "error": "unknown chat"}
            data_dir.mkdir(parents=True, exist_ok=True)
            name = f"preview_{secrets.token_hex(8)}.html"
            target = data_dir / name
            target.write_text(_wrap_preview_html(raw), encoding="utf-8")
            return {"ok": True, "path": str(target.resolve())}
        except Exception:
            return {"ok": False, "error": "save failed"}

    def _save_preview_for_path(self, path: str) -> Optional[Dict[str, Any]]:
        """Read a local HTML file and persist a bridged preview copy.

        Shared helper between ``preview_local_html_file`` (tool) and
        ``/preview-local-file`` HTTP endpoint (GUI re-preview). Returns
        ``{url, path}`` on success, ``None`` on failure."""
        raw = str(path or "").strip().strip('"').strip("'")
        if not raw:
            return None
        try:
            resolver = getattr(self.agent, "_resolve_user_path", None)
            resolved = Path(resolver(raw)) if callable(resolver) else Path(raw)
            resolved = resolved.expanduser().resolve()
        except Exception:
            return None
        if resolved.suffix.lower() not in (".html", ".htm"):
            return None
        try:
            if not resolved.is_file():
                return None
            html = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        saved = self.save_preview_html(self._active_chat_id(), html)
        if not saved.get("ok"):
            return None
        token = self._token
        from urllib.parse import urlencode

        url = "/chat-file?" + urlencode({"token": token, "path": saved["path"]})
        return {"url": url, "path": saved["path"]}

    def preview_local_html_file(self, path: str) -> Dict[str, Any]:
        """Read a local HTML file produced by the model, persist a bridged copy
        under the chat data dir, and open it in the embedded browser.

        ``path`` is resolved with the agent's canonical user-path resolver and
        validated to be an existing ``.html``/``.htm`` file before reading."""
        saved = self._save_preview_for_path(path)
        if saved is None:
            return {"success": False, "error": "preview failed"}
        result = self.dispatch_browser_command("open_preview", {"url": saved["url"]})
        return result if isinstance(result, dict) else {"success": False, "error": "no browser"}

    def read_chat_file(self, path: str) -> Optional[tuple]:
        """Return ``(bytes, content_type)`` for a saved chat-data file (html
        preview), ONLY when ``path`` is contained under chats/data. ``None``
        otherwise."""
        try:
            mgr = getattr(self.agent, "_chat_state_manager", None)
            if mgr is None:
                return None
            target = Path(str(path or "")).resolve()
            if not mgr.is_path_under_chat_data(target):
                return None
            if not target.exists() or not target.is_file():
                return None
            ext = target.suffix.lower().lstrip(".")
            content_types = {
                "html": "text/html; charset=utf-8",
                "htm": "text/html; charset=utf-8",
                "css": "text/css; charset=utf-8",
                "js": "text/javascript; charset=utf-8",
                "txt": "text/plain; charset=utf-8",
            }
            ct = content_types.get(ext)
            if ct is None:
                return None
            return target.read_bytes(), ct
        except Exception:
            return None

    def _publish_state(self) -> None:
        try:
            self.broadcaster.publish(
                "idle", self._route(state=_build_state(self.agent))
            )
        except Exception:
            pass

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
            "project_context_search_enabled": bool(getattr(agent, "project_context_search_enabled", True)),
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
                    if "project_context_search_enabled" in cfg:
                        out["project_context_search_enabled"] = bool(cfg.get("project_context_search_enabled"))
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
        if "project_context_search_enabled" in payload:
            normalized["project_context_search_enabled"] = bool(payload.get("project_context_search_enabled"))
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
        # Reload tool specs so gating changes (e.g. project_context_search_enabled)
        # take effect without a restart.
        try:
            agent.tool_specs = agent._load_tools_spec_from_jsonc()
        except Exception:
            pass
        # Push a fresh state snapshot to refresh any open settings page.
        try:
            self.broadcaster.publish(
                "idle", self._route(state=_build_state(agent))
            )
        except Exception:
            pass
        return True

    # Security audit model config
    # -------------------------------------------------------------------------
    # This setting lives at the top level of ``config.jsonc`` so it is
    # consistent between the TUI and the GUI.

    def get_security_audit_config(self) -> Dict[str, str]:
        """Return current security audit model selector.

        Falls back to the live agent attribute when ``config.jsonc`` is
        missing or unreadable.
        """
        agent = self.agent
        out: str = str(
            getattr(agent, "_security_audit_model_selector", "") or ""
        ).strip()
        try:
            from ..core.config.config_jsonc import (
                CONFIG_JSONC_FILENAME,
                load_config_jsonc,
            )

            cfg_path = agent.config_dir / CONFIG_JSONC_FILENAME
            if cfg_path.exists():
                cfg = load_config_jsonc(cfg_path) or {}
                if isinstance(cfg, dict) and "security_audit_model" in cfg:
                    raw = cfg.get("security_audit_model", "")
                    if isinstance(raw, str):
                        out = raw.strip()
        except Exception:
            pass
        return {"security_audit_model": out}

    def save_security_audit_config(self, payload: Dict[str, Any]) -> bool:
        """Persist security audit model to ``config.jsonc`` and apply
        immediately. The rest of the config file is preserved.
        """
        if not isinstance(payload, dict):
            return False
        raw = payload.get("security_audit_model")
        if not isinstance(raw, str):
            return False
        value = raw.strip()
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
            cfg_data["security_audit_model"] = value
            save_config_jsonc(cfg_path, cfg_data)
        except Exception:
            return False
        # Apply to the live agent so the change takes effect immediately.
        try:
            agent._security_audit_model_selector = value
        except Exception:
            pass
        # Drop the resolved-config cache so the model catalog re-reads fresh.
        agent._resolved_config_data = {}
        return True

    def get_sandbox_config(self) -> Dict[str, Any]:
        """Return the current sandbox settings plus provisioning status."""
        from ..core.sandbox.config import read_sandbox_settings
        from ..core.sandbox import get_sandbox_backend

        agent = self.agent
        settings = read_sandbox_settings(agent.config_dir)
        backend = get_sandbox_backend()
        workspace_root = getattr(agent, "workspace_root", None) or getattr(
            agent, "work_directory", None
        )
        status = backend.status(agent.config_dir, workspace_root)
        out = dict(settings)
        # Live agent values (diagnostic): the file may be correct while the
        # running process still holds a stale level.
        out["agent_level"] = str(
            getattr(agent, "sandbox_level", "") or ""
        )
        out["agent_network"] = bool(getattr(agent, "sandbox_network", False))
        out["supported"] = bool(status.get("supported", False))
        out["provisioned"] = bool(status.get("provisioned", False))
        out["backend"] = str(status.get("name") or backend.name or "")
        out["message"] = status.get("message")
        if status.get("users_exist") is not None:
            out["users_exist"] = bool(status.get("users_exist"))
            out["offline_user"] = status.get("offline_user")
            out["online_user"] = status.get("online_user")
            out["secret_exists"] = bool(status.get("secret_exists"))
            out["users_foreign"] = bool(status.get("users_foreign"))
        from ..core.sandbox.windows import _flag_path, _users_ready_path

        flag_mtime: Optional[float] = None
        try:
            flag_mtime = _flag_path(agent.config_dir).stat().st_mtime
        except Exception:
            pass
        ready_mtime: Optional[float] = None
        try:
            ready_mtime = _users_ready_path(agent.config_dir).stat().st_mtime
        except Exception:
            pass
        key = str(agent.config_dir)
        prev_flag_mtime = _sandbox_flag_mtime.get(key)
        prev_ready_mtime = _sandbox_ready_mtime.get(key)
        if flag_mtime is not None:
            _sandbox_flag_mtime[key] = flag_mtime
        if ready_mtime is not None:
            _sandbox_ready_mtime[key] = ready_mtime
        pw_ok = backend.verify_credentials(agent.config_dir)
        # A setup is considered complete as soon as the ELEVATED phase
        # (users / group / firewall) wrote ``users_ready``; the ACL work that
        # follows on the serve side must not gate the page status.  Either
        # flag advancing since the last load means a setup just finished, so
        # drop the stale cached check and verify once against the fresh
        # accounts.
        setup_just_completed = (
            (
                flag_mtime is not None
                and (prev_flag_mtime is None or flag_mtime > prev_flag_mtime)
            )
            or (
                ready_mtime is not None
                and (prev_ready_mtime is None or ready_mtime > prev_ready_mtime)
            )
        )
        if (
            pw_ok is False
            and setup_just_completed
        ):
            # The cached False predates the completed setup (it was recorded
            # while the old accounts still had the mismatched password).
            # Verify once without the cache so the page reflects the new
            # accounts' passwords.
            pw_ok = backend.verify_credentials(agent.config_dir, fresh=True)
        if pw_ok is not None:
            out["passwords_ok"] = bool(pw_ok)
        return out

    def save_sandbox_config(self, payload: Dict[str, Any]) -> bool:
        """Persist sandbox settings to ``config.jsonc`` and apply live."""
        if not isinstance(payload, dict):
            return False
        agent = self.agent
        try:
            from ..core.sandbox.config import (
                CONFIG_KEY_LEVEL,
                CONFIG_KEY_NETWORK,
                persist_sandbox_settings,
            )

            level = payload.get(CONFIG_KEY_LEVEL)
            network = payload.get(CONFIG_KEY_NETWORK)
            if level is not None and not isinstance(level, str):
                return False
            if network is not None and not isinstance(network, bool):
                return False
            settings = persist_sandbox_settings(
                agent.config_dir,
                level=level,
                network=network,
            )
        except Exception:
            return False
        # Apply to the live agent so the change takes effect immediately.
        try:
            agent.sandbox_level = settings[CONFIG_KEY_LEVEL]
            agent.sandbox_network = settings[CONFIG_KEY_NETWORK]
        except Exception:
            pass
        agent._resolved_config_data = {}
        # Best-effort workspace ACL refresh (no elevation needed; the files
        # belong to the current user). Ignored when not provisioned yet.
        try:
            from ..core.sandbox import refresh_workspace_acls

            refresh_workspace_acls(agent, level=settings[CONFIG_KEY_LEVEL])
        except Exception:
            pass
        return True

    def setup_sandbox(self) -> Dict[str, Any]:
        """Launch the elevated provisioning helper via UAC (best effort)."""
        agent = self.agent
        try:
            from ..core.sandbox.config import read_sandbox_settings
            from ..core.sandbox.windows import launch_elevated_setup
            # A status load that predates this setup may have cached a password
            # mismatch; the elevated helper recreates the users, so drop it now
            # and let the settings page re-verify once provisioning completes.
            try:
                from ..core.sandbox.windows import _credential_check_cache

                _credential_check_cache.pop(
                    str(Path(agent.config_dir).resolve()), None
                )
            except Exception:
                pass
            settings = read_sandbox_settings(agent.config_dir)
            level = settings["sandbox_level"]
            if level not in ("read_only", "workspace_write"):
                level = "workspace_write"
            workspace_root = getattr(agent, "workspace_root", None) or getattr(
                agent, "work_directory", None
            )
            config_dir = agent.config_dir

            def _run_background_setup() -> None:
                # The elevated window appears immediately and only creates
                # the users/group/firewall (fast).  The old-ACL sweep and the
                # slow ACL work (runtime dirs, python/profile read grants,
                # workspace ACLs) run here on this background thread; once
                # the users-ready flag appears the ACL phase starts.
                try:
                    from ..core.sandbox import (
                        cleanup_all_sandbox_acls,
                        get_sandbox_backend,
                    )

                    backend = get_sandbox_backend()
                    ok = launch_elevated_setup(config_dir, workspace_root, level)
                    if not ok:
                        return
                    # Strip the old sandbox ACLs in the background: only
                    # meaningful when the users are rebuilt (their SIDs
                    # change); the dead-SID cleanup removes stale ACEs even
                    # if this runs after the elevated rebuild finished.
                    if backend.verify_credentials(config_dir) is not True:
                        cleanup_all_sandbox_acls(config_dir)
                    backend.wait_and_provision_acls(
                        config_dir, workspace_root, level
                    )
                except Exception:
                    import logging

                    logging.getLogger("codewood.serve").exception(
                        "background sandbox setup failed"
                    )

            import threading

            threading.Thread(target=_run_background_setup, daemon=True).start()
            return {
                "ok": True,
                "message": (
                    "Sandbox setup started: accept the UAC prompt (users are "
                    "created fast); the ACL work finishes in the background, "
                    "so refresh this page afterwards to see the final status."
                ),
            }
        except Exception as exc:
            return {"ok": False, "message": f"setup failed: {exc}"}

    def get_model_selectors(self) -> List[str]:
        """Return the list of configured model selector strings for dropdowns."""
        agent = self.agent
        try:
            selectors = agent._get_configured_model_selectors()
            if isinstance(selectors, list):
                return [str(s) for s in selectors if s]
        except Exception:
            pass
        return []

    def get_confirm_allowlist(self) -> Dict[str, Any]:
        """Return the confirm allowlist data by reading the file directly,
        so the call is not blocked by a running task's in-memory state."""
        try:
            from ..core.security.command_security import confirm_allowlist_path
            p = confirm_allowlist_path(self.agent)
            if p.is_file():
                import json
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            return {
                "version": 3,
                "salt": "",
                "shell_scripts": [],
                "shell_exe_tokens": [],
            }
        except Exception:
            return {
                "version": 3,
                "salt": "",
                "shell_scripts": [],
                "shell_exe_tokens": [],
            }

    def save_confirm_allowlist(self, payload: Dict[str, Any]) -> bool:
        """Write the confirm allowlist file directly and reload into the agent,
        so the mutation is not blocked by a running task's in-memory state."""
        if not isinstance(payload, dict):
            return False
        try:
            from ..core.security.command_security import (
                confirm_allowlist_path,
                load_confirm_allowlist,
                shell_script_hash,
            )
            from pathlib import Path
            import json
            # Auto-compute hashes for shell_scripts entries that have an empty
            # hash. This lets the frontend add entries without needing the salt.
            scripts = payload.get("shell_scripts")
            if isinstance(scripts, list):
                for entry in scripts:
                    if (
                        isinstance(entry, dict)
                        and entry.get("path")
                        and not entry.get("hash")
                    ):
                        try:
                            h = shell_script_hash(self.agent, Path(entry["path"]))
                            if h:
                                entry["hash"] = h
                        except Exception:
                            pass
            p = confirm_allowlist_path(self.agent)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # Reload into the agent so the running loop picks up the change.
            load_confirm_allowlist(self.agent)
            return True
        except Exception:
            return False

    def get_completion_catalog(self) -> Dict[str, Any]:
        """Return suggestion sources used by the composer's slash popup.

        Includes:
          - ``skills``: list of {name, description}. Sourced from the loaded
            skill records on the agent (already merged across builtin/global/
            workspace skill roots and respecting language). Disabled skills
            (via ``skills.jsonc``) are filtered out.
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
        # Collect disabled skill IDs from each source's own skills.jsonc
        disabled_by_source: Dict[str, set] = {}
        try:
            for src in ("builtin", "agents", "global", "workspaceAgents", "workspace"):
                cfg = self._skills_load_jsonc(src)
                disabled_by_source[src] = set(cfg.get("disabledSkills") or [])
        except Exception:
            pass
        out_skills: List[Dict[str, str]] = []
        try:
            for record in getattr(agent, "skills", []) or []:
                skill_id = str(getattr(record, "skill_id", "") or "")
                source = str(getattr(record, "source", "builtin") or "builtin")
                if skill_id in disabled_by_source.get(source, set()):
                    continue
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

    def _mcp_server_enabled_in_config(self, server: str) -> bool:
        """Return the latest persisted enabled flag for one MCP server."""
        srv = str(server or "").strip()
        if not srv:
            return False
        try:
            cfg = self._mcp_load_jsonc()
            servers = cfg.get("mcpServers")
            if not isinstance(servers, dict):
                return False
            conf = servers.get(srv)
            if not isinstance(conf, dict):
                return False
            return not bool(conf.get("skip_preload", False))
        except Exception:
            return False

    def _mcp_reconnect_running(self, server: str) -> bool:
        srv = str(server or "").strip()
        if not srv:
            return False
        with self._mcp_reconnect_lock:
            thread = self._mcp_reconnect_threads.get(srv)
            return bool(thread is not None and thread.is_alive())

    def _refresh_mcp_agent_tools(self) -> None:
        """Rebuild model-visible tool specs after an MCP config/runtime change."""
        try:
            agent = self.agent
            agent.tool_specs = agent._load_tools_spec_from_jsonc()
            agent.system_prompt = agent._compose_system_prompt_snapshot(include_tools=True)
        except Exception:
            pass

    def _publish_mcp_state(self) -> None:
        try:
            self.broadcaster.publish(
                "idle", self._route(state=_build_state(self.agent))
            )
        except Exception:
            pass

    def _deactivate_mcp_server_runtime(self, server: str) -> None:
        """Best-effort live unload for a disabled MCP server."""
        srv = str(server or "").strip()
        if not srv:
            return
        mgr = getattr(self.agent, "mcp_manager", None)
        if mgr is None:
            return
        remove_runtime = getattr(mgr, "_remove_server_runtime", None)
        if callable(remove_runtime):
            try:
                remove_runtime(srv)
            except Exception:
                pass
        set_status = getattr(mgr, "_set_status", None)
        if callable(set_status):
            try:
                set_status(
                    srv,
                    "skipped",
                    last_error="skip_preload=true",
                    failure_type="",
                    suggestion="This server is configured with skip_preload=true; set it to false if you need automatic preload.",
                )
            except Exception:
                pass

    def _reconcile_mcp_server_after_reconnect(self, server: str) -> None:
        """Apply the latest toggle state after an async reconnect settles."""
        srv = str(server or "").strip()
        if not srv:
            return
        if not self._mcp_server_enabled_in_config(srv):
            self._deactivate_mcp_server_runtime(srv)
        self._refresh_mcp_agent_tools()
        self._publish_mcp_state()

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

    # ----- global chat search -------------------------------------------

    def _chat_search_index(self) -> Any:
        """Lazily create the cross-workspace chat search index and start its
        background refresher. Returns ``None`` when the index is unavailable."""
        if self._chat_search is None:
            try:
                from ..services.chat_search_index import ChatSearchIndex

                self._chat_search = ChatSearchIndex()
            except Exception:
                self._chat_search = None
            self._start_chat_search_refresher()
        return self._chat_search

    def _start_chat_search_refresher(self) -> None:
        if self._chat_search_refresher_started:
            return
        self._chat_search_refresher_started = True
        try:
            thread = threading.Thread(
                target=self._chat_search_refresh_loop,
                name="chat-search-refresh",
                daemon=True,
            )
            thread.start()
        except Exception:
            self._chat_search_refresher_started = False

    def _chat_search_refresh_loop(self) -> None:
        import time as _time

        wait = 3.0
        while not self._shutdown_event.is_set():
            try:
                self._chat_search_refresh_once()
            except Exception:
                pass
            try:
                self._chat_search_wake.wait(wait)
            except Exception:
                break
            self._chat_search_wake.clear()
            wait = 15.0

    def _chat_search_refresh_once(self) -> None:
        idx = self._chat_search
        if idx is None:
            return
        # Preload the jieba dictionary off the HTTP path (first index build).
        idx.warmup()
        for ws in self._enumerate_search_workspaces():
            try:
                idx.refresh_workspace(ws["id"], ws["name"], ws["chats_root"])
            except Exception:
                pass

    def _enumerate_search_workspaces(self) -> List[Dict[str, Any]]:
        """Enumerate every known workspace (default + registered) with its
        global chats root for the chat search indexer."""
        agent = self.agent
        out: List[Dict[str, Any]] = []
        try:
            raw = getattr(agent, "_workspaces_state", {})
            entries = raw.get("workspaces") if isinstance(raw, dict) else {}
        except Exception:
            entries = {}
        if not isinstance(entries, dict):
            return out
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            ws_id = str(entry.get("id") or "").strip()
            if not ws_id:
                continue
            # The chats root is the single global ``<global-config>/chats``
            # directory; resolve it through the chat-state manager so a test /
            # override root is honored consistently.
            chats_root = None
            try:
                chats_root = getattr(agent, "_chats_root_override", None)
            except Exception:
                chats_root = None
            if chats_root is None:
                try:
                    mgr = getattr(agent, "_chat_state_manager", None)
                    if mgr is not None:
                        chats_root = mgr.chat_records_dir()
                except Exception:
                    chats_root = None
            if chats_root is None:
                chats_root = get_app_global_config_dir() / "chats"
            out.append(
                {
                    "id": ws_id,
                    "name": str(entry.get("name") or ""),
                    "chats_root": chats_root,
                }
            )
        return out

    def search_chats(self, query: str, limit: int = 20) -> Dict[str, Any]:
        """Full-text search across all workspaces' non-archived chats."""
        idx = self._chat_search_index()
        if idx is None:
            return {"ok": False, "keywords": [], "total": 0, "results": []}
        try:
            result = idx.search(str(query or ""), max(1, min(50, int(limit) or 20)))
            result["ok"] = True
            return result
        except Exception:
            return {"ok": False, "keywords": [], "total": 0, "results": []}

    def _invalidate_chat_search(self, chat_id: str, ws_id: str = "") -> None:
        """Drop a chat from the global search index immediately (archive /
        delete) and wake the refresher so un-archive re-indexes promptly."""
        idx = self._chat_search
        if idx is None:
            return
        wsid = str(ws_id or "").strip()
        if not wsid:
            wsid = str(getattr(self.agent, "workspace_id", "") or "")
        try:
            idx.invalidate_chat(wsid, str(chat_id or ""))
            self._chat_search_wake.set()
        except Exception:
            pass

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
                items = snap.get("servers") if isinstance(snap, dict) else None
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
            # call. For tools, the settings row should show TOTAL catalog size,
            # not only currently enabled tools, so prefer the raw cache count
            # whenever it exists.
            tools_count = int(status_dict.get("tools_count") or 0)
            prompts_count = int(status_dict.get("prompts_count") or 0)
            if enabled:
                mgr_obj = getattr(agent, "mcp_manager", None)
                if mgr_obj is not None:
                    try:
                        cached = getattr(mgr_obj, "_tools_cache", {}).get(str(name), {})
                        cached_tools = cached.get("tools", []) if isinstance(cached, dict) else []
                        if isinstance(cached_tools, list) and cached_tools:
                            tools_count = len(cached_tools)
                    except Exception:
                        pass
                    if prompts_count == 0:
                        try:
                            cached = getattr(mgr_obj, "_prompts_cache", {}).get(str(name), {})
                            cached_prompts = cached.get("prompts", []) if isinstance(cached, dict) else []
                            if isinstance(cached_prompts, list):
                                prompts_count = len(cached_prompts)
                        except Exception:
                            pass
            # Use only runtime-discovered icon sources for GUI display.
            status_icon = str(status_dict.get("icon") or "").strip()
            icon = status_icon
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
                    "icon": icon,
                }
            )
        # Fire background pre-fetch to populate the icon cache.
        self._prefetch_mcp_icons(out)
        return {"servers": out}

    def _prefetch_mcp_icons(self, servers: List[Dict[str, Any]]) -> None:
        """Pre-fetch MCP server icons in a background thread to warm the cache."""
        icon_tasks: List[tuple] = []
        for srv in servers:
            srv_name = str(srv.get("name") or "").strip()
            srv_icon = str(srv.get("icon") or "").strip()
            if srv_name and srv_icon:
                icon_tasks.append((srv_name, srv_icon))
        if not icon_tasks:
            return

        def _worker() -> None:
            for srv_name, srv_icon in icon_tasks:
                try:
                    self.read_mcp_icon(srv_name, srv_icon)
                except Exception:
                    pass

        threading.Thread(
            target=_worker,
            name=f"mcp-icon-prefetch",
            daemon=True,
        ).start()

    def get_mcp_server_details(self, name: str) -> Dict[str, Any]:
        """Fetch the tool/prompt catalog for a single server (cache-first)."""
        srv = str(name or "").strip()
        if not srv:
            return {"ok": False, "tools": [], "prompts": [], "loading": False}
        agent = self.agent
        mgr = getattr(agent, "mcp_manager", None)
        if mgr is None:
            return {"ok": False, "tools": [], "prompts": [], "loading": False}
        tools: List[Dict[str, Any]] = []
        prompts: List[Dict[str, Any]] = []
        loading = False
        try:
            t, _ = mgr.list_tools_with_disabled(srv, use_cache=True)
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
        try:
            status_map = (mgr.get_status() or {}).get("servers", {})
            status = status_map.get(srv, {}) if isinstance(status_map, dict) else {}
            state = str(status.get("state") or "").strip().lower()
            loading = state in ("loading", "pending")
        except Exception:
            loading = False
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
            "loading": loading,
        }

    def _start_mcp_reconnect_async(self, server: str, timeout_s: float = 12.0) -> bool:
        """Reconnect one MCP server on a background thread.

        GUI settings handlers call this helper so an IO-bound MCP startup never
        blocks the HTTP request thread that is serving the settings page.
        """
        srv = str(server or "").strip()
        if not srv:
            return False
        mgr = getattr(self.agent, "mcp_manager", None)
        if mgr is None:
            return False
        with self._mcp_reconnect_lock:
            running = self._mcp_reconnect_threads.get(srv)
            if running is not None and running.is_alive():
                return False
            try:
                set_status = getattr(mgr, "_set_status", None)
                if callable(set_status):
                    set_status(srv, "loading", last_error="", failure_type="", suggestion="")
            except Exception:
                pass

            def _worker() -> None:
                try:
                    mgr.reconnect_server(srv, timeout_s=timeout_s)
                except Exception:
                    # ``McpManager`` already records failure status/logs.
                    pass
                finally:
                    with self._mcp_reconnect_lock:
                        current = self._mcp_reconnect_threads.get(srv)
                        if current is thread:
                            self._mcp_reconnect_threads.pop(srv, None)
                    self._reconcile_mcp_server_after_reconnect(srv)

            thread = threading.Thread(
                target=_worker,
                name=f"mcp-reconnect-{srv}",
                daemon=True,
            )
            self._mcp_reconnect_threads[srv] = thread
            thread.start()
        self._publish_mcp_state()
        return True

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
        except Exception:
            pass
        if enabled:
            self._start_mcp_reconnect_async(srv, timeout_s=12.0)
        elif not self._mcp_reconnect_running(srv):
            self._deactivate_mcp_server_runtime(srv)
        # Refresh tool specs so the model no longer sees disabled servers' tools.
        self._refresh_mcp_agent_tools()
        # Push fresh state so the page reflects the change immediately.
        self._publish_mcp_state()
        return True

    def set_plan_mode(
        self, enabled: bool, chat_id: str = "", workspace_id: str = ""
    ) -> bool:
        """Toggle the agent's sticky plan mode.

        The GUI uses this to keep the user's message bubble free of any
        injected planning instruction: instead of prefixing the outgoing
        message text, the GUI flips this flag and lets ``runtime_loop`` append
        the localized directive (send-time only) inside the agent boundary. The
        flag is shared with the TUI's ``/plan`` command — flipping it from the
        GUI is equivalent to a TUI ``/plan on`` for the same process — and is
        mirrored onto the chat record root so a chat reload resumes it.

        ``chat_id`` + ``workspace_id`` identify the chat explicitly (chat ids
        repeat across workspaces); empty values fall back to the focused chat.
        """
        try:
            self.agent._plan_mode_sticky = bool(enabled)
        except Exception:
            return False
        try:
            manager = getattr(self.agent, "_chat_state_manager", None)
            persist = getattr(manager, "persist_active_chat_plan_mode", None)
            if callable(persist):
                # This runs on an HTTP handler thread where
                # ``agent.active_chat_id`` is the thread's (empty) session, so
                # persisting without binding to the focused chat finds no active
                # chat and silently drops the toggle (the "sometimes not saved"
                # bug). Bind to the validated (workspace_id, chat_id) pair.
                cid, wsid = self._resolve_chat_scope(chat_id, workspace_id)
                if cid:
                    with self._session_scope_for_chat(cid, wsid):
                        persist(bool(enabled))
                else:
                    persist(bool(enabled))
        except Exception:
            pass
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
            agent = self.agent
            agent.tool_specs = agent._load_tools_spec_from_jsonc()
            agent.system_prompt = agent._compose_system_prompt_snapshot(include_tools=True)
        except Exception:
            return False
        return True

    def set_mcp_tools_enabled(
        self, server: str, tools: Any, enabled: bool
    ) -> bool:
        srv = str(server or "").strip()
        if not srv:
            return False
        if not isinstance(tools, (list, tuple)):
            return False
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
            agent = self.agent
            agent.tool_specs = agent._load_tools_spec_from_jsonc()
            agent.system_prompt = agent._compose_system_prompt_snapshot(include_tools=True)
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
        except Exception:
            pass
        if reconnect:
            self._start_mcp_reconnect_async(server, timeout_s=12.0)
        try:
            self.broadcaster.publish(
                "idle", self._route(state=_build_state(self.agent))
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
                "idle", self._route(state=_build_state(self.agent))
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

    # ----- Skills config CRUD -------------------------------------------
    _SKILLS_CONFIG_PATH = "skills.jsonc"

    def _skills_config_dir_for(self, source: str) -> Path:
        """Return the config directory for a skill based on its source.
        Workspace / workspaceAgents skills use <workspace>/.codewood/, everything else uses the global config dir.
        """
        if source in ("workspace", "workspaceAgents"):
            ws_cfg = getattr(self.agent, "workspace_config_dir", None)
            if ws_cfg:
                return Path(ws_cfg)
        return self.agent.config_dir

    def _skills_load_jsonc(self, source: str = "builtin") -> Dict[str, Any]:
        """Load skills configuration from the appropriate config dir."""
        try:
            from ..core.config.config_jsonc import load_config_jsonc

            cfg_dir = self._skills_config_dir_for(source)
            path = cfg_dir / self._SKILLS_CONFIG_PATH
            if not path.is_file():
                return {"disabledSkills": []}
            data = load_config_jsonc(path) or {}
            if not isinstance(data, dict):
                return {"disabledSkills": []}
            disabled = data.get("disabledSkills")
            if not isinstance(disabled, list):
                data["disabledSkills"] = []
            else:
                data["disabledSkills"] = [str(s) for s in disabled if isinstance(s, str) and s.strip()]
            return data
        except Exception:
            return {"disabledSkills": []}

    def _skills_save_jsonc(self, data: Dict[str, Any], source: str = "builtin") -> bool:
        try:
            from ..core.config.config_jsonc import save_config_jsonc

            cfg_dir = self._skills_config_dir_for(source)
            path = cfg_dir / self._SKILLS_CONFIG_PATH
            save_config_jsonc(path, data)
            return True
        except Exception:
            return False

    def get_skills_overview(self) -> Dict[str, Any]:
        """Return all non-workspace skills with source and enabled/disabled status.
        Workspace and workspaceAgents skills are excluded — they belong to workspace-level settings.
        """
        skills_data: List[Dict[str, object]] = []
        try:
            for record in getattr(self.agent, "skills", []) or []:
                source = str(getattr(record, "source", "builtin") or "builtin")
                if source in ("workspace", "workspaceAgents"):
                    continue
                skill_id = str(getattr(record, "skill_id", "") or "")
                name = str(getattr(record, "name", "") or "")
                desc = str(getattr(record, "description", "") or "")
                cfg = self._skills_load_jsonc(source)
                disabled_set: set = set(cfg.get("disabledSkills") or [])
                enabled = skill_id not in disabled_set
                skills_data.append({
                    "skillId": skill_id,
                    "name": name,
                    "description": desc,
                    "source": source,
                    "enabled": enabled,
                })
        except Exception:
            skills_data = []

        return {"ok": True, "skills": skills_data}

    def _find_skill_source(self, skill_id: str) -> Optional[str]:
        """Look up a skill's source from the loaded skills list."""
        for record in getattr(self.agent, "skills", []) or []:
            if str(getattr(record, "skill_id", "") or "") == skill_id:
                return str(getattr(record, "source", "builtin") or "builtin")
        return None

    def set_skill_enabled(self, skill_id: str, enabled: bool) -> bool:
        """Enable or disable a skill. Config file determined by skill source:
        workspace skills -> <workspace>/.codewood/skills.jsonc
        all others -> <global_config_dir>/skills.jsonc
        """
        sid = str(skill_id or "").strip()
        if not sid:
            return False
        try:
            source = self._find_skill_source(sid) or "builtin"
            cfg = self._skills_load_jsonc(source)
            disabled: list = list(cfg.get("disabledSkills") or [])
            if enabled:
                disabled = [s for s in disabled if s != sid]
            else:
                if sid not in disabled:
                    disabled.append(sid)
            cfg["disabledSkills"] = disabled
            if not self._skills_save_jsonc(cfg, source):
                return False
        except Exception:
            return False
        return True

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
                "idle", self._route(state=_build_state(self.agent))
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
            # Refresh the live agent params so the current model's
            # provider/model params reflect the just-saved config without
            # requiring a model switch.
            try:
                current = agent._current_model_selector().lower()
                if current:
                    choice = agent._find_configured_model_choice(current)
                    if choice:
                        agent._apply_runtime_model_choice(choice, validate=False)
            except Exception:
                pass
        except Exception:
            return False
        # Refresh GUI clients with the new available-model list.
        try:
            self.broadcaster.publish("idle", {"state": self.state()})
        except Exception:
            pass
        return True

    def sync_model_presets(self, app_presets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Merge app-level presets into ``model_presets.json`` and return the merged list.

        Existing presets keep their current values, but newly-added fields from
        bundled presets are backfilled when absent so the GUI can adopt new
        default options without overwriting user edits. New presets (by ``id``)
        that exist in ``app_presets`` but not in the file are appended.
        """
        if not isinstance(app_presets, list):
            return []
        path = self.agent.config_dir / "model_presets.json"
        existing: List[Dict[str, Any]] = []
        if path.exists():
            try:
                raw = path.read_text(encoding="utf-8")
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    existing = parsed
            except Exception:
                pass
        existing_by_id = {
            str(p.get("id", "")): p for p in existing if isinstance(p, dict) and str(p.get("id", ""))
        }
        merged = list(existing)
        for idx, preset in enumerate(merged):
            if not isinstance(preset, dict):
                continue
            preset_id = str(preset.get("id", ""))
            if not preset_id:
                continue
            fallback = next(
                (
                    item for item in app_presets
                    if isinstance(item, dict) and str(item.get("id", "")) == preset_id
                ),
                None,
            )
            if not isinstance(fallback, dict):
                continue
            for key, value in fallback.items():
                if key not in preset:
                    preset[key] = value
        for p in app_presets:
            if isinstance(p, dict) and str(p.get("id", "")) not in existing_by_id:
                merged.append(p)
        try:
            path.write_text(
                json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass
        return merged

    def get_model_presets(self) -> List[Dict[str, Any]]:
        """Read the current ``model_presets.json``, or return empty list."""
        path = self.agent.config_dir / "model_presets.json"
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []

    def fetch_provider_models(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch the model list from an OpenAI-compatible provider.

        Expects ``{base_url, api_key, api_mode}`` (api_key may be a ``${ENV}``
        placeholder, which is resolved before the call).  When
        ``context_length_attr_name`` is provided and non-empty, each returned
        model dict may include a ``context_window`` integer.

        Results are cached in ``config/cache/`` to reduce network requests
        across page reloads.
        """
        import json
        import hashlib
        from pathlib import Path
        from ..core.config.config_env import resolve_string_values_in_data

        base_url = str(params.get("base_url") or "").strip()
        api_key_raw = str(params.get("api_key") or "").strip()
        api_mode = str(params.get("api_mode") or "").strip().lower()
        context_attr = str(params.get("context_length_attr_name") or "").strip()

        # ---- Ollama path (no context attribute support) ----
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

                models = fetch_openai_compatible_models(
                    base_url=base_url, api_key=api_key,
                )
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

        # ---- Cache key derived from base_url + attribute name ----
        cache_dir = Path(self.agent.config_dir) / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_key = hashlib.sha256(
            f"{base_url}|{context_attr}".encode()
        ).hexdigest()[:16]
        cache_path = cache_dir / f"models_{cache_key}.json"

        # Try reading from cache first (5-minute TTL).
        import time

        now = time.time()
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(cached, dict):
                    ts = cached.get("_ts", 0)
                    if now - ts < 300 and isinstance(cached.get("models"), list):
                        result = {"ok": True, "models": cached["models"]}
                        return result
            except Exception:  # noqa: BLE001 - stale cache, ignore
                pass

        # ---- Live fetch ----
        try:
            from ..ai.ai_provider_clients import fetch_openai_compatible_models

            models = fetch_openai_compatible_models(
                base_url=base_url,
                api_key=api_key,
                context_length_attr_name=context_attr,
            )
            # Write to cache.
            try:
                cache_path.write_text(
                    json.dumps({"_ts": now, "models": models}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:  # noqa: BLE001 - cache write failure is non-fatal
                pass
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

    def open_folder(self, folder_path: str) -> Optional[Dict[str, str]]:
        """Open a folder as a workspace, switching to it without creating any chat.

        Called from the GUI's File > Open Folder / Ctrl+O / native menu.
        Unlike ``/workspace create`` which goes through the chat runtime loop,
        this executes on the HTTP thread directly, avoiding session-binding
        bleed from the old runtime. Returns the created/existing workspace id
        and name, then broadcasts the new state via SSE.
        """
        from ..config.app_info import get_app_config_dirname as _cfg_dirname
        from ..controllers.workspace_command_controller import _default_workspace_id

        agent = self.agent
        p = str(folder_path or "").strip().strip('"').strip("'")
        if not p:
            return None
        try:
            raw_path = agent._workspace_path_from_arg(p)
        except Exception:
            return None
        root = agent._resolve_path_lenient(raw_path)
        name = root.name or str(root)

        try:
            # If this folder is already a registered workspace, just switch.
            existing = agent._workspace_entry_by_root(root)
            if existing:
                wsid = str(existing.get("id") or "")
                agent._save_current_workspace_position()
                agent._apply_workspace_entry(existing, agent.work_directory)
                try:
                    from ..core.sandbox import refresh_workspace_acls

                    refresh_workspace_acls(agent, str(root))
                except Exception:
                    pass
                agent._refresh_workspace_runtime(create_default_chat=False, lazy_records=True)
                agent._save_current_workspace_position(sync_messages=False)
                self.broadcaster.publish(
                    "idle", self._route(state=_build_state(agent))
                )
                return {"id": wsid, "name": str(existing.get("name") or ""), "existing": True}

            # New workspace: register it.
            root.mkdir(parents=True, exist_ok=True)
            storage = root / _cfg_dirname()
            storage.mkdir(parents=True, exist_ok=True)

            workspace_id = agent._workspace_id_for_path(root)
            base_id = workspace_id
            counter = 2
            workspaces = agent._workspaces_state.setdefault("workspaces", {})
            while workspace_id in workspaces:
                workspace_id = f"{base_id}_{counter}"
                counter += 1

            workspaces[workspace_id] = {
                "id": workspace_id,
                "name": name,
                "kind": "custom",
                "root": str(root),
            }
            agent._save_workspace_state()

            agent._save_current_workspace_position()
            agent._apply_workspace_entry(
                workspaces[workspace_id], agent.work_directory
            )
            try:
                from ..core.sandbox import refresh_workspace_acls

                refresh_workspace_acls(agent, str(root))
            except Exception:
                pass
            # Don't auto-create a default chat — the GUI enters draft mode.
            agent._refresh_workspace_runtime(create_default_chat=False, lazy_records=True)
            agent._save_current_workspace_position(sync_messages=False)
        except Exception:
            return None

        self.broadcaster.publish(
            "idle", self._route(state=_build_state(agent))
        )
        return {"id": workspace_id, "name": name, "existing": False}

    def delete_workspace(self, workspace_id: str) -> Optional[Dict]:
        """Archive (delete) a workspace, falling back to another workspace if
        the deleted one was active.

        The workspace entry is kept in the registry and flagged ``archived``;
        its chat data stays in the global chats directory and remains visible
        — and deletable — from the 设置/已归档 settings page. Executes on the
        HTTP thread — bypassing the chat runtime — so the SSE broadcast
        carries a clean state with no session-bleed from the old chat.
        """
        from ..controllers.workspace_command_controller import _default_workspace_id

        agent = self.agent
        wsid = str(workspace_id or "").strip()
        if not wsid:
            return None

        entry = agent._workspace_entry_by_selector(wsid)  # type: ignore[attr-defined]
        if not entry:
            return None
        if wsid == _default_workspace_id():
            return None  # default workspace can't be deleted

        active_deleted = wsid == str(getattr(agent, "workspace_id", "") or "")
        if active_deleted:
            agent._save_current_workspace_position()

        # The workspace is NOT removed from the registry. It is flagged as
        # archived so its chat records stay in place (chats live in the global
        # chats directory and are never deleted by a workspace delete) and
        # remain reachable — and deletable — from the 设置/已归档 settings page.
        # The sidebar hides archived workspaces; only the archive page lists
        # them.
        workspaces = agent._workspaces_state.get("workspaces", {})
        if isinstance(workspaces, dict):
            if wsid in workspaces:
                workspaces[wsid]["archived"] = True
        from ..managers.chat_state_manager import archive_workspace_chats

        archive_workspace_chats(wsid, agent)

        # The workspace is forgotten: revoke the sandbox users/group/capability
        # SIDs' ACLs on its directory tree so the sandbox keeps no access to a
        # directory the app no longer tracks. This walks the whole tree and
        # can take a while on large projects, so it runs in the background.
        # Best-effort, never raises.
        deleted_root = str(entry.get("root") or "")
        if deleted_root:
            try:
                from ..core.sandbox import cleanup_workspace_acls

                def _cleanup_in_background() -> None:
                    try:
                        cleanup_workspace_acls(agent, deleted_root)
                    except Exception:
                        pass

                threading.Thread(
                    target=_cleanup_in_background, daemon=True
                ).start()
            except Exception:
                pass

        fallback_id = ""
        if active_deleted:
            default_ws_id = _default_workspace_id()
            default_entry = agent._default_workspace_entry()  # type: ignore[attr-defined]
            if isinstance(workspaces, dict):
                workspaces[default_ws_id] = default_entry
            agent._apply_workspace_entry(default_entry, agent.work_directory)
            try:
                from ..core.sandbox import refresh_workspace_acls

                refresh_workspace_acls(
                    agent,
                    str(default_entry.get("root") or "")
                    or str(getattr(agent, "workspace_root", "") or ""),
                )
            except Exception:
                pass
            # Don't auto-create a default chat; the frontend will enter
            # draft mode when the fallback workspace has no chats.
            agent._save_current_workspace_position(sync_messages=False)
            agent._refresh_workspace_runtime(create_default_chat=False, lazy_records=True)
            fallback_id = default_ws_id
        else:
            agent._save_workspace_state()

        agent._refresh_input_handler_skill_completions()  # type: ignore[attr-defined]
        self.broadcaster.publish(
            "idle", self._route(state=_build_state(agent))
        )
        return {"id": wsid, "wasActive": active_deleted, "fallbackId": fallback_id, "archived": True}

    def new_chat(self, workspace_id: str = "", model: str = "", reasoning: str = "") -> Optional[str]:
        """Silently create and activate a new chat; return its id.

        Not refused while other chats are running: the new chat gets its own
        loop thread on first input and is independent of any in-flight turn.

        ``model`` an optional ``provider/model_name`` selector to apply
        atomically during chat creation, overriding the default inheritance
        from the last used chat. Used by the GUI draft mode to prevent a
        race where the idle event carries the inherited model and overwrites
        the frontend's optimistic model update.

        ``workspace_id`` optionally switches the focused workspace FIRST, so the
        new chat is created in the target workspace as a single atomic op. The
        GUI uses this to materialize a draft chat for a specific workspace
        without a separate ``select_chat`` round-trip — doing both separately
        previously left an extra empty chat behind when the target workspace
        switch incidentally activated/created a chat before the new one.
        """
        import contextlib

        agent = self.agent
        try:
            wsid = str(workspace_id or "").strip()
            if wsid and wsid != str(getattr(agent, "workspace_id", "") or ""):
                from ..controllers.workspace_command_controller import (
                    workspace_switch_command,
                )

                with contextlib.redirect_stdout(io.StringIO()):
                    workspace_switch_command(agent, wsid, lazy_records=True)
                with self._ws_persist_lock:
                    self._ws_persist_ctx.clear()

            from ..core.localization import get_display_language, translate

            name = translate("chat.new.default_name", get_display_language(agent))
            with agent._chat_state_lock:
                cid = agent._next_chat_id()
                entry = agent._new_chat_entry(cid, name=name)
                model_sel = str(model or "").strip()
                if model_sel and "/" in model_sel:
                    parts = model_sel.split("/", 1)
                    entry["model_provider"] = parts[0].strip()
                    entry["model_name"] = parts[1].strip()
                    # Reset reasoning level since the new model may not support
                    # the previous chat's reasoning effort.
                    entry["reasoning_level"] = ""
                reasoning_sel = str(reasoning or "").strip()
                if reasoning_sel:
                    entry["reasoning_level"] = reasoning_sel
                agent._chat_entries().append(entry)
                agent._save_chat_state()
                agent._activate_chat(
                    cid, announce=False, clear_screen=False, print_history=False
                )
                # The GUI user is now composing in this new chat, so record it
                # as the focused chat (mirrors select_chat). Without this the
                # focus marker still points at the previously opened chat and
                # the new chat's first completed turn is misclassified as a
                # background completion, leaving a persistent unread dot on a
                # chat the user watched finish.
                track = getattr(self, "_track_focus", None)
                if callable(track):
                    # ``None`` (not ``""``) makes _runtime_key fall back to the
                    # agent's current workspace, matching how runtimes resolve it.
                    track(cid, wsid or None)
                # Snapshot the state while the lock is held so a concurrent
                # background load_chat_state can't replace agent._chat_state
                # with stale data before we publish the idle event.
                idle_state = _build_state(agent)
                idle_payload = self._route(state=idle_state)
        except Exception:
            return None
        self.broadcaster.publish("idle", idle_payload)
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

            original_wsid = str(getattr(agent, "workspace_id", "") or "").strip()
            switched = bool(wsid and wsid != original_wsid)

            if switched:
                with agent._chat_state_lock:
                    workspace_switch_command(agent, wsid, lazy_records=True)
            with agent._chat_state_lock:
                target = agent._resolve_chat_selector(cid)
                rid = str(target.get("id") or "") if target else ""
            if not rid:
                return False
            # Refuse to delete a chat whose loop is mid-task; the user should
            # interrupt it first.
            rkey = self._runtime_key(rid, wsid or None)
            with self._runtimes_lock:
                rt = self._runtimes.get(rkey)
                if rt is not None and rt.busy.is_set():
                    return False
            was_active = False
            remaining: list = []
            with agent._chat_state_lock:
                chats = agent._chat_entries()
                # ``agent.active_chat_id`` is thread-bound and empty on the HTTP
                # handler thread, so resolve the active chat from the stable
                # cross-thread index instead.
                was_active = rid == _primary_active_chat_id(agent)
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
            # Persist outside the lock so a concurrent _save_chat_state (held
            # by an in-progress task loop) does not block the HTTP handler.
            agent._save_chat_state()
            # Drop the deleted chat's runtime (if any) and its session, keyed by
            # the workspace-qualified composite.
            skey = agent._session_registry_key_for(rid, wsid) if wsid else agent._session_registry_key(rid)
            with self._runtimes_lock:
                self._runtimes.pop(rkey, None)
            try:
                reg = agent.__dict__.get("_session_registry")
                if isinstance(reg, dict):
                    reg.pop(skey, None)
            except Exception:
                pass
            if remaining and was_active:
                next_id = str(agent._chat_state.get("active") or "")
                if next_id:
                    with self._runtimes_lock:
                        has_runtime = next_id in self._runtimes
                    if not has_runtime:
                        # Hydrate the next chat from disk before activating it:
                        # after a lazy workspace switch the in-memory entry may
                        # still be a summary placeholder without messages.
                        try:
                            agent._refresh_chat_record_from_disk(next_id)
                        except Exception:
                            pass
                        agent._activate_chat(
                            next_id,
                            announce=False,
                            clear_screen=False,
                            print_history=False,
                        )
            # Switch back to the original workspace so the idle event is
            # broadcast from the focused workspace, not the deleted chat's
            # workspace. Otherwise the frontend ignores the state update
            # (idleForFocused=false) and marks remaining chats as unread.
            if switched:
                with agent._chat_state_lock:
                    workspace_switch_command(agent, original_wsid, lazy_records=True)
        except Exception:
            return False
        self.broadcaster.publish(
            "idle", self._route(state=_build_state(agent))
        )
        self._invalidate_chat_search(rid, wsid)
        return True

    def toggle_chat_archive(self, chat_id: str, ws_id: str = "") -> bool:
        """Toggle the ``archived`` flag on a chat record (GUI-only, persistent).

        Operates directly on the workspace's chat index without switching the
        active workspace, so the UI is never disrupted.
        """
        agent = self.agent
        wsid = str(ws_id or "").strip()
        cid = str(chat_id or "").strip()
        if not cid:
            return False
        is_active = not wsid or wsid == str(getattr(agent, "workspace_id", "") or "")
        try:
            if is_active:
                # Active workspace: toggle in-memory, save, broadcast.
                with agent._chat_state_lock:
                    target = agent._resolve_chat_selector(cid)
                    rid = str(target.get("id") or "") if target else ""
                    if not rid:
                        return False
                    chats = agent._chat_entries()
                    for c in chats:
                        if str(c.get("id") or "") == rid:
                            c["archived"] = not bool(c.get("archived", False))
                            c["updated_at"] = datetime.datetime.now().strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                            agent._save_chat_state()
                            break
                    else:
                        return False
                self.broadcaster.publish(
                    "idle", self._route(state=_build_state(agent))
                )
                self._invalidate_chat_search(rid, ws_id)
                return True

            # Non-active workspace: update its chat index directly on disk
            # without switching the active workspace.
            entry = agent._workspace_entry_by_selector(wsid)
            if not entry:
                return False
            index_path = get_app_global_config_dir() / "chats" / f"{wsid}.json"
            if not index_path.exists():
                return False
            with open(index_path, "r", encoding="utf-8") as f:
                index = json.load(f)
            if not isinstance(index, dict):
                return False
            file_wsid = str(index.get("workspace_id") or "").strip()
            if file_wsid and file_wsid != wsid:
                logger.warning(
                    "toggle_chat_archive: refusing to modify %s — index "
                    "workspace_id=%r != requested workspace_id=%r",
                    index_path, file_wsid, wsid,
                )
                return False
            chats = index.get("chats")
            if not isinstance(chats, list):
                return False
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for c in chats:
                if not isinstance(c, dict):
                    continue
                if str(c.get("id") or "") == cid:
                    c["archived"] = not bool(c.get("archived", False))
                    c["updated_at"] = now
                    with open(index_path, "w", encoding="utf-8") as f:
                        json.dump(index, f, ensure_ascii=False, indent=2)
                    self._invalidate_chat_search(cid, ws_id)
                    return True
            return False
        except Exception:
            return False

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
                            "archived": bool(c.get("archived", False)),
                            "hasUnread": bool(c.get("has_unread", False)),
                            "model": _chat_model_selector(c),
                            "reasoning": str(c.get("reasoning_level") or ""),
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
            return _read_workspace_chat_index(ws_id)
        return None

    # ----- file change undo / reapply -----------------------------------

    @staticmethod
    def _read_file_for_patch(file_path: str) -> tuple:
        """Read file using the same encoding detection as apply_patch.

        Returns ``(lines, codec, bom_bytes, newline)`` where *lines* is the
        BOM-free split content (ready for line-by-line comparison against
        DiffRow[]), *codec* is the detected encoding name, *bom_bytes* is
        the BOM prefix, and *newline* is the detected line ending (one of
        ``"\r\n"``, ``"\n"``, ``"\r"``).  Callers MUST use
        ``_write_patched_file`` to preserve the encoding + BOM + newline
        when writing back.
        """
        from ..tools.apply_patch import _read_text_preserving_encoding
        from pathlib import Path as _Path
        path = _Path(file_path)
        content, codec, bom = _read_text_preserving_encoding(path)
        newline = "\n"
        try:
            raw = path.read_bytes()
            if b"\r\n" in raw:
                newline = "\r\n"
            elif b"\r" in raw:
                newline = "\r"
        except Exception:
            pass
        lines = content.splitlines()
        return lines, codec, bom, newline

    @staticmethod
    def _write_patched_file(
        file_path: str, result_lines: List[str], codec: str, bom_bytes: bytes,
        newline: str = "\n",
    ) -> Dict[str, Any]:
        """Write patched result back, preserving the original encoding, BOM,
        and line-ending style."""
        from pathlib import Path as _Path
        try:
            data = newline.join(result_lines)
            if result_lines:
                data += newline
            raw = bom_bytes + data.encode(codec, errors="replace")
            _Path(file_path).write_bytes(raw)
            return {"success": True}
        except Exception as exc:
            return {"success": False, "error": f"write failed: {exc}"}

    @staticmethod
    def _apply_reverse_patch(file_path: str, diff_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Reverse-apply DiffRow[] to *file_path*, returning {success, error?}.

        Walks the current file line-by-line guided by ``newNo`` in each DiffRow,
        verifying every line affected by the patch matches the expected post-change
        content. Returns ``{"success": True}`` on success, or
        ``{"success": False, "error": "Conflict at line N"}`` on failure.
        """
        try:
            current, codec, bom, newline = ServeApp._read_file_for_patch(file_path)
        except Exception:
            return {"success": False, "error": "cannot read file"}
        rows = [
            r for r in diff_rows
            if isinstance(r, dict) and r.get("type") not in ("omitted",)
        ]
        result: List[str] = []
        file_pos = 0  # 0-based index into *current*
        result_has_bom = False

        for row in rows:
            nn = row.get("newNo")
            row_type = row.get("type", "")
            if nn is not None and isinstance(nn, int) and nn >= 1:
                while file_pos < nn - 1:
                    if file_pos >= len(current):
                        return {"success": False, "error": "file shorter than expected"}
                    result.append(current[file_pos])
                    file_pos += 1
                if row_type == "context":
                    if file_pos >= len(current):
                        return {"success": False, "error": f"unexpected eof at line {nn}"}
                    expected = _strip_bom(str(row.get("newText", "")))
                    actual = _strip_bom(current[file_pos])
                    if expected and actual != expected:
                        return {"success": False, "error": f"conflict at line {nn}"}
                    result.append(actual if actual == current[file_pos] else current[file_pos])
                    file_pos += 1
                elif row_type == "add":
                    if file_pos >= len(current):
                        return {"success": False, "error": f"unexpected eof at line {nn}"}
                    expected = _strip_bom(str(row.get("newText", "")))
                    actual = _strip_bom(current[file_pos])
                    if expected and actual != expected:
                        return {"success": False, "error": f"conflict at line {nn}"}
                    file_pos += 1
                elif row_type == "change":
                    if file_pos >= len(current):
                        return {"success": False, "error": f"unexpected eof at line {nn}"}
                    expected = _strip_bom(str(row.get("newText", "")))
                    actual = _strip_bom(current[file_pos])
                    if expected and actual != expected:
                        return {"success": False, "error": f"conflict at line {nn}"}
                    old_text = str(row.get("oldText", ""))
                    if old_text.startswith("\ufeff"):
                        result_has_bom = True
                    result.append(old_text)
                    file_pos += 1
                elif row_type == "del":
                    result.append(current[file_pos])
                    file_pos += 1
                else:
                    result.append(current[file_pos])
                    file_pos += 1
            else:
                if row_type == "del":
                    old_text = str(row.get("oldText", ""))
                    if old_text.startswith("\ufeff"):
                        result_has_bom = True
                    result.append(old_text)

        while file_pos < len(current):
            result.append(current[file_pos])
            file_pos += 1

        if result_has_bom:
            if not bom:
                bom = b"\xef\xbb\xbf"
            # Always strip the BOM character from the first line so it is
            # not encoded twice (once as bom_bytes, once in the text),
            # regardless of whether the current file already had a BOM.
            if result and result[0].startswith("\ufeff"):
                result[0] = result[0][1:]
        return ServeApp._write_patched_file(file_path, result, codec, bom, newline)

    @staticmethod
    def _apply_forward_patch(file_path: str, diff_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Forward-apply DiffRow[] to *file_path*, returning {success, error?}.

        Walks the current file (assumed to be in the pre-change / undone state)
        guided by ``oldNo``.  Verification uses ``oldText``; conflict = abort.
        """
        try:
            current, codec, bom, newline = ServeApp._read_file_for_patch(file_path)
        except Exception:
            return {"success": False, "error": "cannot read file"}
        rows = [
            r for r in diff_rows
            if isinstance(r, dict) and r.get("type") not in ("omitted",)
        ]
        result: List[str] = []
        file_pos = 0

        for row in rows:
            on = row.get("oldNo")
            row_type = row.get("type", "")
            if on is not None and isinstance(on, int) and on >= 1:
                while file_pos < on - 1:
                    if file_pos >= len(current):
                        return {"success": False, "error": "file shorter than expected"}
                    result.append(current[file_pos])
                    file_pos += 1
                if row_type == "context":
                    if file_pos >= len(current):
                        return {"success": False, "error": f"unexpected eof at line {on}"}
                    expected = _strip_bom(str(row.get("oldText", "")))
                    actual = _strip_bom(current[file_pos])
                    if expected and actual != expected:
                        return {"success": False, "error": f"conflict at line {on}"}
                    result.append(actual)
                    file_pos += 1
                elif row_type == "del":
                    if file_pos >= len(current):
                        return {"success": False, "error": f"unexpected eof at line {on}"}
                    expected = _strip_bom(str(row.get("oldText", "")))
                    actual = _strip_bom(current[file_pos])
                    if expected and actual != expected:
                        return {"success": False, "error": f"conflict at line {on}"}
                    file_pos += 1
                elif row_type == "change":
                    if file_pos >= len(current):
                        return {"success": False, "error": f"unexpected eof at line {on}"}
                    expected = _strip_bom(str(row.get("oldText", "")))
                    actual = _strip_bom(current[file_pos])
                    if expected and actual != expected:
                        return {"success": False, "error": f"conflict at line {on}"}
                    result.append(str(row.get("newText", "")))
                    file_pos += 1
                elif row_type == "add":
                    result.append(current[file_pos])
                    file_pos += 1
                else:
                    result.append(current[file_pos])
                    file_pos += 1
            else:
                if row_type == "add":
                    result.append(str(row.get("newText", "")))

        while file_pos < len(current):
            result.append(current[file_pos])
            file_pos += 1

        return ServeApp._write_patched_file(file_path, result, codec, bom, newline)

    @staticmethod
    def _reconstruct_expected_from_diffrows(
        diff_rows: List[Dict[str, Any]],
    ) -> List[str]:
        """Reconstruct the *new* content that a forward patch produces applied to
        an empty file. Used for conflict detection on ``create`` type changes."""
        rows = [
            r for r in diff_rows
            if isinstance(r, dict) and r.get("type") not in ("omitted",)
        ]
        result: List[str] = []
        for row in rows:
            t = row.get("type", "")
            if t in ("context", "add", "change"):
                result.append(str(row.get("newText", "")))
            # del: not present in new content
        return result

    @staticmethod
    def _normalized_undo_lines(lines: List[str]) -> List[str]:
        """Normalize a line list for undo conflict comparison.

        Recorded content may carry a ``\\ufeff`` BOM character on the first
        line (``Path.read_text`` keeps it), while on-disk reads via
        ``_read_text_preserving_encoding`` strip the BOM bytes.  Without this
        normalization, undoing a create/rename of a BOM file always reports
        "file modified since creation".
        """
        if lines and str(lines[0]).startswith("\ufeff"):
            lines = [str(lines[0])[1:]] + list(lines[1:])
        return lines

    def _lookup_file_change(
        self, chat_id: str, ref: str, file_path: str, workspace_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Find a single file change record by chat, ref, and file path.

        ``workspace_id`` qualifies the chat (chat ids repeat across workspaces).
        """
        scope_key = ServeApp._file_changes_scope_key(chat_id, workspace_id)
        _fc_by_chat: Dict[str, Any] = dict(
            getattr(self.agent, "_file_changes_by_chat", {}) or {}
        )
        store = _fc_by_chat.get(scope_key, {})
        if not isinstance(store, dict):
            return None
        summary = store.get(str(ref or ""))
        if not isinstance(summary, dict):
            # Try loading from disk (workspace-aware sidecar).
            data_dir = self._chat_data_dir_for(chat_id, workspace_id)
            if data_dir is not None:
                on_disk = None
                try:
                    with open(data_dir / "file_changes.json", "r", encoding="utf-8") as _fh:
                        on_disk = json.load(_fh)
                except Exception:
                    on_disk = None
                if isinstance(on_disk, dict):
                    maybe_summary = on_disk.get(str(ref or ""))
                    if isinstance(maybe_summary, dict):
                        summary = maybe_summary
                    elif on_disk.get("ref") == ref:
                        summary = on_disk
                elif isinstance(on_disk, list):
                    for item in on_disk:
                        if isinstance(item, dict) and item.get("ref") == ref:
                            summary = item
                            break
        if not isinstance(summary, dict):
            return None
        files = summary.get("files")
        if not isinstance(files, list):
            return None
        norm = os.path.normcase(os.path.normpath(str(file_path or "")))
        for fc in files:
            if not isinstance(fc, dict):
                continue
            if os.path.normcase(os.path.normpath(str(fc.get("filePath", "")))) == norm:
                return fc
        return None

    def _update_undone_files_state(
        self,
        chat_id: str,
        ref: str,
        file_paths: List[str],
        undone: bool,
        workspace_id: str = "",
    ) -> None:
        """Mark files as undone/redone in the file-changes store and persist to disk.

        ``workspace_id`` qualifies the chat (chat ids repeat across workspaces).
        """
        scope_key = ServeApp._file_changes_scope_key(chat_id, workspace_id)
        _fc_by_chat: Dict[str, Any] = dict(
            getattr(self.agent, "_file_changes_by_chat", {}) or {}
        )
        store: dict = dict(_fc_by_chat.get(scope_key, {}))
        summary = store.get(str(ref or ""))
        if not isinstance(summary, dict):
            return
        undone_list: List[str] = list(summary.get("undoneFiles") or [])
        if undone:
            for p in file_paths:
                if p not in undone_list:
                    undone_list.append(p)
        else:
            undone_list = [p for p in undone_list if p not in file_paths]
        summary["undoneFiles"] = undone_list
        store[str(ref or "")] = summary
        _fc_by_chat[scope_key] = store
        setattr(self.agent, "_file_changes_by_chat", _fc_by_chat)
        try:
            data_dir = self._chat_data_dir_for(chat_id, workspace_id)
            if data_dir is not None:
                data_dir.mkdir(parents=True, exist_ok=True)
                from ..managers.chat_state_manager import _safe_replace
                import json as _json
                target = data_dir / "file_changes.json"
                tmp = target.with_suffix(target.suffix + ".tmp")
                with open(tmp, "w", encoding="utf-8") as _fh:
                    _json.dump(store, _fh, ensure_ascii=False, indent=2)
                    _fh.write("\n")
                _safe_replace(tmp, target)
        except Exception:
            pass

    def _resolve_backup_full_path(
        self, chat_id: str, backup_name: str, workspace_id: str = "",
    ) -> Optional[Path]:
        """Convert a relative backup filename to an absolute path under the chat's
        backups directory (workspace-qualified)."""
        data_dir = self._chat_data_dir_for(chat_id, workspace_id)
        if data_dir is None:
            return None
        return data_dir / "backups" / str(backup_name)

    def undo_file_changes(
        self, chat_id: str, ref: str, files: List[str], workspace_id: str = "",
    ) -> Dict[str, Any]:
        """Undo a list of files.  Returns {results: {filePath: {success, error?}}}.

        ``workspace_id`` scopes the chat (chat ids repeat across workspaces).

        Process files in reverse order so that later operations (e.g. delete,
        rename) are undone first, restoring the file before earlier operations
        (e.g. modify) are undone.
        """
        outcome: Dict[str, Dict[str, Any]] = {}
        files = list(reversed(files))
        for fpath in files:
            try:
                fc = self._lookup_file_change(chat_id, ref, fpath, workspace_id)
                if fc is None:
                    outcome[fpath] = {"success": False, "error": "change record not found"}
                    continue
                ct = str(fc.get("changeType", ""))
                diff = fc.get("patch")
                if isinstance(diff, list) and len(diff) > 0 and all(
                    isinstance(r, dict) for r in diff
                ):
                    pass
                else:
                    diff = None

                if ct == "modify" and diff is not None:
                    # Text-file modify: reverse-apply DiffRow[].
                    outcome[fpath] = self._apply_reverse_patch(fpath, diff)
                elif ct == "create":
                    # For create, verify file content still matches.
                    expected_new = self._reconstruct_expected_from_diffrows(diff or [])
                    try:
                        actual, _, _, _ = self._read_file_for_patch(fpath)
                    except FileNotFoundError:
                        outcome[fpath] = {"success": True}
                        continue
                    except Exception:
                        outcome[fpath] = {"success": False, "error": "cannot read file"}
                        continue
                    if self._normalized_undo_lines(actual) != self._normalized_undo_lines(expected_new):
                        outcome[fpath] = {"success": False, "error": "file modified since creation"}
                        continue
                    try:
                        Path(fpath).unlink()
                        outcome[fpath] = {"success": True}
                    except Exception as exc:
                        outcome[fpath] = {"success": False, "error": f"delete failed: {exc}"}
                elif ct == "rename":
                    old_path = str(fc.get("oldPath", "") or "")
                    if not old_path:
                        outcome[fpath] = {"success": False, "error": "no original path recorded"}
                        continue
                    old_target = Path(old_path)
                    if old_target.exists():
                        outcome[fpath] = {"success": False, "error": "target file already exists"}
                        continue
                    # Verify the renamed file still matches what we recorded.
                    expected_new = self._reconstruct_expected_from_diffrows(diff or [])
                    try:
                        actual, _, _, _ = self._read_file_for_patch(fpath)
                    except FileNotFoundError:
                        outcome[fpath] = {"success": False, "error": "renamed file missing"}
                        continue
                    except Exception:
                        outcome[fpath] = {"success": False, "error": "cannot read file"}
                        continue
                    if self._normalized_undo_lines(actual) != self._normalized_undo_lines(expected_new):
                        outcome[fpath] = {"success": False, "error": "file modified since creation"}
                        continue
                    try:
                        old_target.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(fpath, old_target)
                        outcome[fpath] = {"success": True}
                    except Exception as exc:
                        outcome[fpath] = {"success": False, "error": f"rename failed: {exc}"}
                elif ct == "delete":
                    bp = str(fc.get("backupPath", ""))
                    if not bp:
                        outcome[fpath] = {"success": False, "error": "no backup available"}
                        continue
                    backup_full = self._resolve_backup_full_path(chat_id, bp, workspace_id)
                    if backup_full is None or not backup_full.exists():
                        outcome[fpath] = {"success": False, "error": "backup file missing"}
                        continue
                    target = Path(fpath)
                    if target.exists():
                        outcome[fpath] = {"success": False, "error": "target file already exists"}
                        continue
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(backup_full.read_bytes())
                        outcome[fpath] = {"success": True}
                    except Exception as exc:
                        outcome[fpath] = {"success": False, "error": f"restore failed: {exc}"}
                elif ct == "modify" and diff is None:
                    bp = str(fc.get("backupPath", ""))
                    if not bp:
                        outcome[fpath] = {"success": False, "error": "no backup available"}
                        continue
                    backup_full = self._resolve_backup_full_path(chat_id, bp, workspace_id)
                    if backup_full is None or not backup_full.exists():
                        outcome[fpath] = {"success": False, "error": "backup file missing"}
                        continue
                    try:
                        Path(fpath).parent.mkdir(parents=True, exist_ok=True)
                        Path(fpath).write_bytes(backup_full.read_bytes())
                        outcome[fpath] = {"success": True}
                    except Exception as exc:
                        outcome[fpath] = {"success": False, "error": f"restore failed: {exc}"}
                else:
                    outcome[fpath] = {"success": False, "error": f"unsupported change type: {ct}"}
            except Exception as exc:
                outcome[fpath] = {"success": False, "error": f"unexpected error: {exc}"}
        if outcome:
            undone_success = [
                f for f, r in outcome.items()
                if r.get("success")
            ]
            if undone_success:
                self._update_undone_files_state(
                    chat_id, ref, undone_success, undone=True, workspace_id=workspace_id
                )
        return {"results": outcome}

    def reapply_file_changes(
        self, chat_id: str, ref: str, files: List[str], workspace_id: str = "",
    ) -> Dict[str, Any]:
        """Reapply a list of files.  Returns {results: {filePath: {success, error?}}}.

        ``workspace_id`` scopes the chat (chat ids repeat across workspaces).
        """
        outcome: Dict[str, Dict[str, Any]] = {}
        for fpath in files:
            try:
                fc = self._lookup_file_change(chat_id, ref, fpath, workspace_id)
                if fc is None:
                    outcome[fpath] = {"success": False, "error": "change record not found"}
                    continue
                ct = str(fc.get("changeType", ""))
                diff = fc.get("patch")
                if isinstance(diff, list) and len(diff) > 0 and all(
                    isinstance(r, dict) for r in diff
                ):
                    pass
                else:
                    diff = None

                if ct == "modify" and diff is not None:
                    outcome[fpath] = self._apply_forward_patch(fpath, diff)
                elif ct == "create":
                    target = Path(fpath)
                    if target.exists():
                        outcome[fpath] = {"success": False, "error": "file already exists"}
                        continue
                    reconstructed = self._reconstruct_expected_from_diffrows(diff or [])
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(
                            "\n".join(reconstructed) + "\n", encoding="utf-8",
                        )
                        outcome[fpath] = {"success": True}
                    except Exception as exc:
                        outcome[fpath] = {"success": False, "error": f"create failed: {exc}"}
                elif ct == "rename":
                    old_path = str(fc.get("oldPath", "") or "")
                    if not old_path:
                        outcome[fpath] = {"success": False, "error": "no original path recorded"}
                        continue
                    old_target = Path(old_path)
                    new_target = Path(fpath)
                    if new_target.exists():
                        outcome[fpath] = {"success": False, "error": "file already exists"}
                        continue
                    if not old_target.exists():
                        outcome[fpath] = {"success": False, "error": "original file missing"}
                        continue
                    try:
                        new_target.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(old_target, new_target)
                        outcome[fpath] = {"success": True}
                    except Exception as exc:
                        outcome[fpath] = {"success": False, "error": f"rename failed: {exc}"}
                elif ct == "delete":
                    bp = str(fc.get("backupPath", ""))
                    if not bp:
                        outcome[fpath] = {"success": False, "error": "no backup available"}
                        continue
                    backup_full = self._resolve_backup_full_path(chat_id, bp, workspace_id)
                    if backup_full is None or not backup_full.exists():
                        outcome[fpath] = {"success": False, "error": "backup file missing"}
                        continue
                    target = Path(fpath)
                    if not target.exists():
                        outcome[fpath] = {"success": False, "error": "file does not exist"}
                        continue
                    try:
                        expected = backup_full.read_bytes()
                        actual = target.read_bytes()
                        if expected != actual:
                            outcome[fpath] = {"success": False, "error": "file content modified"}
                            continue
                        target.unlink()
                        outcome[fpath] = {"success": True}
                    except Exception as exc:
                        outcome[fpath] = {"success": False, "error": f"reapply delete failed: {exc}"}
                elif ct == "modify" and diff is None:
                    outcome[fpath] = {"success": False, "error": "binary reapply not supported"}
                else:
                    outcome[fpath] = {"success": False, "error": f"unsupported change type: {ct}"}
            except Exception as exc:
                outcome[fpath] = {"success": False, "error": f"unexpected error: {exc}"}
        if outcome:
            reapplied_success = [
                f for f, r in outcome.items()
                if r.get("success")
            ]
            if reapplied_success:
                self._update_undone_files_state(
                    chat_id, ref, reapplied_success, undone=False, workspace_id=workspace_id
                )
        return {"results": outcome}

    def request_shutdown(self) -> None:
        self._shutdown_event.set()
        # Tear down all PTY sessions so no orphan shells survive the server.
        try:
            self._console.close_all()
        except Exception:
            pass
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
        # Same for any unresolved ``request_user_input`` prompts; pushing an
        # empty answer makes the runtime treat it as "no selection" and
        # pause the task cleanly instead of hanging the loop thread.
        with self._request_user_input_lock:
            pending_ami = list(self._request_user_input.values())
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
        # The execution-policy confirmation prompt calls this so the GUI can
        # render a fixed-option single-choice panel (instead of a modal y/n/a
        # dialog). The picked option is mapped to y/n/a locally and never sent
        # to the model.
        self.agent._confirm_choice_provider = self._confirm_choice_provider  # type: ignore[assignment]
        # The runtime loop calls this for ``request_user_input`` so the GUI can
        # render clickable option chips instead of a raw text prompt.
        self.agent._request_user_input_provider = self._request_user_input_provider  # type: ignore[assignment]

        bridge = _OutputBridge(
            self.broadcaster,
            chat_id_getter=self._active_chat_id,
            workspace_id_getter=self._active_chat_workspace_id,
        )
        self._bridge = bridge
        # GUI streaming mode: the runtime emits clean append-only deltas and
        # brackets the assistant reply with these hooks so the bridge can tag
        # those writes as "assistant" (vs "output" steps).
        self.agent._gui_plain_stream = True  # type: ignore[attr-defined]
        self.agent._gui_assistant_begin = lambda: bridge.set_tag("assistant")  # type: ignore[attr-defined]
        self.agent._gui_assistant_end = lambda: bridge.set_tag("output")  # type: ignore[attr-defined]
        # Forward thinking/reasoning content deltas to the GUI as "thinking" SSE events.
        self.agent._gui_thinking_chunk = lambda delta: (  # type: ignore[attr-defined]
            bridge.write_tagged("thinking", str(delta or "")),
        )
        # Forward context-compaction status banners to the GUI so it can mirror
        # the TUI's "Compacting context" / "Context compacted" feedback.
        self.agent._gui_compaction_notice = lambda phase, mode, title, body, text: self.broadcaster.publish(  # type: ignore[attr-defined]
            "compact_notice",
            self._route(
                stage=str(phase or ""),
                mode=str(mode or ""),
                title=str(title or ""),
                body=str(body or ""),
                text=str(text or ""),
            ),
        )
        # Each model round (one request->response within a turn) is bracketed so
        # the GUI can show a per-round "Working/Worked" wait timer and lay out
        # model text + tool output for that round in natural order. Scoped to the
        # chat bound to the calling loop thread so parallel chats stay separate.
        self.agent._gui_round_begin = lambda: setattr(  # type: ignore[attr-defined]
            self.agent, "_gui_round_start_mono", time.monotonic()
        ) or self.broadcaster.publish("round_start", self._route())
        self.agent._gui_round_end = lambda: self.broadcaster.publish(  # type: ignore[attr-defined]
            "round_end",
            self._route(
                contextUsage={
                    "percent": int(getattr(self.agent, "_last_context_usage_percent", 0) or 0),
                    "tokens": int(getattr(self.agent, "_last_context_input_tokens", 0) or 0),
                    "window": int(getattr(self.agent, "_last_context_window", 0)
                              or getattr(self.agent, "context_window", 0) or 0),
                    "parts": _safe_context_usage_parts(self.agent),
                },
                cacheStats=_compute_chat_cache_stats(self.agent),
                tokenStats=_compute_chat_token_stats(self.agent),
                thinkingElapsedSeconds=round(
                    time.monotonic() - getattr(self.agent, "_gui_round_start_mono", time.monotonic()), 1
                ),
            ),
        )
        # Forward 429/503 retry countdown ticks to the GUI so it can render a
        # live countdown line under the last message while the backend backs
        # off (3s first, then 2^n seconds capped at 60s, retrying forever).
        # ``_route`` tags each event with the calling loop thread's chat +
        # workspace id so parallel chats stay separate.
        from ..ai.ai_provider_clients import set_retry_countdown_callback

        def _publish_retry_countdown(**kw: Any) -> None:
            self.broadcaster.publish(
                "retry_countdown",
                self._route(
                    code=int(kw.get("code") or 0),
                    retryNumber=int(kw.get("retry_number") or 0),
                    waitSeconds=float(kw.get("wait_seconds") or 0),
                    remainingSeconds=float(kw.get("remaining_seconds") or 0),
                    modelName=str(kw.get("model_name") or ""),
                    message=str(kw.get("message") or ""),
                    done=bool(kw.get("done")),
                ),
            )

        set_retry_countdown_callback(_publish_retry_countdown)
        # Bridge for the GUI-only browser tools: lets a tool send a command to
        # the embedded browser and block for its result. Its presence also gates
        # the browser_* tools into the model-visible spec (registry: gui_enabled).
        self.agent._browser_dispatch = self.dispatch_browser_command  # type: ignore[attr-defined]
        # Hook for the browser_preview_file tool: read a local HTML file the
        # model just wrote, persist a bridged copy, and open it in the browser.
        self.agent._browser_preview_file = self.preview_local_html_file  # type: ignore[attr-defined]
        # Bridge for the GUI-only console tools: lets a tool drive the active
        # PTY session (exec/read/info). Its presence gates the console_* tools
        # into the spec (registry: gui_enabled).
        self.agent._console_dispatch = self.dispatch_console_command  # type: ignore[attr-defined]
        # When the model updates its plan mid-turn, push a fresh state snapshot
        # so the GUI's plan panel reflects it immediately. We use the dedicated
        # ``state`` event (not ``idle``) because the loop is still actively
        # running — emitting ``idle`` here would falsely flip the GUI's busy
        # flag and freeze the current turn's "Working…" timer, which made the
        # Plan-mode "Execute now" button appear before the model had finished
        # streaming its plan reply.
        self.agent._gui_plan_changed = lambda: self.broadcaster.publish(  # type: ignore[attr-defined]
            "state", self._route(state=self._state_for_loop_thread())
        )
        self.agent._gui_context_usage_changed = lambda: self.broadcaster.publish(  # type: ignore[attr-defined]
            "state", self._route(state=self._state_for_loop_thread())
        )
        # Hook for sub-agent session events: lets the sub-agent executor emit
        # SSE events for real-time viewing in the GUI.
        self.agent._gui_subagent_event = lambda event_name, data: self.broadcaster.publish(  # type: ignore[attr-defined]
            event_name, self._route(**data)
        )
        self.agent._gui_tool_feedback_repaint = lambda text: self.broadcaster.publish(  # type: ignore[attr-defined]
            "tool_feedback_repaint",
            self._route(text=str(text or "")),
        )
        def _emit_tool_output(text: str, **kw: Any) -> None:
            payload: Dict[str, Any] = {"text": str(text or "")}
            bg_task_id = str(kw.get("bg_task_id") or "")
            if bg_task_id:
                payload["bgTaskId"] = bg_task_id
            self.broadcaster.publish("output", self._route(**payload))

        self.agent._gui_tool_output_emit = _emit_tool_output  # type: ignore[attr-defined]
        self.agent._gui_bg_task_output_emit = lambda task_id, text, end=False, status="", return_code=None: self.broadcaster.publish(  # type: ignore[attr-defined]
            "background_task_output",
            self._route(
                taskId=str(task_id or ""),
                text=str(text or ""),
                end=bool(end),
                status=str(status or ""),
                returnCode=return_code,
            ),
        )
        self.agent._gui_request_user_input_answer_emit = lambda answer: self.broadcaster.publish(  # type: ignore[attr-defined]
            "request_user_input_answer",
            self._route(answer=str(answer or "")),
        )
        # Hook for file change events: emits a summary of all file changes
        # at the end of a task.  Each summary is stored in file_changes.json
        # keyed by a random hashcode, and a [FILE_CHANGE_REF:<hashcode>]
        # internal message is inserted into the conversation history so the
        # structured-turn builder can attach the right summary to each turn.
        FILE_CHANGE_REF_PREFIX = "[FILE_CHANGE_REF:"
        from ..core.logging.app_logging import get_logger
        _fc_logger = get_logger("codewood.file_change")
        def _on_file_changes(summary: dict) -> None:
            cid = str(self._active_chat_id())
            wsid = str(self._active_chat_workspace_id())
            # Reuse hashcodes from _pending_preview_refs (generated by
            # _record_model_tool_execution_history for each apply_patch)
            # so previews.json and file_changes.json share the same key.
            # Pop only the first ref; don't clear the list — leftover
            # refs are harmless (they'll be overwritten on next use).
            _pending_refs = getattr(self.agent, "_pending_preview_refs", None)
            if isinstance(_pending_refs, list) and _pending_refs:
                _hash = _pending_refs.pop(0)
            else:
                _hash = secrets.token_hex(8)
            summary["ref"] = _hash
            # Store as a dict keyed by hashcode under the workspace-qualified
            # composite key so a same-id chat in another workspace can't collide.
            _scope_key = ServeApp._file_changes_scope_key(cid, wsid)
            _fc_by_chat = dict(getattr(self.agent, "_file_changes_by_chat", {}) or {})
            _fc_store: dict = dict(_fc_by_chat.get(_scope_key, {}))
            _fc_store[_hash] = summary
            _fc_by_chat[_scope_key] = _fc_store
            setattr(self.agent, "_file_changes_by_chat", _fc_by_chat)
            # Persist to disk so it survives restarts (workspace-aware dir).
            try:
                data_dir = self._chat_data_dir_for(cid, wsid)
                if data_dir is not None:
                    data_dir.mkdir(parents=True, exist_ok=True)
                    from ..managers.chat_state_manager import _safe_replace
                    import json as _json
                    target = data_dir / "file_changes.json"
                    tmp = target.with_suffix(target.suffix + ".tmp")
                    with open(tmp, "w", encoding="utf-8") as _fh:
                        _json.dump(_fc_store, _fh, ensure_ascii=False, indent=2)
                        _fh.write("\n")
                    _safe_replace(tmp, target)
            except Exception:
                pass
            # Insert a [FILE_CHANGE_REF:<hashcode>] internal message into the
            # conversation history so the structured-turn builder can attach
            # the file change to the correct turn.  This message is filtered
            # from the model context (exclude_from_model_context) and the UI
            # (_internal), and is automatically removed on conversation edit.
            try:
                _hist = getattr(self.agent, "conversation_history", None)
                if isinstance(_hist, list):
                    _hist.append({
                        "role": "user",
                        "content": f"{FILE_CHANGE_REF_PREFIX}{_hash}]",
                        "_internal": True,
                        "exclude_from_model_context": True,
                        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })
            except Exception:
                pass
            _fc_logger.debug(f"[file_changes] broadcasting: ref={_hash} files={summary.get('totalFiles')}")
            self.broadcaster.publish(
                "file_changes",
                self._route(**_truncate_file_changes(dict(summary))),
            )
        self.agent._gui_file_changes = _on_file_changes  # type: ignore[attr-defined]
        # The GUI renders its own layout, so disable terminal hard-wrapping and
        # force SGR color emission (stdout is not a TTY here). The bridge keeps
        # the SGR runs so the GUI can color step output like the terminal.
        self.agent._gui_no_wrap = True  # type: ignore[attr-defined]
        os.environ["FORCE_COLOR"] = "1"
        os.environ.pop("NO_COLOR", None)
        prev_stdout, prev_stderr = sys.stdout, sys.stderr
        sys.stdout = bridge
        sys.stderr = bridge
        # The bridge has replaced sys.stderr, and logging's ``lastResort``
        # handler writes to sys.stderr. Any log record that falls through
        # with no real handler (e.g. the ripgrep downloader's error logs)
        # would otherwise be forwarded to the GUI as an ``output`` event and
        # rendered inside the chat transcript. Disable ``lastResort`` while
        # the bridge is installed so internal log lines never reach the
        # frontend (they still reach the application log file when logging
        # is configured).
        prev_last_resort = logging.lastResort
        logging.lastResort = None

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
            logging.lastResort = prev_last_resort
            try:
                self._httpd.server_close()
            except Exception:
                pass
            try:
                self.agent.shutdown(wait=False)
            except Exception:
                pass
        return 0

    def _workspace_config_dir_for(self, workspace_id: str) -> Optional[Path]:
        """Resolve a workspace's config dir (``…/.codewood``) by id, without
        switching the agent's globals. Returns ``None`` if it can't be resolved.
        """
        wsid = str(workspace_id or "").strip()
        if not wsid:
            return None
        agent = self.agent
        # The focused workspace's dir is already on the agent.
        try:
            if wsid == str(getattr(agent, "workspace_id", "") or "").strip():
                return Path(agent.workspace_config_dir)
        except Exception:
            pass
        try:
            wsmgr = getattr(agent, "_workspace_state_manager", None)
            state = getattr(agent, "_workspaces_state", None) or {}
            workspaces = state.get("workspaces") if isinstance(state, dict) else None
            entry = workspaces.get(wsid) if isinstance(workspaces, dict) else None
            if entry and wsmgr is not None:
                return Path(wsmgr.workspace_storage_path(entry))
        except Exception:
            pass
        return None

    def _persist_ctx_for_workspace(
        self, workspace_id: str, config_dir_hint: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Lazy provider for a background workspace's persistence context.

        Called by ``ChatStateManager`` only when a loop's workspace is NOT the
        focused one. Builds (and caches per-workspace) ``{config_dir,
        chat_state, lock}`` by reading that workspace's chat index from disk —
        which is current because the loop persisted through the agent globals
        while it was still focused. The cache is invalidated on every focus
        switch so a later activation re-reads fresh disk state. Returns
        ``None`` if the workspace's config dir can't be resolved.
        """
        wsid = str(workspace_id or "").strip()
        if not wsid:
            return None
        with self._ws_persist_lock:
            ctx = self._ws_persist_ctx.get(wsid)
            if ctx is not None:
                return ctx
            # Prefer the dir captured at runtime spawn (always correct); fall
            # back to deriving from the workspace registry.
            cfg: Optional[Path] = None
            hint = str(config_dir_hint or "").strip()
            if hint:
                cfg = Path(hint)
            if cfg is None:
                cfg = self._workspace_config_dir_for(wsid)
            if cfg is None:
                return None
            try:
                snapshot = self.agent._chat_state_manager.load_chat_state_snapshot(
                    cfg,
                    expected_workspace_id=wsid,
                    # Index-only placeholders: this context exists to let a
                    # background loop persist ITS OWN workspace without touching
                    # the focused globals. Reading every record (dozens, some
                    # multi-MB) under _ws_persist_lock stalled select_chat for
                    # ~0.3-0.8s; save_chat_state hydrates placeholders from
                    # disk on demand before writing.
                    lazy_records=True,
                )
            except Exception:
                return None
            ctx = {
                "workspace_id": wsid,
                "config_dir": cfg,
                "chats_root": get_app_global_config_dir() / "chats",
                "chat_state": snapshot,
                # RLock: sync_active_chat_messages holds it then re-enters via
                # save_chat_state (mirrors the agent's reentrant chat lock).
                "lock": threading.RLock(),
            }
            self._ws_persist_ctx[wsid] = ctx
            return ctx

    def _run_chat_loop(self, rt: "_ChatRuntime") -> None:
        """Run one chat's agent loop on its own thread, bound to its session.

        The chat's :class:`SessionState` is expected to already hold its
        conversation (loaded by the startup activation, ``select_chat``, or
        ``new_chat`` before input is routed here); this thread only binds to it
        so every per-session attribute the loop touches resolves correctly.

        A thread-local workspace override is installed for the whole loop so
        the chat keeps resolving relative tool paths, cache dirs and
        system-prompt roots against ITS OWN workspace even after the user
        focuses another workspace (which swaps the agent's workspace globals).
        """
        try:
            try:
                self.agent._bind_session(rt.chat_id, rt.workspace_id)
            except Exception:
                pass

            ws_ctx = self._runtime_workspace_ctx(rt)
            if ws_ctx:
                try:
                    self.agent._set_workspace_ctx(ws_ctx)
                except Exception:
                    pass

            from ..runtime.runtime_loop import run_agent_loop

            run_agent_loop(self.agent)
        except Exception:
            import traceback

            tb_lines = traceback.format_exc()
            try:
                self.broadcaster.publish(
                    "output",
                    {
                        "text": "\n[agent loop terminated]\n",
                        "chatId": rt.chat_id,
                        "workspaceId": rt.workspace_id,
                    },
                )
            except Exception:
                pass
            try:
                sys.stderr.write(
                    f"[_run_chat_loop] chat={rt.chat_id} exception:\n{tb_lines}\n"
                )
                sys.stderr.flush()
            except Exception:
                pass
        finally:
            try:
                self.agent._set_workspace_ctx(None)
            except Exception:
                pass
            key = self._runtime_key(rt.chat_id, rt.workspace_id)
            with self._runtimes_lock:
                if self._runtimes.get(key) is rt:
                    self._runtimes.pop(key, None)

    def _runtime_workspace_ctx(self, rt: "_ChatRuntime") -> Optional[Dict[str, Any]]:
        """Resolve the thread-local workspace override for a chat loop thread.

        The agent's workspace globals track the FOCUSED workspace; a chat whose
        loop keeps running after the user focuses another workspace must keep
        resolving paths/prompts against ITS OWN workspace. Mirrors
        ``apply_workspace_entry`` so the loop sees the same identity the chat's
        workspace was activated with. Returns ``None`` when the workspace
        cannot be resolved (the loop then falls back to the globals).
        """
        agent = self.agent
        wsid = str(getattr(rt, "workspace_id", "") or "").strip()
        if not wsid:
            return None
        try:
            entry = agent._workspace_entry_by_selector(wsid)
        except Exception:
            return None
        if not isinstance(entry, dict):
            return None
        try:
            root = agent._workspace_root_path(entry)
            storage = agent._workspace_storage_path(entry)
            name = str(entry.get("name") or (root.name if root else wsid) or wsid)
            kind = str(entry.get("kind") or "custom").lower()
            if kind != "default" and root.exists() and root.is_dir():
                work_dir = root
            else:
                work_dir = getattr(agent, "work_directory", root)
            return {
                "workspace_id": wsid,
                "workspace_name": name,
                "workspace_root": str(root),
                "workspace_config_dir": str(storage),
                "work_directory": str(work_dir),
            }
        except Exception:
            return None


def _make_handler(app: ServeApp):
    class _Handler(BaseHTTPRequestHandler):
        server_version = "CodeWoodServe/1.0"
        protocol_version = "HTTP/1.1"

        # Silence default stderr request logging (would hit the SSE bridge).
        def log_message(self, *_args: Any) -> None:  # noqa: N802
            return None

        # A client that aborts mid-request (frontend AbortController on a
        # debounced search, page unload tearing down an EventSource/SSE, or a
        # fetch cancelled by navigation) makes the socket raise
        # ConnectionAbortedError/ConnectionResetError while reading the
        # request line or writing the response. socketserver would otherwise
        # print a scary-but-harmless "Exception occurred during processing of
        # request" traceback; the request is abandoned anyway, so swallow the
        # connection errors and close the socket cleanly.
        def handle_one_request(self) -> None:  # noqa: N802
            try:
                super().handle_one_request()
            except (
                ConnectionAbortedError,
                ConnectionResetError,
                BrokenPipeError,
                TimeoutError,
            ):
                self.close_connection = True

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

        def _read_json_body(
            self, max_bytes: int = _MAX_BODY_BYTES
        ) -> Optional[Dict[str, Any]]:
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                return None
            if length <= 0:
                return {}
            if length > max_bytes:
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

        def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # The cache-busting ``v`` query param lets the webview safely cache.
            self.send_header("Cache-Control", "private, max-age=31536000")
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
                chat_id = str((query.get("chatId") or [""])[0] or "")[:256]
                ws_id = str((query.get("workspaceId") or [""])[0] or "")[:256]
                self._send_json(
                    200, app.chat_history(before, limit, chat_id, ws_id)
                )
                return
            if path == "/chat-search":
                q = str((query.get("q") or [""])[0] or "")[:512]
                lim = 20
                try:
                    lim_vals = query.get("limit") or []
                    if lim_vals and str(lim_vals[0]).strip():
                        lim = max(1, min(50, int(str(lim_vals[0])[:4])))
                except (ValueError, TypeError):
                    lim = 20
                self._send_json(200, app.search_chats(q, lim))
                return
            if path == "/background-image":
                result = app.read_background_image()
                if result is None:
                    self._send_json(404, {"error": "not found"})
                else:
                    data, content_type = result
                    self._send_bytes(200, data, content_type)
                return
            if path == "/chat-image":
                vals = query.get("path") or []
                img_path = str(vals[0]) if vals else ""
                ws_vals = query.get("workspaceId") or []
                result = app.read_chat_image(img_path, str(ws_vals[0]) if ws_vals else "")
                if result is None:
                    self._send_json(404, {"error": "not found"})
                else:
                    data, content_type = result
                    self._send_bytes(200, data, content_type)
                return
            if path == "/chat-file":
                vals = query.get("path") or []
                file_path = str(vals[0]) if vals else ""
                result = app.read_chat_file(file_path)
                if result is None:
                    self._send_json(404, {"error": "not found"})
                else:
                    data, content_type = result
                    self._send_bytes(200, data, content_type)
                return
            if path == "/mcp-icon":
                server_vals = query.get("server") or []
                server = str(server_vals[0]) if server_vals else ""
                vals = query.get("src") or []
                src = str(vals[0]) if vals else ""
                result = app.read_mcp_icon(server, src)
                if result is None:
                    self._send_json(404, {"error": "not found"})
                else:
                    data, content_type = result
                    self._send_bytes(200, data, content_type)
                return
            if path == "/consoles":
                self._send_json(200, {"ok": True, "consoles": app.list_consoles()})
                return
            if path == "/events":
                self._stream_events()
                return
            if path == "/index-status":
                self._send_json(200, app.index_status())
                return
            if path == "/confirm-allowlist":
                self._send_json(200, {"ok": True, "allowlist": app.get_confirm_allowlist()})
                return
            if path == "/sandbox-config":
                self._send_json(200, {"ok": True, "sandbox": app.get_sandbox_config()})
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if not self._guard(query):
                return
            # Pasted bitmaps arrive as base64 data URLs and can exceed the
            # default 1 MiB JSON cap; allow a larger body only for that route.
            if path == "/paste-image":
                max_body = ServeApp._PASTE_IMAGE_MAX_BYTES * 2 + 65536
            elif path == "/save-draft-attachment":
                max_body = 50 * 1024 * 1024 * 2 + 65536
            elif path == "/save-dropped-file":
                max_body = 50 * 1024 * 1024 * 2 + 65536
            elif path == "/browser-preview-html":
                max_body = ServeApp._PREVIEW_HTML_MAX_BYTES * 2 + 65536
            else:
                max_body = _MAX_BODY_BYTES
            body = self._read_json_body(max_body)
            if body is None:
                self._send_json(400, {"error": "invalid body"})
                return
            if path == "/frontend-trace":
                # Renderer-side workspace routing diagnostics.  Keep this
                # endpoint authenticated and deliberately metadata-only so a
                # reproduction can be understood from codewood.log without
                # recording user prompts or assistant/tool output.
                phase = str(body.get("phase") or "")[:80]
                raw_data = body.get("data")
                data = raw_data if isinstance(raw_data, dict) else {}
                try:
                    from ..config.app_info import get_app_logger_root
                    from ..core.logging.app_logging import get_logger

                    get_logger(f"{get_app_logger_root()}.workspace_routing").debug(
                        "frontend-trace phase=%s data=%s",
                        phase,
                        json.dumps(data, ensure_ascii=False, default=str)[:4000],
                    )
                except Exception:
                    pass
                self._send_json(200, {"ok": True})
                return
            if path == "/input":
                text = str(body.get("text") or "")
                if len(text) > _MAX_INPUT_CHARS:
                    self._send_json(413, {"error": "input too large"})
                    return
                chat_id = str(body.get("chatId") or "")[:256]
                ws_id = str(body.get("workspaceId") or "")[:256]
                app.submit_input(
                    text,
                    chat_id=chat_id,
                    as_prompt=bool(body.get("asPrompt")),
                    workspace_id=ws_id,
                )
                self._send_json(200, {"ok": True})
                return
            if path == "/set-chat-model":
                chat_id = str(body.get("chatId") or "")[:256]
                ws_id = str(body.get("workspaceId") or "")[:256]
                model = str(body.get("model") or "")[:256]
                ok = app.set_chat_model(chat_id, model, ws_id)
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/set-chat-reasoning":
                chat_id = str(body.get("chatId") or "")[:256]
                ws_id = str(body.get("workspaceId") or "")[:256]
                reasoning = str(body.get("reasoning") or "")[:256]
                ok = app.set_chat_reasoning(chat_id, reasoning, ws_id)
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/rename-chat":
                chat_id = str(body.get("chatId") or "")[:256]
                ws_id = str(body.get("workspaceId") or "")[:256]
                name = str(body.get("name") or "")[:512]
                ok = app.rename_chat(chat_id, name, ws_id)
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/chat-fork":
                chat_id = str(body.get("chatId") or "")[:256]
                ws_id = str(body.get("workspaceId") or "")[:256]
                try:
                    index = int(body.get("index") or -1)
                except (TypeError, ValueError):
                    index = -1
                result = app.chat_fork(chat_id, ws_id, index)
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path == "/chat-new-from-compact":
                chat_id = str(body.get("chatId") or "")[:256]
                ws_id = str(body.get("workspaceId") or "")[:256]
                first_message = str(body.get("firstMessage") or "")[:20000]
                result = app.chat_new_from_compact(
                    chat_id, ws_id, first_message
                )
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path == "/chat-edit":
                chat_id = str(body.get("chatId") or "")[:256]
                ws_id = str(body.get("workspaceId") or "")[:256]
                try:
                    index = int(body.get("index") or -1)
                except (TypeError, ValueError):
                    index = -1
                ok = app.chat_edit(chat_id, ws_id, index)
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/set-execution-policy":
                policy = str(body.get("policy") or "")[:64]
                ok = app.set_execution_policy(policy)
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/workspace-create":
                p = str(body.get("path") or "")[:4096]
                result = app.workspace_create(p)
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path == "/workspace-rename":
                wid = str(body.get("id") or "")[:256]
                name = str(body.get("name") or "")[:512]
                result = app.workspace_rename(wid, name)
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path == "/save-pending-inputs":
                chat_id = str(body.get("chatId") or "")[:256]
                ws_id = str(body.get("workspaceId") or "")[:256]
                inputs = body.get("inputs")
                if not isinstance(inputs, list):
                    inputs = []
                inputs = [str(x) for x in inputs if str(x).strip()][:50]
                ok = app.save_pending_inputs(chat_id, ws_id, inputs)
                self._send_json(200 if ok else 404, {"ok": ok})
                return
            if path == "/paste-image":
                chat_id = str(body.get("chatId") or "")[:256]
                workspace_id = str(body.get("workspaceId") or "")[:256]
                data_url = str(body.get("dataUrl") or "")
                # ``data:`` URLs can be large; bound to the decoded cap * ~1.4
                # to account for base64 inflation plus a small header margin.
                if len(data_url) > (ServeApp._PASTE_IMAGE_MAX_BYTES * 2):
                    self._send_json(413, {"error": "image too large"})
                    return
                result = app.save_pasted_image(chat_id, data_url, workspace_id)
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path == "/save-dropped-file":
                chat_id = str(body.get("chatId") or "")[:256]
                workspace_id = str(body.get("workspaceId") or "")[:256]
                data_url = str(body.get("dataUrl") or "")
                file_name = str(body.get("fileName") or "")[:256]
                max_url_len = 50 * 1024 * 1024 * 2
                if len(data_url) > max_url_len:
                    self._send_json(413, {"error": "file too large"})
                    return
                result = app.save_dropped_file(chat_id, data_url, file_name, workspace_id)
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path == "/save-draft-attachment":
                workspace_id = str(body.get("workspaceId") or "")[:256]
                data_url = str(body.get("dataUrl") or "")
                file_name = str(body.get("fileName") or "")[:256]
                max_url_len = 50 * 1024 * 1024 * 2
                if len(data_url) > max_url_len:
                    self._send_json(413, {"error": "file too large"})
                    return
                result = app.save_draft_attachment(workspace_id, data_url, file_name)
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path == "/materialize-draft-attachments":
                chat_id = str(body.get("chatId") or "")[:256]
                workspace_id = str(body.get("workspaceId") or "")[:256]
                paths = body.get("paths")
                result = app.materialize_draft_attachments(chat_id, workspace_id, paths)
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path == "/browser-result":
                request_id = str(body.get("requestId") or "")[:64]
                result = body.get("result")
                ok = app.answer_browser_result(
                    request_id, result if isinstance(result, dict) else {}
                )
                self._send_json(200 if ok else 404, {"ok": ok})
                return
            if path == "/browser-preview-html":
                chat_id = str(body.get("chatId") or "")[:256]
                ws_id = str(body.get("workspaceId") or "")[:256]
                html = str(body.get("html") or "")
                result = app.save_preview_html(chat_id, html, ws_id)
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path == "/preview-local-file":
                file_path = str(body.get("path") or "").strip()
                result = app._save_preview_for_path(file_path)
                if result:
                    self._send_json(200, result)
                else:
                    self._send_json(400, {"error": "preview failed"})
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
                ok = app.answer_request_user_input(pid, answer)
                self._send_json(200 if ok else 404, {"ok": ok})
                return
            if path == "/interrupt":
                cid = str(body.get("chatId") or "")[:256]
                wsid = str(body.get("workspaceId") or "")[:256]
                app.interrupt(chat_id=cid, workspace_id=wsid)
                self._send_json(200, {"ok": True})
                return
            if path == "/pause":
                cid = str(body.get("chatId") or "")[:256]
                wsid = str(body.get("workspaceId") or "")[:256]
                app.pause(chat_id=cid, workspace_id=wsid)
                self._send_json(200, {"ok": True})
                return
            if path == "/compact":
                cid = str(body.get("chatId") or "")[:256]
                wsid = str(body.get("workspaceId") or "")[:256]
                result = app.compact_context(chat_id=cid, workspace_id=wsid)
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path == "/export-chat":
                cid = str(body.get("id") or "")[:256]
                wsid = str(body.get("workspaceId") or "")[:256]
                file_path = str(body.get("filePath") or "")[:1024]
                ok = app.export_chat(cid, file_path, wsid)
                self._send_json(200 if ok else 400, {"ok": ok})
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
                ws_id = str(body.get("workspaceId") or "")[:256]
                model = str(body.get("model") or "")[:256]
                reasoning = str(body.get("reasoning") or "")[:256]
                cid = app.new_chat(ws_id, model=model, reasoning=reasoning)
                self._send_json(
                    200 if cid else 409, {"ok": bool(cid), "id": cid or ""}
                )
                return
            if path == "/open-folder":
                folder = str(body.get("folder") or "")[:4096]
                result = app.open_folder(folder)
                if result is None:
                    self._send_json(400, {"ok": False})
                else:
                    self._send_json(200, {"ok": True, **result})
                return
            if path == "/delete-workspace":
                ws_id = str(body.get("id") or "")[:256]
                result = app.delete_workspace(ws_id)
                if result is None:
                    self._send_json(400, {"ok": False})
                else:
                    self._send_json(200, {"ok": True, **result})
                return
            if path == "/toggle-chat-archive":
                cid = str(body.get("id") or "")[:256]
                ws_id = str(body.get("workspaceId") or "")[:256]
                ok = app.toggle_chat_archive(cid, ws_id)
                self._send_json(200 if ok else 400, {"ok": ok})
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
            if path == "/console-open":
                kind = str(body.get("kind") or "")[:32]
                result = app.open_console(kind)
                self._send_json(200 if result.get("success") else 400, result)
                return
            if path == "/console-close":
                sid = str(body.get("id") or "")[:64]
                ok = app.close_console(sid)
                self._send_json(200 if ok else 404, {"ok": ok})
                return
            if path == "/console-activate":
                sid = str(body.get("id") or "")[:64]
                ok = app.set_active_console(sid)
                self._send_json(200 if ok else 404, {"ok": ok})
                return
            if path == "/console-attach":
                sid = str(body.get("id") or "")[:64]
                result = app.attach_console(sid)
                self._send_json(200 if result.get("ok") else 404, result)
                return
            if path == "/console-input":
                sid = str(body.get("id") or "")[:64]
                data = body.get("data")
                ok = app.console_input(sid, data if isinstance(data, str) else "")
                self._send_json(200 if ok else 404, {"ok": ok})
                return
            if path == "/console-resize":
                sid = str(body.get("id") or "")[:64]
                ok = app.console_resize(sid, body.get("cols"), body.get("rows"))
                self._send_json(200 if ok else 404, {"ok": ok})
                return
            if path == "/set-console-options":
                options = body.get("options")
                ok = app.set_console_options(options if isinstance(options, dict) else {})
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/set-background-image":
                source_path = str(body.get("sourcePath") or "")[:4096]
                ok = app.set_background_image(source_path)
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/clear-background-image":
                ok = app.clear_background_image()
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/set-background-opacity":
                ok = app.set_background_opacity(body.get("opacity"))
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
            if path == "/sync-model-presets":
                app_presets = body.get("presets")
                merged = app.sync_model_presets(app_presets if isinstance(app_presets, list) else [])
                self._send_json(200, {"ok": True, "presets": merged})
                return
            if path == "/model-presets":
                presets = app.get_model_presets()
                self._send_json(200, {"ok": True, "presets": presets})
                return
            if path == "/general-config":
                self._send_json(200, {"ok": True, "general": app.get_general_config()})
                return
            if path == "/save-general-config":
                general = body.get("general")
                ok = app.save_general_config(general if isinstance(general, dict) else {})
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/security-audit-config":
                self._send_json(200, {"ok": True, "audit": app.get_security_audit_config()})
                return
            if path == "/model-selectors":
                self._send_json(200, {"ok": True, "selectors": app.get_model_selectors()})
                return
            if path == "/save-security-audit-config":
                audit = body.get("audit")
                ok = app.save_security_audit_config(
                    audit if isinstance(audit, dict) else {}
                )
                self._send_json(200 if ok else 400, {"ok": ok})
                return
            if path == "/save-confirm-allowlist":
                allowlist = body.get("allowlist")
                ok = app.save_confirm_allowlist(
                    allowlist if isinstance(allowlist, dict) else {}
                )
                result = {"ok": ok}
                if ok:
                    result["allowlist"] = app.get_confirm_allowlist()
                self._send_json(200 if ok else 400, result)
                return
            if path == "/save-sandbox-config":
                sandbox = body.get("sandbox")
                ok = app.save_sandbox_config(
                    sandbox if isinstance(sandbox, dict) else {}
                )
                result = {"ok": ok}
                if ok:
                    result["sandbox"] = app.get_sandbox_config()
                self._send_json(200 if ok else 400, result)
                return
            if path == "/sandbox-setup":
                result = app.setup_sandbox()
                self._send_json(200 if result.get("ok") else 400, result)
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
                cid = str(body.get("chatId") or "")[:256]
                wsid = str(body.get("workspaceId") or "")[:256]
                ok = app.set_plan_mode(
                    bool(body.get("enabled")), chat_id=cid, workspace_id=wsid
                )
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
            if path == "/skills-overview":
                self._send_json(200, app.get_skills_overview())
                return
            if path == "/set-skill-enabled":
                sid = str(body.get("skillId") or "")[:256]
                enabled = bool(body.get("enabled", True))
                ok = app.set_skill_enabled(sid, enabled)
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
            if path == "/subagent-session-history":
                import logging as _sa_logging
                _sa_log = _sa_logging.getLogger("codewood.server")
                session_id = str(body.get("sessionId") or "")[:128]
                chat_id = str(body.get("chatId") or "")[:128]
                _sa_log.info("subagent-session-history: session_id=%s, chat_id=%s", session_id, chat_id)
                if not session_id:
                    self._send_json(400, {"ok": False, "error": "sessionId required"})
                    return
                from ..subagents.executor import get_session_store
                store = get_session_store()
                session = store.get_session(session_id)
                _sa_log.info("subagent-session-history: cache hit=%s", session is not None)
                # Prefer the on-disk file when the cached session is missing the
                # structured ``_tool_rounds_raw`` (e.g. it was generated under an
                # older build and is still held in the in-memory cache). The disk
                # file is always the canonical, up-to-date record, so reloading
                # from it guarantees the GUI sees the current tool-round data.
                # ``load_session_from_disk`` short-circuits on a cache hit, so
                # evict the (stale) cached entry first to force a fresh read.
                _cached_has_raw = bool(session) and any(
                    isinstance(_m, dict) and _m.get("_tool_rounds_raw")
                    for _m in (session.get("messages") or [])
                )
                if (session is None or not _cached_has_raw) and chat_id:
                    try:
                        store._cache.pop(session_id, None)
                    except Exception:
                        pass
                    _disk = store.load_session_from_disk(app.agent, chat_id, session_id)
                    if _disk is not None:
                        _sa_log.info("subagent-session-history: disk load (cache stale/missing)=%s", _cached_has_raw)
                        session = _disk
                if session is None:
                    _sa_log.warning("subagent-session-history: session %s not found (chat_id=%s)", session_id, chat_id)
                    self._send_json(404, {"ok": False, "error": "session not found"})
                    return
                # Render structured ``_tool_rounds_raw`` into the pre-rendered
                # ``tool_rounds`` the GUI session viewer expects, via the SAME
                # pipeline the main chat uses for history reload. This keeps the
                # reloaded view identical to the live one and lets tool outputs
                # expand on demand. Render on a copy so the shared/cached
                # session dict (and the on-disk file) stay structured.
                try:
                    _session_out = json.loads(json.dumps(session))
                    # The persisted ``image`` value is RELATIVE to the session
                    # record file's directory. Resolve it to an absolute on-disk
                    # path so the GUI can serve it via ``/chat-image`` (which
                    # validates the path lives under the chat data dir). Drop
                    # the field when the file is gone or unresolvable.
                    _rel_img = _session_out.get("image")
                    if _rel_img:
                        try:
                            _sdir = store._session_dir(app.agent, chat_id)
                            if _sdir is not None:
                                _abs_img = (_sdir / str(_rel_img)).resolve()
                                if _abs_img.is_file():
                                    _session_out["image"] = str(_abs_img)
                                else:
                                    _session_out.pop("image", None)
                        except Exception:
                            _session_out.pop("image", None)
                    _session_messages_out: List[Dict[str, Any]] = []
                    for _m in _session_out.get("messages", []) or []:
                        if not isinstance(_m, dict):
                            continue
                        if str(_m.get("role") or "").strip().lower() == "assistant":
                            # The sub-agent viewer renders tool steps from
                            # ``tool_rounds``/``tool_calls``; never synthesize the
                            # raw tool_calls JSON into content (would leak).
                            _m = _assistant_display_view(_m, synthesize_plan_payload=False)
                        _session_messages_out.append(_m)
                    _session_out["messages"] = _session_messages_out
                    for _m in _session_out.get("messages", []) or []:
                        _raw = _m.get("_tool_rounds_raw")
                        if isinstance(_raw, list) and _raw and not _m.get("tool_rounds"):
                            try:
                                _m["tool_rounds"] = self.agent._rerender_tool_rounds(_raw)
                            except Exception:
                                # Fallback: render a human-readable description
                                # straight from the raw entry (reusing the agent's
                                # own tool-label logic when available) so a render
                                # failure never produces a raw JSON blob or a blank
                                # "• undefined" row.
                                try:
                                    _fallback_rounds: List[str] = []
                                    _natural = getattr(self.agent, "_natural_tool_action", None)
                                    _humanize = getattr(self.agent, "_humanize_tool_name", None)
                                    for _item in _raw:
                                        _tn = str(_item.get("tool") or "tool")
                                        _args = _item.get("args") if isinstance(_item.get("args"), dict) else {}
                                        _label = _tn
                                        _detail = ""
                                        try:
                                            if callable(_natural):
                                                _label, _detail = _natural(_tn, _args)
                                            elif callable(_humanize):
                                                _label = _humanize(_tn)
                                        except Exception:
                                            pass
                                        _bullet = "\u2022"
                                        _line = f"{_bullet} {_label}" + (f" {_detail}" if _detail else "")
                                        _out = _item.get("output")
                                        if _out:
                                            _line = f"{_line}\n\uE000{_out}\uE001"
                                        _fallback_rounds.append(_line)
                                    _m["tool_rounds"] = _fallback_rounds
                                except Exception:
                                    pass
                except Exception:
                    _session_out = session
                self._send_json(200, {"ok": True, "session": _session_out})
                return
            if path == "/fetch-models":
                result = app.fetch_provider_models(body if isinstance(body, dict) else {})
                self._send_json(
                    200 if result.get("ok") else 400, result
                )
                return
            if path == "/undo-file-changes":
                chat_id = str(body.get("chatId") or "")[:256]
                ws_id = str(body.get("workspaceId") or "")[:256]
                ref = str(body.get("ref") or "")[:256]
                file_list = body.get("files")
                if not isinstance(file_list, list):
                    file_list = []
                file_list = [str(f) for f in file_list if str(f).strip()]
                result = app.undo_file_changes(chat_id, ref, file_list, ws_id)
                self._send_json(200, result)
                return
            if path == "/reapply-file-changes":
                chat_id = str(body.get("chatId") or "")[:256]
                ws_id = str(body.get("workspaceId") or "")[:256]
                ref = str(body.get("ref") or "")[:256]
                file_list = body.get("files")
                if not isinstance(file_list, list):
                    file_list = []
                file_list = [str(f) for f in file_list if str(f).strip()]
                result = app.reapply_file_changes(chat_id, ref, file_list, ws_id)
                self._send_json(200, result)
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
                # Prime the client with the current state immediately. Tag the
                # snapshot with the active chat + workspace so a reconnect
                # (whose earlier closing events may have been lost) can settle
                # a stale live turn via the normal idle path.
                self._write_sse(
                    {"event": "idle", "data": app._route(state=app.state())}
                )
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
                chunk = f"data: {payload}\n\n".encode("utf-8")
            except Exception as exc:
                # A payload that cannot be JSON-serialized (or UTF-8 encoded,
                # e.g. lone surrogates) must never silently disappear: the GUI
                # would wait forever for the very event that closes the current
                # tool round / turn (``round_end`` / ``idle``), leaving the
                # spinner stuck. Log the culprit instead of dropping it unseen.
                try:
                    _SSE_LOGGER.warning(
                        "SSE event dropped: serialization failed (%s) event=%r",
                        exc,
                        message.get("event") if isinstance(message, dict) else None,
                    )
                except Exception:
                    pass
                return True
            return self._write_raw(chunk)

        def _write_raw(self, chunk: bytes) -> bool:
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
                return True
            except Exception:
                return False

    return _Handler
