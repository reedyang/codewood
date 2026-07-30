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

from ..core.console_utils import (
    GUI_FORCE_PROMPT_PREFIX,
    GUI_INTERNAL_COMMAND_PREFIX,
)
from ..config.app_info import get_app_slug_snake

_MCP_LOGGER_NAME = f"{get_app_slug_snake()}.mcp"

from ..core.logging.app_logging import get_logger as _get_logger
_log = _get_logger("codewood.serve.cid")

try:  # diagnostics: workspace-switch persistence routing (temporary)
    from ..config.app_info import get_app_logger_root as _logger_root

    def _wslog(msg: str) -> None:
        try:
            _get_logger(f"{_logger_root()}.serve.wsswitch").info(msg)
        except Exception:
            pass
except Exception:  # pragma: no cover - logging is best-effort
    def _wslog(msg: str) -> None:
        return None

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


def _strip_bom(text: str) -> str:
    """Strip UTF-8 BOM prefix (``\ufeff``) for safe content comparison."""
    if isinstance(text, str) and text.startswith("\ufeff"):
        return text[1:]
    return text


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
                "archived": bool(c.get("archived", False)),
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
        content = str(msg.get("content") or "")
        clean_content = str(msg.get("_clean_content") or "") or content
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
                    _fc_store = getattr(agent, "_file_changes_by_chat", {}) or {}
                    _cid = str(getattr(agent, "active_chat_id", "") or "")
                    _chat_store = _fc_store.get(_cid, {})
                    if isinstance(_chat_store, dict):
                        _fc = _chat_store.get(_ref)
                        if isinstance(_fc, dict) and _fc.get("totalFiles", 0) > 0:
                            if current:
                                current["fileChanges"] = _fc
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
                                        "compactNoticeTitle": str(compact_display.get("title") or ""),
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
            # Conversation-interrupted banners are rendered as a separate
            # field so the frontend can display them outside the collapsible
            # "Worked for" section — they are status messages, not tool steps.
            is_interrupted = agent._parse_conversation_interrupted_history_content(content) is not None
            if is_interrupted:
                if current_round is None or current_round.get("text") or current_round.get("interrupted"):
                    current_round = _new_round(turn, wait)
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
            )
        ]
    # Attach per-turn file-change summaries from the sidecar.
    # New format: hashcode-ref dict loaded via [FILE_CHANGE_REF] messages
    # (already handled above).  Legacy fallback: turnIndex-based list.
    try:
        _cs = getattr(agent, "_chat_state", None)
        _cid = str(_cs.get("active", "")) if isinstance(_cs, dict) else ""
        if _cid:
            _fc_by_chat = getattr(agent, "_file_changes_by_chat", {}) or {}
            _fc_store = _fc_by_chat.get(_cid, {})
            if not _fc_store:
                _mgr = getattr(agent, "_chat_state_manager", None)
                if _mgr is not None:
                    _disk = _mgr.load_file_changes(_cid)
                    if isinstance(_disk, list):
                        # Legacy turnIndex format: attach by position
                        for i, turn in enumerate(turns):
                            _fc = _disk[i] if i < len(_disk) else None
                            if isinstance(_fc, dict) and _fc.get("totalFiles", 0) > 0:
                                turn["fileChanges"] = _fc
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
                            turn["fileChanges"] = _fc
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
        return self.write_tagged(str(getattr(self._tls, "tag", "output") or "output"), s)

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


def _compute_chat_cache_stats(agent: Any) -> Dict[str, Any]:
    """Aggregate cache-hit statistics for the active chat's current model.

    Scans conversation_history for messages sent with the same model name,
    summing recorded prompt_cache_hit_tokens and prompt_cache_miss_tokens.
    When the API only provides root-level input_tokens (no cache breakdown),
    those are accumulated as totalTokens with hasBreakdown=False.
    Falls back to the persisted chat record when conversation_history is empty.
    """
    provider = str(getattr(agent, "provider", "") or "").strip()
    model_name = str(getattr(agent, "model_name", "") or "").strip()
    agent_key = f"{provider}/{model_name}" if provider and model_name else ""
    result: Dict[str, Any] = {
        "totalTokens": 0,
        "hitTokens": 0,
        "missTokens": 0,
        "hitRate": 0.0,
        "model": model_name,
        "supported": False,
        "hasBreakdown": True,
    }
    if not model_name:
        return result
    hist = list(getattr(agent, "conversation_history", None) or [])
    if not hist:
        cid = _primary_active_chat_id(agent)
        chat = agent._find_chat_by_id(cid) if cid else None
        if isinstance(chat, dict):
            hist = list(chat.get("messages") or [])

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

    Scans messages for ``_output_tokens`` and ``_reasoning_tokens`` fields,
    summing them per model. When ``_token_count_includes_reasoning`` is True
    (e.g. DeepSeek), the effective output tokens = ``_output_tokens`` -
    ``_reasoning_tokens``. Falls back to the persisted chat record when
    conversation_history is empty.
    """
    provider = str(getattr(agent, "provider", "") or "").strip()
    model_name = str(getattr(agent, "model_name", "") or "").strip()
    agent_key = f"{provider}/{model_name}" if provider and model_name else ""
    result: Dict[str, Any] = {
        "outputTokens": 0,
        "reasoningTokens": 0,
        "hasOutputTokens": False,
        "hasReasoningTokens": False,
        "includesReasoning": False,
    }
    if not model_name:
        return result
    hist = list(getattr(agent, "conversation_history", None) or [])
    if not hist:
        cid = _primary_active_chat_id(agent)
        try:
            chat = agent._find_chat_by_id(cid) if cid else None
            if isinstance(chat, dict):
                hist = list(chat.get("messages") or [])
        except Exception:
            pass

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


def _build_state(agent: Any) -> Dict[str, Any]:
    """Serialize a read-only snapshot of agent state for the GUI.

    A background chat's loop thread may build this snapshot (e.g. when it emits
    an ``idle``/``state`` event). That thread carries a per-workspace
    PERSISTENCE override pointing at its OWN workspace; the snapshot, however,
    must describe the FOCUSED workspace (its chat list, active chat, etc.).
    Suspend the override for the whole build so chat reads (``_chat_entries``)
    resolve against the focused global index, not the background workspace's.
    """
    suspend = getattr(agent, "_suspend_persist_workspace_ctx", None)
    if callable(suspend):
        with suspend():
            return _build_state_inner(agent)
    return _build_state_inner(agent)


def _build_state_inner(agent: Any) -> Dict[str, Any]:
    from ..config.app_info import get_app_name, get_app_version
    from ..core.localization import get_display_language
    from ..managers.chat_state_manager import _chat_mode_is_plan

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
                    # True while this chat's agent loop is mid-turn, so the
                    # sidebar busy dot survives focus changes and reloads.
                    "running": cid in running_chat_ids,
                    # Sticky Plan-mode flag recorded on the chat record root
                    # (as ``mode: "plan"|"agent"``). Surfaced so the GUI can
                    # restore the per-chat compose mode after a restart instead
                    # of defaulting every chat to Agent.
                    "planMode": _chat_mode_is_plan(c),
                    "archived": bool(c.get("archived", False)),
                    "pendingInputs": [str(x) for x in (c.get("pending_inputs") or []) if str(x).strip()],
                    # Private: keep the on-disk record file stem so side-data
                    # (file_changes.json) can be resolved without find_chat_by_id.
                    "_recordFile": str(c.get("_record_file") or ""),
                }
            )
        # Attach per-chat file-change summaries (now a dict keyed by hashcode
        # ref).  Prefer the in-memory cache (set by _gui_file_changes hook);
        # fall back to disk sidecar so persisted data survives restarts.
        _fc_map: Dict[str, Any] = getattr(agent, "_file_changes_by_chat", {}) or {}
        _fc_mgr = getattr(agent, "_chat_state_manager", None)
        def _merge_by_file(summaries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            """Merge multiple summaries, combining patches per file path."""
            _by_file: Dict[str, Dict[str, Any]] = {}
            _file_order: List[str] = []
            for _s in summaries:
                if not isinstance(_s, dict):
                    continue
                for _f in (_s.get("files") or []):
                    _fp = str(_f.get("filePath") or "")
                    _patch = _f.get("patch")
                    _ct = _f.get("changeType", "modify")
                    if _fp not in _by_file:
                        _by_file[_fp] = {
                            "filePath": _fp,
                            "changeType": _ct,
                            "addedLines": 0,
                            "deletedLines": 0,
                            "patch": [],
                        }
                        _file_order.append(_fp)
                    _m = _by_file[_fp]
                    if _ct == "delete":
                        _m["addedLines"] += _f.get("addedLines", 0)
                        _m["deletedLines"] += _f.get("deletedLines", 0)
                    elif isinstance(_patch, list):
                        _m["patch"].extend(_patch)
                        for _r in _patch:
                            if not isinstance(_r, dict):
                                continue
                            _t = _r.get("type")
                            if _t == "add":
                                _m["addedLines"] += 1
                            elif _t == "del":
                                _m["deletedLines"] += 1
                            elif _t == "change":
                                _m["addedLines"] += 1
                                _m["deletedLines"] += 1
                    _m["changeType"] = _ct if _ct == "delete" else _m["changeType"]
                    _bp = _f.get("backupPath")
                    if _bp:
                        _m["backupPath"] = _bp
            _files = [_by_file[_fp] for _fp in _file_order]
            _undone = set()
            _ref = None
            for _s in summaries:
                if isinstance(_s, dict):
                    _r = _s.get("ref")
                    if _r:
                        _ref = _r
                    for _uf in (_s.get("undoneFiles") or []):
                        _undone.add(str(_uf))
            _merged: Dict[str, Any] = {
                "totalFiles": len(_files),
                "totalAdded": sum(_f["addedLines"] for _f in _files),
                "totalDeleted": sum(_f["deletedLines"] for _f in _files),
                "files": _files,
            }
            if _undone:
                _merged["undoneFiles"] = list(_undone)
            if _ref:
                _merged["ref"] = _ref
            return [_merged]
        for _ch in chats:
            _ch_id = str(_ch.get("id") or "")
            if not _ch_id:
                continue
            if _ch_id in _fc_map:
                # Convert the dict (keyed by hashcode ref) to an array of
                # summaries, then merge by file path so every file shows
                # cumulative changes across all turns.
                _raw_list = list(_fc_map[_ch_id].values())
                _ch["fileChanges"] = _merge_by_file(_raw_list)
            elif _fc_mgr is not None:
                try:
                    _rec = str(_ch.get("_recordFile") or "")
                    _data_dir = _fc_mgr.chat_data_dir(_rec) if _rec else None
                    _disk_path = (_data_dir / "file_changes.json") if _data_dir else None
                    if _disk_path is not None and _disk_path.exists():
                        import json as _json
                        with open(_disk_path, "r", encoding="utf-8") as _fh:
                            _disk_fc = _json.load(_fh)
                        # New format: dict keyed by hashcode ref
                        # Old format: list of summaries or single dict
                        _fc_store: dict = {}
                        if isinstance(_disk_fc, dict):
                            _first_val = next(iter(_disk_fc.values()), None)
                            if isinstance(_first_val, dict) and "ref" in _first_val:
                                _fc_store = _disk_fc
                            elif _disk_fc.get("totalFiles", 0) > 0:
                                _fc_store["_legacy"] = _disk_fc
                        elif isinstance(_disk_fc, list):
                            _fc_store["_legacy"] = _disk_fc
                        if _fc_store:
                            # Convert to array, merge by file path
                            if "_legacy" in _fc_store:
                                _legacy = _fc_store["_legacy"]
                                if isinstance(_legacy, list):
                                    _raw_list = _legacy
                                else:
                                    _raw_list = [_legacy]
                            else:
                                _raw_list = list(_fc_store.values())
                            _ch["fileChanges"] = _merge_by_file(_raw_list)
                            _fc_map[_ch_id] = _fc_store
                except Exception:
                    pass
        if _fc_map:
            setattr(agent, "_file_changes_by_chat", _fc_map)
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
            "ready": model_ready,
            "reasoningEffort": _safe_reasoning_effort(agent),
            "reasoningEfforts": _safe_reasoning_efforts(agent),
            "reasoningEffortsBySelector": model_reasoning_efforts,
        },
        "contextUsage": {
            "percent": active_context_percent,
            "tokens": active_context_tokens,
            "window": active_context_window,
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
        _wslog(f"[CID] FALLBACK rt={'hit' if rt else 'miss'} agentCid={cid} rtCid={rt.chat_id if rt else 'N/A'}")
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
            "idle", self._route(state=_build_state(self.agent))
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
                    _wslog(
                        f"install override turn chat={rt.chat_id} "
                        f"rt_ws={rt.workspace_id} cfg={rt_cfg} "
                        f"focused_ws={getattr(self.agent,'workspace_id','')}"
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
        list of option labels (Yes / No / optionally Always) and blocks until
        the frontend POSTs the chosen answer to ``/confirm``. The frontend
        renders an inline single-choice panel (same style as the
        ``request_user_input`` panel) below the message area; the user's pick is
        posted back as the option **index** and mapped here to ``"y" | "n" |
        "a"`` locally — the choice is never sent to the model.

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
        # The frontend posts either the option index (preferred) or a direct
        # y/n/a token. Map both to the canonical y/n/a the policy gate expects.
        ans = str(answer or "").strip().lower()
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

    def _apply_immediate_chat_config(
        self,
        chat_id: str,
        apply_fn: "callable[[], None]",
    ) -> str:
        """Apply a GUI model/reasoning change against the target chat session.

        HTTP handler threads are not bound to a chat loop session by default, so
        mutating model globals here would otherwise pin the change onto the
        handler thread's anonymous session instead of the target chat's runtime
        session. Bind to ``chat_id`` first, restore that chat's saved model into
        the shared globals, then apply the update so future turns in that chat
        use the new selection.
        """
        agent = self.agent
        cid = str(chat_id or "").strip() or _primary_active_chat_id(agent)
        with agent._session_scope(cid):
            try:
                agent.active_chat_id = cid
            except Exception:
                pass
            try:
                finder = getattr(agent, "_find_chat_by_id", None)
                restore = getattr(agent, "_apply_chat_model_from_entry", None)
                if callable(finder) and callable(restore):
                    chat = finder(cid)
                    if chat:
                        restore(chat, persist_if_missing=True)
            except Exception:
                pass
            apply_fn()
        return cid

    def submit_input(self, text: str, chat_id: str = "", as_prompt: bool = False) -> None:
        line = str(text or "")
        cid = str(chat_id or "").strip() or _primary_active_chat_id(self.agent)
        # Composer input is forced to a model prompt: prefix a sentinel the
        # runtime loop strips so "/foo" / "!bar" never run as command/shell.
        if as_prompt and line:
            line = GUI_FORCE_PROMPT_PREFIX + line
        elif line.lstrip().startswith("/"):
            stripped = line.lstrip()
            # Apply execution-policy changes immediately so they take effect
            # even while a multi-round task is executing (the inner tool loop
            # does not poll the input queue until the current task finishes).
            if stripped.startswith("/execution-policy "):
                policy = stripped[len("/execution-policy "):].strip().lower()
                if policy in ("unlimited", "moderate", "confirmation"):
                    if policy != str(getattr(self.agent, "execution_policy", "")).lower():
                        self.agent.execution_policy = policy
                        try:
                            save = getattr(self.agent, "_save_execution_policy_to_config", None)
                            if callable(save):
                                save()
                        except Exception:
                            pass
                    self.broadcaster.publish(
                        "state", self._route(state=_build_state(self.agent))
                    )
                    return
            # Apply reasoning effort changes immediately so the new level
            # is used on the next model call, even while a task is executing.
            if stripped.startswith("/reasoning ") or stripped == "/reasoning":
                level = stripped[len("/reasoning"):].strip() if stripped.startswith("/reasoning ") else ""
                try:
                    cid = self._apply_immediate_chat_config(
                        cid,
                        lambda: self.agent._set_reasoning_effort(level),
                    )
                except Exception:
                    pass
                self.broadcaster.publish(
                    "state", self._route(chat_id=cid, state=_build_state(self.agent))
                )
                return
            # Apply model switch immediately so the new model is used on the
            # next call, even while a task is executing.
            if stripped.startswith("/model ") or stripped == "/model":
                try:
                    cid = self._apply_immediate_chat_config(
                        cid,
                        lambda: self.agent._handle_model_builtin_command(stripped),
                    )
                except Exception:
                    pass
                self.broadcaster.publish(
                    "state", self._route(chat_id=cid, state=_build_state(self.agent))
                )
                return
            # Apply chat rename immediately so the new name takes effect
            # even while a multi-round task is executing (the inner tool loop
            # does not poll the input queue until the current task finishes).
            if stripped.startswith("/chat rename "):
                parts = stripped.split()
                if len(parts) >= 4:
                    selector = parts[2]
                    new_name = " ".join(parts[3:]).strip()
                    if new_name:
                        try:
                            agent = self.agent
                            with agent._chat_state_lock:
                                target = agent._resolve_chat_selector(selector)
                                if target:
                                    target["name"] = new_name
                                    target["name_source"] = "manual"
                                    target["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                    if str(target.get("id") or "") == agent.active_chat_id:
                                        agent.active_chat_name = new_name
                                    agent._save_chat_state()
                        except Exception:
                            pass
                self.broadcaster.publish(
                    "state", self._route(state=_build_state(self.agent))
                )
                return
            # All other slash commands: mark them so the runtime loop runs it
            # but keeps them out of the user's input history (history.json).
            line = GUI_INTERNAL_COMMAND_PREFIX + line
            # Editing a message while a task is running: interrupt the task first
            # so the edit command is processed immediately after the task unwinds
            # rather than waiting for the entire task to complete.
            if stripped.startswith("/chat edit"):
                # Suppress the "task interrupted" banner: the edit command
                # truncates the conversation history, so the interrupted task's
                # context is already gone and the banner would be misleading.
                try:
                    self.agent._conversation_interrupt_banner_recent = True
                    self.agent._conversation_interrupt_banner_recent_at = 0.0
                except Exception:
                    pass
                self.interrupt()
        rt = self._get_or_spawn_runtime(cid)
        rt.input_queue.put(line)

    def _get_or_spawn_runtime(self, chat_id: str) -> "_ChatRuntime":
        """Return the chat's runtime, starting its loop thread on first use.

        Keyed by the workspace-qualified composite so a chat in a newly-focused
        workspace never reuses a same-id chat's runtime from another workspace.
        """
        cid = str(chat_id or "")
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

    def interrupt(self) -> None:
        """Cancel the in-flight turn for the GUI's "stop" button.

        The agent loops run on per-chat worker threads, so the CLI's
        ``_thread.interrupt_main()`` path is unusable here (it would raise in
        the unrelated HTTP/main thread). Instead we set the cooperative task
        interrupt flag the loop polls on every model-stream chunk, tool-round
        boundary, and tool dispatch (see ``runtime_loop``), and terminate any
        running interruptible subprocess so the loop unwinds promptly.
        """
        agent = self.agent
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

    def compact_context(self) -> Dict[str, Any]:
        """Trigger manual context compaction via the session memory service.

        Bridge output on the HTTP thread is suppressed so the compaction's
        TUI-oriented printed banners do not create spurious SSE ``output`` events.
        The localized ``compaction.no_context`` message is returned in the response
        so the frontend can render it as a sidebar-style notification (no turn
        lifecycle, no history interference).
        """
        from ..core.localization import get_display_language, translate

        agent = self.agent
        svc = getattr(agent, "session_memory_service", None)
        compact_fn = getattr(svc, "compact_context", None) if svc else None
        focus_chat = _primary_active_chat_id(agent)
        if callable(compact_fn):
            bridge = getattr(self, "_bridge", None)
            prev_suppressed = getattr(bridge, "suppressed", False) if bridge is not None else False
            if bridge is not None:
                bridge.suppressed = True
            try:
                if focus_chat:
                    with agent._session_scope(focus_chat):
                        ok = compact_fn(mode="manual")
                else:
                    ok = compact_fn(mode="manual")
            finally:
                if bridge is not None:
                    bridge.suppressed = prev_suppressed
            if not ok:
                if focus_chat:
                    with agent._session_scope(focus_chat):
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
            st = idx.status()
            result = {
                "hidden": False,
                "files_total": int(st.get("files_total", 0)),
                "workspace_name": str(getattr(agent, "workspace_name", "") or ""),
                "is_default_workspace": str(getattr(agent, "workspace_id", "")) == "default",
                "refresh_phase": str(st.get("refresh_phase", "") or ""),
                "refresh_progress_total": int(st.get("refresh_progress_total", 0)),
                "refresh_progress_done": int(st.get("refresh_progress_done", 0)),
                "refresh_progress_percent": int(st.get("refresh_progress_percent", 0)),
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
            try:
                idx = getattr(self.agent, "_project_context_index", None)
                if idx is not None and hasattr(idx, "request_yield"):
                    idx.request_yield()
            except Exception:
                pass
            focus_chat = _primary_active_chat_id(self.agent)
            # When the active chat is not actively streaming a turn here
            # (no busy runtime), pull the latest record from disk before
            # building turns so messages, ``pending_request_user_input`` markers
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
                            persist=False,
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

        cid = str(chat_id or "").strip()
        wsid = str(workspace_id or "").strip()
        if not cid and not wsid:
            return False
        agent = self.agent
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
                    runner = getattr(agent, "_active_runtime_chat_ids", None)
                    running_ids = (
                        [str(x) for x in (runner() or []) if str(x)]
                        if callable(runner)
                        else []
                    )
                    if running_ids:
                        sync = getattr(agent, "_sync_active_chat_messages", None)
                        if callable(sync):
                            sync()
                        save = getattr(agent, "_save_chat_state", None)
                        if callable(save):
                            save()
                except Exception:
                    pass

                from ..controllers.workspace_command_controller import (
                    workspace_switch_command,
                )

                with contextlib.redirect_stdout(io.StringIO()):
                    workspace_switch_command(agent, wsid)

                with self._ws_persist_lock:
                    self._ws_persist_ctx.clear()
            if cid:
                with agent._chat_state_lock:
                    target = agent._resolve_chat_selector(cid)
                    rid = str(target.get("id") or "") if target else ""
                if not rid:
                    return False
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
                    result = agent._activate_chat(
                        rid, announce=False, clear_screen=False, print_history=False, persist=False
                    )
                    if result:
                        return False
                    try:
                        save = getattr(agent, "_save_chat_state", None)
                        if callable(save):
                            save()
                    except Exception:
                        pass
        except Exception:
            return False
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
        cfg = ctx.get("config_dir")
        state = ctx.get("chat_state")
        chats = state.get("chats") if isinstance(state, dict) else None
        if cfg is None or not isinstance(chats, list):
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
        rel = Path(record_file)
        if (
            not record_file
            or rel.is_absolute()
            or rel.name != record_file
            or record_file == "chats.json"
        ):
            return None
        stem = record_file[:-len(".json")] if record_file.endswith(".json") else record_file
        return Path(cfg) / "chats" / "data" / stem

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

    def read_chat_image(self, path: str) -> Optional[tuple]:
        """Return ``(bytes, content_type)`` for a pasted image, but ONLY when
        ``path`` resolves to a file inside the chats/data directory. Returns
        ``None`` otherwise (path traversal / not found)."""
        try:
            mgr = getattr(self.agent, "_chat_state_manager", None)
            if mgr is None:
                return None
            records_dir = mgr.chat_records_dir()
            data_root = (records_dir / "data").resolve()
            target = Path(str(path or "")).resolve()
            # Containment check: target must live under chats/data.
            try:
                target.relative_to(data_root)
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
            out_lines = list(r.get("lines") or [])
            pending = str(r.get("pending") or "")
            total_lines = r.get("totalLines", 0)
            truncated = r.get("truncated", False)
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
                output_text += f"\n(totalLines: 0)"
            # Cap output at 12 000 characters to avoid blowing the context window.
            if len(output_text) > 12000:
                output_text = output_text[:11980] + "\n... (output truncated at 12000 chars) ...\n"
            r["output"] = output_text
            return {"success": True, **r}
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

    def save_preview_html(self, chat_id: str, html: str) -> Dict[str, Any]:
        """Persist an HTML snippet (wrapped with the preview bridge) under the
        chat data dir. Returns ``{ok, path}`` or ``{ok: False, error}``."""
        cid = str(chat_id or "").strip()
        if not cid:
            return {"ok": False, "error": "missing chatId"}
        raw = str(html or "")
        if not raw.strip():
            return {"ok": False, "error": "empty html"}
        if len(raw.encode("utf-8", "ignore")) > self._PREVIEW_HTML_MAX_BYTES:
            return {"ok": False, "error": "html too large"}
        try:
            mgr = getattr(self.agent, "_chat_state_manager", None)
            if mgr is None:
                return {"ok": False, "error": "no chat"}
            data_dir = mgr.chat_data_dir_for_chat(cid)
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
            data_root = (mgr.chat_records_dir() / "data").resolve()
            target = Path(str(path or "")).resolve()
            try:
                target.relative_to(data_root)
            except ValueError:
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

    def set_plan_mode(self, enabled: bool) -> bool:
        """Toggle the agent's sticky plan mode.

        The GUI uses this to keep the user's message bubble free of any
        injected planning instruction: instead of prefixing the outgoing
        message text, the GUI flips this flag and lets ``runtime_loop`` append
        the localized directive (send-time only) inside the agent boundary. The
        flag is shared with the TUI's ``/plan`` command — flipping it from the
        GUI is equivalent to a TUI ``/plan on`` for the same process — and is
        mirrored onto the active chat record root so a chat reload resumes it.
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
                # bug). Bind to the stable cross-thread primary chat first.
                cid = _primary_active_chat_id(self.agent)
                if cid:
                    with self.agent._session_scope(cid):
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
                agent._refresh_workspace_runtime(create_default_chat=False)
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
            # Don't auto-create a default chat — the GUI enters draft mode.
            agent._refresh_workspace_runtime(create_default_chat=False)
            agent._save_current_workspace_position(sync_messages=False)
        except Exception:
            return None

        self.broadcaster.publish(
            "idle", self._route(state=_build_state(agent))
        )
        return {"id": workspace_id, "name": name, "existing": False}

    def delete_workspace(self, workspace_id: str) -> Optional[Dict]:
        """Delete a workspace from the registry, falling back to another
        workspace if the deleted one was active.  Executes on the HTTP
        thread — bypassing the chat runtime — so the SSE broadcast carries
        a clean state with no session-bleed from the old chat.
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

        workspaces = agent._workspaces_state.get("workspaces", {})
        if isinstance(workspaces, dict):
            workspaces.pop(wsid, None)

        if active_deleted:
            default_ws_id = _default_workspace_id()
            default_entry = (
                workspaces.get(default_ws_id)
                if isinstance(workspaces.get(default_ws_id), dict)
                else agent._default_workspace_entry()  # type: ignore[attr-defined]
            )
            if isinstance(workspaces, dict):
                workspaces[default_ws_id] = default_entry
            agent._apply_workspace_entry(default_entry, agent.work_directory)
            # Don't auto-create a default chat; the frontend will enter
            # draft mode when the fallback workspace has no chats.
            agent._save_current_workspace_position(sync_messages=False)
            agent._refresh_workspace_runtime(create_default_chat=False)
        else:
            agent._save_workspace_state()

        agent._refresh_input_handler_skill_completions()  # type: ignore[attr-defined]
        self.broadcaster.publish(
            "idle", self._route(state=_build_state(agent))
        )
        return {"id": wsid, "wasActive": active_deleted}

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
                    workspace_switch_command(agent, wsid)
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
                    workspace_switch_command(agent, wsid)
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
                    workspace_switch_command(agent, original_wsid)
        except Exception:
            return False
        self.broadcaster.publish(
            "idle", {"state": _build_state(agent)}
        )
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
                return True

            # Non-active workspace: update its chat index directly on disk
            # without switching the active workspace.
            from ..agent import CHAT_STATE_FILE

            entry = agent._workspace_entry_by_selector(wsid)
            if not entry:
                return False
            storage = agent._workspace_storage_path(entry)
            index_path = storage / "chats" / CHAT_STATE_FILE
            if not index_path.exists():
                return False
            with open(index_path, "r", encoding="utf-8") as f:
                index = json.load(f)
            if not isinstance(index, dict):
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

    def _lookup_file_change(
        self, chat_id: str, ref: str, file_path: str,
    ) -> Optional[Dict[str, Any]]:
        """Find a single file change record by chat, ref, and file path."""
        _fc_by_chat: Dict[str, Any] = dict(
            getattr(self.agent, "_file_changes_by_chat", {}) or {}
        )
        store = _fc_by_chat.get(str(chat_id or ""), {})
        if not isinstance(store, dict):
            return None
        summary = store.get(str(ref or ""))
        if not isinstance(summary, dict):
            # Try loading from disk too.
            mgr = getattr(self.agent, "_chat_state_manager", None)
            if mgr is not None:
                try:
                    on_disk = mgr.load_file_changes(str(chat_id or ""))
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
                except Exception:
                    pass
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
        self, chat_id: str, ref: str, file_paths: List[str], undone: bool,
    ) -> None:
        """Mark files as undone/redone in the file-changes store and persist to disk."""
        _fc_by_chat: Dict[str, Any] = dict(
            getattr(self.agent, "_file_changes_by_chat", {}) or {}
        )
        cid = str(chat_id or "")
        store: dict = dict(_fc_by_chat.get(cid, {}))
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
        _fc_by_chat[cid] = store
        setattr(self.agent, "_file_changes_by_chat", _fc_by_chat)
        try:
            mgr = getattr(self.agent, "_chat_state_manager", None)
            if mgr is not None:
                mgr.save_file_changes(cid, store)
        except Exception:
            pass

    def _resolve_backup_full_path(
        self, chat_id: str, backup_name: str,
    ) -> Optional[Path]:
        """Convert a relative backup filename to an absolute path under the chat's
        backups directory."""
        mgr = getattr(self.agent, "_chat_state_manager", None)
        if mgr is None:
            return None
        backups_dir = getattr(mgr, "chat_backups_dir_for_chat", None)
        if not callable(backups_dir):
            return None
        bd = backups_dir(str(chat_id or ""))
        if bd is None:
            return None
        return bd / str(backup_name)

    def undo_file_changes(
        self, chat_id: str, ref: str, files: List[str],
    ) -> Dict[str, Any]:
        """Undo a list of files.  Returns {results: {filePath: {success, error?}}}."""
        outcome: Dict[str, Dict[str, Any]] = {}
        for fpath in files:
            try:
                fc = self._lookup_file_change(chat_id, ref, fpath)
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
                    if actual != expected_new:
                        outcome[fpath] = {"success": False, "error": "file modified since creation"}
                        continue
                    try:
                        Path(fpath).unlink()
                        outcome[fpath] = {"success": True}
                    except Exception as exc:
                        outcome[fpath] = {"success": False, "error": f"delete failed: {exc}"}
                elif ct == "delete":
                    bp = str(fc.get("backupPath", ""))
                    if not bp:
                        outcome[fpath] = {"success": False, "error": "no backup available"}
                        continue
                    backup_full = self._resolve_backup_full_path(chat_id, bp)
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
                    backup_full = self._resolve_backup_full_path(chat_id, bp)
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
                self._update_undone_files_state(chat_id, ref, undone_success, undone=True)
        return {"results": outcome}

    def reapply_file_changes(
        self, chat_id: str, ref: str, files: List[str],
    ) -> Dict[str, Any]:
        """Reapply a list of files.  Returns {results: {filePath: {success, error?}}}."""
        outcome: Dict[str, Dict[str, Any]] = {}
        for fpath in files:
            try:
                fc = self._lookup_file_change(chat_id, ref, fpath)
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
                elif ct == "delete":
                    bp = str(fc.get("backupPath", ""))
                    if not bp:
                        outcome[fpath] = {"success": False, "error": "no backup available"}
                        continue
                    backup_full = self._resolve_backup_full_path(chat_id, bp)
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
                self._update_undone_files_state(chat_id, ref, reapplied_success, undone=False)
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
                },
                cacheStats=_compute_chat_cache_stats(self.agent),
                tokenStats=_compute_chat_token_stats(self.agent),
                thinkingElapsedSeconds=round(
                    time.monotonic() - getattr(self.agent, "_gui_round_start_mono", time.monotonic()), 1
                ),
            ),
        )
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
            "state", self._route(state=_build_state(self.agent))
        )
        self.agent._gui_context_usage_changed = lambda: self.broadcaster.publish(  # type: ignore[attr-defined]
            "state", self._route(state=_build_state(self.agent))
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
        self.agent._gui_tool_output_emit = lambda text: self.broadcaster.publish(  # type: ignore[attr-defined]
            "output",
            self._route(text=str(text or "")),
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
            # Store as a dict keyed by hashcode
            _fc_by_chat = dict(getattr(self.agent, "_file_changes_by_chat", {}) or {})
            _fc_store: dict = dict(_fc_by_chat.get(cid, {}))
            _fc_store[_hash] = summary
            _fc_by_chat[cid] = _fc_store
            setattr(self.agent, "_file_changes_by_chat", _fc_by_chat)
            # Persist to disk so it survives restarts
            try:
                mgr = getattr(self.agent, "_chat_state_manager", None)
                if mgr is not None:
                    mgr.save_file_changes(cid, _fc_store)
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
            self.broadcaster.publish("file_changes", self._route(**summary))
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
                snapshot = self.agent._chat_state_manager.load_chat_state_snapshot(cfg)
            except Exception:
                return None
            ctx = {
                "config_dir": cfg,
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
        """
        try:
            try:
                self.agent._bind_session(rt.chat_id, rt.workspace_id)
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
            key = self._runtime_key(rt.chat_id, rt.workspace_id)
            with self._runtimes_lock:
                if self._runtimes.get(key) is rt:
                    self._runtimes.pop(key, None)


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
                self._send_json(200, app.chat_history(before, limit))
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
                result = app.read_chat_image(img_path)
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
                html = str(body.get("html") or "")
                result = app.save_preview_html(chat_id, html)
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
                app.interrupt()
                self._send_json(200, {"ok": True})
                return
            if path == "/compact":
                result = app.compact_context()
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
                    for _m in _session_out.get("messages", []) or []:
                        if not isinstance(_m, dict):
                            continue
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
                ref = str(body.get("ref") or "")[:256]
                file_list = body.get("files")
                if not isinstance(file_list, list):
                    file_list = []
                file_list = [str(f) for f in file_list if str(f).strip()]
                result = app.undo_file_changes(chat_id, ref, file_list)
                self._send_json(200, result)
                return
            if path == "/reapply-file-changes":
                chat_id = str(body.get("chatId") or "")[:256]
                ref = str(body.get("ref") or "")[:256]
                file_list = body.get("files")
                if not isinstance(file_list, list):
                    file_list = []
                file_list = [str(f) for f in file_list if str(f).strip()]
                result = app.reapply_file_changes(chat_id, ref, file_list)
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
