"""Persistent cache for the *history* part of the model context, stored in the
exact form that is sent to the provider.

Background
----------
When historical messages are packed into the model context we rebuild a
normalized representation of every history message on each request (reply-block
flattening, ``_normalize_history_content_for_model``, assistant clipping, ...).
That assembly is deterministic per message and is the most expensive part of
context packing.

This module caches the assembled history — as ``{role, content, tool_calls,
tool_call_id, name, reasoning_content}`` dicts, i.e. the clean wire format, with
**no internal bookkeeping** such as ``_model`` / ``_cache_stats`` / ``_token_count`` —
so that:

* new messages only *append* to the cached prefix (bytes stay stable -> better
  provider prompt-prefix cache hit rate), and
* an edited / rewound message invalidates the cache from that point onward,
  forcing a re-assembly (which we then re-cache).

Only the history messages are cached — never the system prompt, tool schemas or
model parameters. Because different API kinds serialize history differently,
the cache is keyed by the API kind (replay mode) plus the assistant clip budget;
switching the API kind simply re-generates the cache.

The cache file lives at ``chats/<YYYY>/<MM>/<DD>/data/<record-stem>/`` alongside
the chat record's other side data.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# Name of the sidecar file kept inside the chat's data directory.
_CACHE_FILENAME = "context_history.json"

# Cache revision. Bump whenever the history-to-wire assembly logic changes in
# a way that changes the serialized form of a message (or the cache format
# itself) — stale caches are detected by a rev mismatch and rebuilt.
CACHE_REV = 1

_write_lock = threading.Lock()


def _message_sig(msg: Optional[Dict[str, Any]]) -> str:
    """Deterministic content signature for one source history message.

    The signature is only ever stored for the *tail* message (``tail_sig``).
    Because editing always truncates the history and clears the cache, a single
    boundary signature is enough to prove the whole cached prefix still
    matches the current history.
    """
    try:
        payload = json.dumps(
            msg,
            default=str,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except Exception:
        payload = repr(msg)
    return hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()


def _chat_data_dir(agent: Any) -> Optional[Path]:
    """Resolve the active chat's data directory, or None when unavailable."""
    try:
        cid = str(getattr(agent, "active_chat_id", "") or "").strip()
    except Exception:
        return None
    if not cid:
        return None
    mgr = getattr(agent, "_chat_state_manager", None)
    if mgr is None:
        mgr = getattr(agent, "chat_state_manager", None)
    if mgr is None:
        return None
    try:
        path = mgr.chat_data_dir_for_chat(cid)
    except Exception:
        return None
    if path is None:
        return None
    try:
        return Path(path)
    except Exception:
        return None


def history_cache_path(agent: Any) -> Optional[Path]:
    """The cache file path for the active chat, or None when unavailable."""
    data_dir = _chat_data_dir(agent)
    if data_dir is None:
        return None
    return data_dir / _CACHE_FILENAME


def build_api_key(replay_mode: str, assistant_clip_tokens: int) -> str:
    """Cache key covering everything that changes the serialized history.

    ``replay_mode`` selects merged vs interleaved reply-block replay and the
    assistant clip budget changes the (post-normalization) clipping. Other
    assembly-parameter drift is covered by ``CACHE_REV``.
    """
    return f"{str(replay_mode or '')}|{int(assistant_clip_tokens or 0)}"


def load_history_cache(agent: Any) -> Optional[Dict[str, Any]]:
    """Return the parsed cache dict (with ``meta`` and ``rows``), or None."""
    path = history_cache_path(agent)
    if path is None:
        return None
    try:
        if not path.is_file():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    rows_raw = raw.get("rows")
    if not isinstance(rows_raw, list):
        return None
    rows: List[Dict[str, Any]] = []
    for row in rows_raw:
        if not isinstance(row, dict):
            continue
        if not isinstance(row.get("idx"), int):
            continue
        rows.append(
            {
                "idx": row["idx"],
                "msg": row.get("msg"),
            }
        )
    return {
        "meta": {
            "rev": raw.get("rev"),
            "api_key": raw.get("api_key"),
            "tail_sig": raw.get("tail_sig"),
            "start_idx": raw.get("start_idx"),
        },
        "rows": rows,
    }


def save_history_message_cache(
    agent: Any,
    *,
    rows: List[Dict[str, Any]],
    api_key: str,
    tail_sig: str,
    start_idx: int = 0,
) -> bool:
    """Atomically write ``rows`` (each ``{idx, msg|None}``) for the given
    ``api_key`` to the active chat's cache.

    ``tail_sig`` is the deterministic signature of the *last* source history
    message covered by ``rows``. Cache reuse only ever trusts the whole
    prefix when this one boundary signature still matches the current history
    tail (edits always truncate + clear the cache, so per-row signatures are
    unnecessary).

    ``start_idx`` is the index in the eligible history where the cached
    prefix begins. A post-edit prune may only keep the cached head when the
    surviving history starts at the same ``start_idx`` — otherwise the prefix
    would silently misalign (e.g. an edit that removed a compaction summary).
    """
    path = history_cache_path(agent)
    if path is None:
        return False
    try:
        data = {
            "rev": CACHE_REV,
            "api_key": str(api_key or ""),
            "tail_sig": str(tail_sig or ""),
            "start_idx": int(start_idx or 0),
            "rows": rows,
        }
        payload = json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock:
            fd, tmp = tempfile.mkstemp(
                prefix=".ctx_history_", dir=str(path.parent), suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp, str(path))
            except BaseException:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
                raise
        return True
    except Exception:
        return False


def clear_history_cache(agent: Any) -> None:
    """Best-effort removal of the active chat's history cache file."""
    path = history_cache_path(agent)
    if path is None:
        return
    try:
        if path.exists():
            with _write_lock:
                path.unlink(missing_ok=True)
    except Exception:
        pass


def prune_history_cache(
    agent: Any,
    *,
    api_key: str,
    keep_count: int,
    tail_sig: str,
    start_idx: int = 0,
) -> bool:
    """Trim the persistent cache to ``keep_count`` head rows (the surviving
    history after an edit truncation), preserving the prefix so the next
    context pack does not reassemble it from scratch.

    Only a *prefix* trim is ever allowed. Callers must pass the reconciled
    ``api_key`` and the ``start_idx`` of the surviving history; if those do not
    match the cached meta (e.g. an edit removed the compaction summary, so the
    candidate prefix no longer starts at the same eligible-history position),
    the whole cache is cleared so the next pack rebuilds correctly rather than
    serving a misaligned prefix.

    Returns True if a prefix was kept (or the cache was cleared as a fallback),
    False if there was nothing to prune (e.g. no cache file).
    """
    path = history_cache_path(agent)
    if path is None or not path.is_file():
        return False
    try:
        cache = load_history_cache(agent)
        if cache is None:
            return False
        meta = cache.get("meta") or {}
        rows = list(cache.get("rows") or [])
        if (
            meta.get("rev") != CACHE_REV
            or meta.get("api_key") != api_key
            or meta.get("start_idx") != start_idx
            or keep_count <= 0
        ):
            # Prefix would misalign or the cache is stale: drop it entirely so
            # the next pack rebuilds from the (truncated) history.
            clear_history_cache(agent)
            return True
        keep_count = min(keep_count, len(rows))
        data = {
            "rev": CACHE_REV,
            "api_key": str(api_key or ""),
            "tail_sig": str(tail_sig or ""),
            "start_idx": int(start_idx or 0),
            "rows": rows[:keep_count],
        }
        payload = json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock:
            fd, tmp = tempfile.mkstemp(
                prefix=".ctx_history_", dir=str(path.parent), suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp, str(path))
            except BaseException:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
                raise
        return True
    except Exception:
        return False


def to_send_message(entry: Dict[str, Any], include_thinking: bool = True) -> Dict[str, Any]:
    """Strip internal bookkeeping from an assembled entry so the cached result
    is exactly the wire payload sent to the model.

    Kept fields: ``role``, ``content``, ``tool_calls`` (assistant),
    ``tool_call_id`` / ``name`` (tool), and ``reasoning_content`` (assistant
    native thinking, when the provider consumes it). Everything else (``_model``,
    ``_cache_stats``, ``_token_count``, …) is dropped.
    """
    role = str(entry.get("role") or "").strip().lower()
    msg: Dict[str, Any] = {"role": role}
    content = entry.get("content")
    msg["content"] = str(content) if content is not None else ""
    if role == "assistant":
        tcs = entry.get("tool_calls")
        if isinstance(tcs, list) and tcs:
            msg["tool_calls"] = tcs
        thinking = str(entry.get("_thinking") or "").strip()
        if thinking:
            from_content = bool(entry.get("_thinking_from_content"))
            if include_thinking and not from_content:
                msg["reasoning_content"] = thinking
    elif role == "tool":
        tid = str(entry.get("tool_call_id") or "").strip()
        if tid:
            msg["tool_call_id"] = tid
        tname = str(entry.get("name") or "").strip()
        if tname:
            msg["name"] = tname
    return msg