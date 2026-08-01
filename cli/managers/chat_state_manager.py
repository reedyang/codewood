import json
import logging
import os
import re
import secrets
import shutil
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.localization import translate

logger = logging.getLogger("codewood.chat_state")

# ``updated_at`` is a top-level record field serialized near the start of the
# file (before ``messages``), so the save-time disk-newer-wins check for
# unchanged chats can read just the header instead of parsing the whole JSON.
_UPDATED_AT_HEAD_RE = re.compile(r'"updated_at"\s*:\s*"([^"]+)"')
_UPDATED_AT_HEAD_BYTES = 1024


def _safe_replace(src: Path, dst: Path) -> None:
    """Replace *dst* with *src*, retrying on Windows transient locks."""
    try:
        os.replace(src, dst)
        return
    except OSError:
        if os.name != "nt":
            raise
    for delay in (0.02, 0.05, 0.12, 0.25, 0.5):
        try:
            os.replace(src, dst)
            return
        except OSError:
            time.sleep(delay)
    try:
        try:
            dst.unlink()
        except FileNotFoundError:
            pass
        os.replace(src, dst)
    except OSError:
        shutil.copy2(src, dst)
        try:
            src.unlink()
        except OSError:
            pass


CHAT_STATE_VERSION = 1

_PLAN_STATUSES = ("pending", "in_progress", "completed")
_PLAN_MAX_ITEMS = 32
_PLAN_MAX_STEP_CHARS = 200

_REPLY_NODE_KINDS = frozenset({"reasoning", "content", "tool_call"})
_REPLY_RAW_KIND = "raw"


def _normalize_reply_records(records: Any) -> List[Dict[str, Any]]:
    """Normalize a ``_reply_records`` list for persistence.

    Only whitelisted fields survive: node records keep kind/data/from/
    tool_call_id; the trailing raw record keeps kind/_split_source/content."""
    if not isinstance(records, list):
        return []
    out: List[Dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        kind = str(record.get("kind") or "").strip().lower()
        normalized: Dict[str, Any] = {"kind": kind}
        if kind == _REPLY_RAW_KIND:
            content = str(record.get("content") or "")
            if content:
                normalized["content"] = content
            if record.get("_split_source"):
                normalized["_split_source"] = True
        elif kind in _REPLY_NODE_KINDS:
            if kind == "tool_call":
                tcid = str(record.get("tool_call_id") or "").strip()
                if tcid:
                    normalized["tool_call_id"] = tcid
            data = record.get("data")
            if kind == "tool_call":
                if isinstance(data, dict):
                    normalized["data"] = data
            elif isinstance(data, str) and data:
                normalized["data"] = data
            src = str(record.get("from") or "").strip().lower()
            if src in ("native", "content_split"):
                normalized["from"] = src
        out.append(normalized)
    return out

# Chat interaction mode, persisted on the chat record root as ``"mode"``.
# ``"agent"`` is the default; ``"plan"`` is the sticky Plan mode.
CHAT_MODE_AGENT = "agent"
CHAT_MODE_PLAN = "plan"
_CHAT_MODES = (CHAT_MODE_AGENT, CHAT_MODE_PLAN)


def _read_chat_mode(raw: Dict[str, Any]) -> str:
    """Return the chat's interaction mode as ``"plan"`` or ``"agent"``.

    Reads the string ``"mode"`` field; anything missing or unrecognized
    defaults to Agent mode.
    """
    if not isinstance(raw, dict):
        return CHAT_MODE_AGENT
    mode = str(raw.get("mode") or "").strip().lower()
    return mode if mode in _CHAT_MODES else CHAT_MODE_AGENT


def _chat_mode_is_plan(raw: Dict[str, Any]) -> bool:
    """Convenience boolean form of :func:`_read_chat_mode`."""
    return _read_chat_mode(raw) == CHAT_MODE_PLAN


def _normalize_plan_items(raw_plan: Any) -> List[Dict[str, str]]:
    """Best-effort plan normalization used when loading or syncing chat state.

    Invalid entries are dropped instead of raising so a corrupted record
    cannot brick the whole chat history. The `update_plan` tool path
    performs strict validation before reaching this function.
    """
    if not isinstance(raw_plan, list):
        return []
    out: List[Dict[str, str]] = []
    for entry in raw_plan:
        if not isinstance(entry, dict):
            continue
        step_text = str(entry.get("step") or "").strip()
        if not step_text:
            continue
        step_text = " ".join(step_text.split())
        if len(step_text) > _PLAN_MAX_STEP_CHARS:
            step_text = step_text[:_PLAN_MAX_STEP_CHARS].rstrip()
        status = str(entry.get("status") or "").strip().lower()
        if status not in _PLAN_STATUSES:
            continue
        out.append({"step": step_text, "status": status})
        if len(out) >= _PLAN_MAX_ITEMS:
            break
    return out


class ChatStateManager:
    """Encapsulates chat state persistence and active-chat switching logic."""

    def __init__(self, agent: Any, chat_state_file: str) -> None:
        self._agent = agent
        self._chat_state_file = chat_state_file

    @staticmethod
    def _now_text() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _persist_ctx(self) -> Optional[Dict[str, Any]]:
        """Thread-local persistence override for a background chat's workspace.

        When set (by a loop thread whose chat belongs to a workspace OTHER than
        the agent's currently-focused one), persistence must target that
        workspace's index/dir/lock instead of the swapped-out agent globals.
        Returns ``None`` on threads with no override (the focused loop, HTTP
        and display threads), so the normal global path is used.
        """
        getter = getattr(self._agent, "_persist_workspace_ctx", None)
        if not callable(getter):
            return None
        try:
            ctx = getter()
        except Exception:
            return None
        if not isinstance(ctx, dict):
            return None
        # The override only applies while this loop's workspace is NOT the
        # agent's currently-focused one. When the user is focused on this
        # chat's own workspace the globals already point at the right
        # index/dir, so use them (and let cross-process merge-on-save logic
        # run against the live in-memory index).
        ctx_wsid = str(ctx.get("workspace_id") or "").strip()
        if not ctx_wsid:
            return None
        focused = str(getattr(self._agent, "workspace_id", "") or "").strip()
        if ctx_wsid == focused:
            return None
        # Resolve the concrete {config_dir, chat_state, lock} lazily via the
        # installed provider so the index snapshot is read from disk AFTER the
        # focus switch (the running loop persisted through the globals while it
        # was focused, so disk is current at switch time). The provider caches
        # per-workspace until the next focus switch invalidates it.
        provider = ctx.get("provider")
        if callable(provider):
            try:
                resolved = provider(ctx_wsid)
            except Exception:
                resolved = None
            try:
                from ..config.app_info import get_app_logger_root
                from ..core.logging.app_logging import get_logger

                cfg = resolved.get("config_dir") if isinstance(resolved, dict) else None
            except Exception:
                pass
            if isinstance(resolved, dict):
                return resolved
            return None
        return ctx

    def _active_chat_state(self) -> Dict[str, Any]:
        """The in-memory chat index this thread should persist into."""
        ctx = self._persist_ctx()
        if ctx is not None:
            state = ctx.get("chat_state")
            if isinstance(state, dict):
                return state
        state = getattr(self._agent, "_chat_state", None)
        return state if isinstance(state, dict) else {}

    def _active_chat_state_lock(self):
        ctx = self._persist_ctx()
        if ctx is not None:
            lock = ctx.get("lock")
            if lock is not None:
                return lock
        return getattr(self._agent, "_chat_state_lock", None)

    def chat_state_path(self) -> Path:
        return self.chat_records_dir() / self._chat_state_file

    def chat_records_dir(self) -> Path:
        ctx = self._persist_ctx()
        if ctx is not None:
            cfg = ctx.get("config_dir")
            if cfg:
                return Path(cfg) / "chats"
        return self._agent.workspace_config_dir / "chats"

    def _new_chat_record_filename(self) -> str:
        while True:
            name = f"{secrets.token_hex(16)}.json"
            if name != self._chat_state_file and not (self.chat_records_dir() / name).exists():
                return name

    def _chat_record_filename_for_chat(self, chat: Dict[str, Any]) -> str:
        existing = str(chat.get("_record_file") or "").strip()
        if existing:
            return existing
        name = self._new_chat_record_filename()
        chat["_record_file"] = name
        return name

    def _resolve_chat_record_path(self, record_file: str) -> Path:
        name = str(record_file or "").strip()
        rel = Path(name)
        if not name or rel.is_absolute() or rel.name != name:
            raise ValueError("chat record_file must be a file name")
        if name == self._chat_state_file:
            raise ValueError("chat record_file cannot be the chat index file")
        path = (self.chat_records_dir() / rel).resolve()
        records_dir = self.chat_records_dir().resolve()
        try:
            path.relative_to(records_dir)
        except ValueError as exc:
            raise ValueError("chat record_file must be under chats directory") from exc
        return path

    # Per-chat side data (pasted images, apply_patch change-preview sidecar) is
    # kept in a dedicated directory ``chats/data/<record-stem>/`` so each chat
    # owns one folder, it is trivially associated with its chat record, and it
    # can be deleted/cleaned up wholesale alongside the chat record.
    _CHAT_DATA_DIRNAME = "data"
    _CHAT_PREVIEWS_FILENAME = "previews.json"
    _CHAT_FILE_CHANGES_FILENAME = "file_changes.json"
    _CHAT_BACKUPS_DIRNAME = "backups"

    def _chat_data_dir_for_record_file(self, record_file: str) -> Optional[Path]:
        """Resolve ``chats/data/<record-stem>/`` for a chat record file name."""
        try:
            record_path = self._resolve_chat_record_path(record_file)
        except Exception:
            return None
        stem = (
            record_path.name[: -len(".json")]
            if record_path.name.endswith(".json")
            else record_path.name
        )
        return self.chat_records_dir() / self._CHAT_DATA_DIRNAME / stem

    def chat_data_dir(self, record_file: str) -> Optional[Path]:
        """Public accessor for a chat record's side-data directory."""
        return self._chat_data_dir_for_record_file(record_file)

    def chat_data_dir_for_chat(self, chat_id: str) -> Optional[Path]:
        """Resolve the side-data directory for ``chat_id`` (creating the chat's
        record file name if needed). Returns None when the chat is unknown."""
        cid = str(chat_id or "").strip()
        if not cid:
            return None
        chat = self.find_chat_by_id(cid)
        if not isinstance(chat, dict):
            return None
        record_file = self._chat_record_filename_for_chat(chat)
        return self._chat_data_dir_for_record_file(record_file)

    def chat_backups_dir_for_chat(self, chat_id: str) -> Optional[Path]:
        """Resolve the backups directory for ``chat_id`` under its chat data
        directory. Returns None when the chat is unknown."""
        data_dir = self.chat_data_dir_for_chat(chat_id)
        if data_dir is None:
            return None
        return data_dir / self._CHAT_BACKUPS_DIRNAME

    def _backup_corrupted_record(self, record_path: Path, expected_id: str, reason: str) -> None:
        """Back up a corrupted record file before it is skipped or overwritten."""
        try:
            corrupted = record_path.with_name(record_path.name + ".corrupted." + str(int(time.time())))
        except Exception:
            corrupted = record_path.with_name(record_path.name + ".corrupted")
        try:
            shutil.copy2(str(record_path), str(corrupted))
            logger.warning(
                "backup_corrupted_record: %s backed up to %s — %s",
                record_path.name, corrupted.name, reason,
            )
        except Exception:
            logger.warning(
                "backup_corrupted_record: could not back up %s (%s)",
                record_path.name, reason,
            )

    def _previews_path_for_record_file(self, record_file: str) -> Optional[Path]:
        data_dir = self._chat_data_dir_for_record_file(record_file)
        if data_dir is None:
            return None
        return data_dir / self._CHAT_PREVIEWS_FILENAME

    def chat_previews_path(self, chat_id: str) -> Optional[Path]:
        """Resolve the apply_patch preview sidecar path for ``chat_id`` (stored
        under the chat's side-data directory). Returns None when the chat is
        unknown."""
        data_dir = self.chat_data_dir_for_chat(chat_id)
        if data_dir is None:
            return None
        return data_dir / self._CHAT_PREVIEWS_FILENAME

    def chat_file_changes_path(self, chat_id: str) -> Optional[Path]:
        """Resolve the file-changes sidecar path for ``chat_id``."""
        data_dir = self.chat_data_dir_for_chat(chat_id)
        if data_dir is None:
            return None
        return data_dir / self._CHAT_FILE_CHANGES_FILENAME

    def save_file_changes(self, chat_id: str, summaries: Any) -> None:
        """Persist a dict of per-turn file-change summaries (keyed by hashcode ref)
        or a list (legacy turnIndex-based format) for ``chat_id`` to disk."""
        path = self.chat_file_changes_path(chat_id)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(summaries, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            _safe_replace(tmp, path)
        except Exception:
            pass

    def load_file_changes(self, chat_id: str) -> Optional[Any]:
        """Load persisted file-change data for ``chat_id``.

        Returns the raw stored shape so callers can distinguish:
        - new format: ``{ref: summary}``
        - legacy format: ``[summary, ...]`` or a single summary dict
        """
        path = self.chat_file_changes_path(chat_id)
        if path is None or not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data
            return None
        except Exception:
            return None

    def delete_chat_data(self, record_file: str) -> None:
        """Remove the entire side-data directory for a chat record being
        deleted (covers pasted images and the preview sidecar)."""
        data_dir = self._chat_data_dir_for_record_file(record_file)
        if data_dir is None:
            return
        try:
            if data_dir.exists():
                shutil.rmtree(data_dir, ignore_errors=True)
        except Exception:
            pass

    def cleanup_orphan_chat_data(self) -> None:
        """Delete any ``chats/data/<stem>/`` whose sibling chat record
        ``<stem>.json`` no longer exists. Called at startup so side data never
        outlives its chat."""
        try:
            records_dir = self.chat_records_dir()
            data_root = records_dir / self._CHAT_DATA_DIRNAME
            if not data_root.exists():
                return
            for child in data_root.iterdir():
                try:
                    if not child.is_dir():
                        continue
                    record = records_dir / f"{child.name}.json"
                    if not record.exists():
                        shutil.rmtree(child, ignore_errors=True)
                except Exception:
                    pass
        except Exception:
            pass

    def _last_used_chat_model(self) -> Tuple[str, str]:
        """Return ("provider", "model_name") of the workspace's latest chat.

        Picks the chat with the most recent ``updated_at`` that has a model
        recorded, so a freshly created chat inherits the user's last selection.
        Returns ("", "") when no existing chat carries a model.
        """
        try:
            state = getattr(self._agent, "_chat_state", None)
            chats = (state or {}).get("chats") if isinstance(state, dict) else None
            if not isinstance(chats, list):
                return "", ""
        except Exception:
            return "", ""
        best_key = ""
        best_provider = ""
        best_model = ""
        for c in chats:
            if not isinstance(c, dict):
                continue
            provider = str(c.get("model_provider") or "").strip()
            model_name = str(c.get("model_name") or "").strip()
            if not provider or not model_name:
                continue
            key = str(c.get("updated_at") or c.get("created_at") or "")
            if key >= best_key:
                best_key = key
                best_provider = provider
                best_model = model_name
        return best_provider, best_model

    def new_chat_entry(self, chat_id: str, name: str = "New Chat") -> Dict[str, Any]:
        now = self._now_text()
        # A new chat defaults to the model used by the most recently updated chat
        # in this workspace, so it inherits the user's last choice rather than the
        # shared global agent selection (which a concurrent chat may have changed).
        provider, model_name = self._last_used_chat_model()
        if not provider or not model_name:
            provider = str(getattr(self._agent, "provider", "") or "").strip()
            model_name = str(getattr(self._agent, "model_name", "") or "").strip()
        # Seed the mode from the live sticky flag so a chat created while Plan
        # mode is active (e.g. the GUI's draft compose toggled to Plan before
        # the first send creates the record) persists Plan rather than the bare
        # Agent default — otherwise the choice is lost on reload.
        mode = CHAT_MODE_PLAN if bool(getattr(self._agent, "_plan_mode_sticky", False)) else CHAT_MODE_AGENT
        # Context usage (percent/input tokens/window) is no longer persisted on
        # the chat record. The in-memory snapshot (``_last_context_*``) rebuilt
        # from the message history on activate is authoritative, so a new chat
        # starts with no usage fields and the GUI falls back to that snapshot.
        self.mark_chat_dirty(chat_id)
        return {
            "id": chat_id,
            "name": name,
            "name_source": "default",
            "created_at": now,
            "updated_at": now,
            "model_provider": provider,
            "model_name": model_name,
            "reasoning_level": "",
            "mode": mode,
            "messages": [],
            "pending_inputs": [],
            "archived": False,
            "first_user_message_at": "",
        }

    def _normalize_message(
        self,
        raw: Dict[str, Any],
    ) -> Dict[str, Any]:
        role = str(raw.get("role") or "").strip().lower()
        if role not in ("user", "assistant", "tool"):
            raise ValueError("invalid role")
        content = str(raw.get("content") or "")
        created_at = str(raw.get("created_at") or "").strip() or self._now_text()
        out = {
            "role": role,
            "content": content,
            "created_at": created_at,
        }
        if role == "tool":
            tcid = str(raw.get("tool_call_id") or "").strip()
            if tcid:
                out["tool_call_id"] = tcid
            tname = str(raw.get("name") or "").strip()
            if tname:
                out["name"] = tname
        if role == "assistant":
            plan_items = _normalize_plan_items(raw.get("plan"))
            if plan_items:
                out["plan"] = plan_items
                out["plan_explanation"] = str(raw.get("plan_explanation") or "").strip()
                out["plan_updated_at"] = str(raw.get("plan_updated_at") or "").strip()
        if bool(raw.get("exclude_from_model_context", False)):
            out["exclude_from_model_context"] = True
        if bool(raw.get("_internal", False)):
            out["_internal"] = True
        context_suffix = str(raw.get("_context_suffix") or "").strip()
        if context_suffix:
            out["_context_suffix"] = context_suffix
        thinking = str(raw.get("_thinking") or "").strip()
        if thinking:
            out["_thinking"] = thinking
        if raw.get("_thinking_from_content"):
            out["_thinking_from_content"] = True
        api_content = str(raw.get("_api_content") or "").strip()
        if api_content:
            out["_api_content"] = api_content
        cache_stats = raw.get("_cache_stats")
        if isinstance(cache_stats, dict) and cache_stats:
            out["_cache_stats"] = cache_stats
        output_tokens = raw.get("_output_tokens")
        if isinstance(output_tokens, int) and output_tokens > 0:
            out["_output_tokens"] = output_tokens
        reasoning_tokens = raw.get("_reasoning_tokens")
        if isinstance(reasoning_tokens, int):
            out["_reasoning_tokens"] = reasoning_tokens
        token_count_includes_reasoning = raw.get("_token_count_includes_reasoning")
        if isinstance(token_count_includes_reasoning, bool):
            out["_token_count_includes_reasoning"] = token_count_includes_reasoning
        token_count = raw.get("_token_count")
        if isinstance(token_count, (int, float)) and token_count > 0:
            out["_token_count"] = int(token_count)
        model_name = str(raw.get("_model") or "").strip()
        if model_name:
            out["_model"] = model_name
        tool_calls = raw.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            out["tool_calls"] = tool_calls
        tool_rounds = raw.get("tool_rounds")
        if isinstance(tool_rounds, list) and tool_rounds:
            out["tool_rounds"] = tool_rounds
        if role == "assistant":
            reply_records = raw.get("_reply_records")
            if isinstance(reply_records, list) and reply_records:
                out["_reply_records"] = _normalize_reply_records(reply_records)
        raw_rounds = raw.get("_tool_rounds_raw")
        if isinstance(raw_rounds, list) and raw_rounds:
            out["_tool_rounds_raw"] = raw_rounds
        pseudo_tool_call_text = str(raw.get("pseudo_tool_call_text") or "").strip()
        if pseudo_tool_call_text:
            out["pseudo_tool_call_text"] = pseudo_tool_call_text
            pseudo_tools = raw.get("pseudo_tool_call_tools")
            if isinstance(pseudo_tools, list):
                cleaned_tools = [
                    str(x).strip()
                    for x in pseudo_tools
                    if str(x).strip()
                ]
                if cleaned_tools:
                    out["pseudo_tool_call_tools"] = cleaned_tools
        return out

    def _validate_chat_entry(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(raw.get("id") or "").strip()
        if not cid:
            raise ValueError("chat id required")
        name = str(raw.get("name") or "").strip() or "New Chat"
        source = str(raw.get("name_source") or "default").strip().lower()
        if source not in ("default", "auto", "manual"):
            source = "default"

        messages_raw = raw.get("messages")
        if not isinstance(messages_raw, list):
            raise ValueError("messages must be list")
        messages = []
        for item in messages_raw:
            if not isinstance(item, dict):
                raise ValueError("message item must be object")
            messages.append(self._normalize_message(item))

        entry: Dict[str, Any] = {
            "id": cid,
            "name": name,
            "name_source": source,
            "created_at": str(raw.get("created_at") or "").strip() or self._now_text(),
            "updated_at": str(raw.get("updated_at") or "").strip() or self._now_text(),
            "model_provider": str(raw.get("model_provider") or "").strip(),
            "model_name": str(raw.get("model_name") or "").strip(),
            "reasoning_level": str(raw.get("reasoning_level") or "").strip(),
            # Interaction mode ("plan" or "agent") recorded on the chat record
            # root so reloading the chat (TUI or GUI) restores the sticky mode
            # the user last left it in. Missing/unknown values default to Agent.
            "mode": _read_chat_mode(raw),
            "messages": messages,
            "archived": bool(raw.get("archived", False)),
            "first_user_message_at": str(raw.get("first_user_message_at") or "").strip(),
        }
        # Preserve cross-process clarifying-prompt state. Another codewood
        # process (typically the TUI) writes ``pending_request_user_input`` onto
        # the chat record while it waits for the user's selection; this
        # process needs to surface the same panel when it focuses the chat,
        # so keep the field as-is rather than dropping it during validation.
        pending = raw.get("pending_request_user_input")
        if isinstance(pending, dict):
            entry["pending_request_user_input"] = dict(pending)
        # Preserve pending inputs queue (queued messages waiting to be sent
        # when the model is busy). Persisted alongside messages so they survive
        # a restart.
        pending_inputs = raw.get("pending_inputs")
        if isinstance(pending_inputs, list):
            entry["pending_inputs"] = [str(x) for x in pending_inputs if str(x).strip()]
        else:
            entry["pending_inputs"] = []
        # Preserve the GUI unread flag (blue dot) through validation so a
        # disk refresh (disk-newer-wins) does not silently clear it.
        if "has_unread" in raw and isinstance(raw.get("has_unread"), bool):
            entry["has_unread"] = raw["has_unread"]
        return entry

    def default_chat_state(self) -> Dict[str, Any]:
        default_chat = self.new_chat_entry("chat-1")
        return {"version": CHAT_STATE_VERSION, "active": "chat-1", "chats": [default_chat]}

    def _apply_chat_usage_snapshot(self, chat: Dict[str, Any]) -> None:
        # Context usage is no longer persisted on the chat record, so there is
        # nothing to restore from disk. Instead rebuild the in-memory snapshot
        # from the restored message history plus the active model's system
        # prompt and tool schemas. The runtime refresh invoked from
        # ``activate_chat`` performs the actual recompute; this helper exists to
        # keep call sites stable and to avoid reading a stale/zero value that
        # would otherwise clobber the live snapshot during reload.
        try:
            refresh = getattr(self._agent, "_refresh_status_context_usage_snapshot", None)
            if callable(refresh):
                refresh()
        except Exception:
            pass

    def _notify_gui_context_usage_changed(self) -> None:
        try:
            notify = getattr(self._agent, "_gui_context_usage_changed", None)
            if callable(notify):
                notify()
        except Exception:
            pass

    # ---- per-chat dirty tracking -----------------------------------------
    # ``save_chat_state`` used to re-read + re-serialize EVERY chat record on
    # every save (disk-newer-wins timestamp check + serialized-text compare),
    # which scaled linearly with the total chat history bytes and made chat
    # switching slow in workspaces with many/large chats. We now skip the
    # expensive read/serialize/write for chats that are unchanged since the
    # last time we loaded or wrote them, tracking dirtiness per chat. Keys are
    # scoped by the workspace's records dir so same-id chats in different
    # workspaces never share a dirty flag.

    def _dirty_scope(self) -> str:
        try:
            return str(self.chat_records_dir().resolve())
        except Exception:
            return "default"

    def _dirty_key(self, chat_id: str) -> str:
        cid = str(chat_id or "").strip()
        return f"{self._dirty_scope()}::{cid}" if cid else ""

    def _dirty_ids(self) -> set:
        ids = getattr(self._agent, "_chat_dirty_ids", None)
        if not isinstance(ids, set):
            ids = set()
            self._agent._chat_dirty_ids = ids
        return ids

    def mark_chat_dirty(self, chat_id: str) -> None:
        key = self._dirty_key(chat_id)
        if key:
            self._dirty_ids().add(key)

    def clear_chat_dirty(self, chat_id: str) -> None:
        key = self._dirty_key(chat_id)
        if key:
            self._dirty_ids().discard(key)

    def reset_chat_dirty(self) -> None:
        self._agent._chat_dirty_ids = set()

    def _seed_last_written_from_chats(self, chats: Any) -> None:
        """Record that freshly loaded chat records match their on-disk state.

        After ``load_chat_state`` / ``load_chat_state_snapshot`` the in-memory
        entries are byte-for-byte what is on disk, so a subsequent save can
        skip them entirely (no disk read, no serialization, no write) unless
        they are explicitly marked dirty or become active/running.
        """
        try:
            last_written = getattr(self._agent, "_last_saved_chat_updated_at", None)
            if not isinstance(last_written, dict):
                last_written = {}
                self._agent._last_saved_chat_updated_at = last_written
            if not isinstance(chats, list):
                return
            for chat in chats:
                if not isinstance(chat, dict):
                    continue
                rf = str(chat.get("_record_file") or "").strip()
                if not rf:
                    continue
                last_written[rf] = str(chat.get("updated_at") or "")
        except Exception:
            pass

    def set_chat_unread(self, chat_id: str, unread: bool) -> None:
        """Persist the GUI unread flag (blue dot) for a chat.

        The flag is stored on the chat record and in the ``chats.json`` index
        (``has_unread``), so it survives restarts. It is the source of truth
        for the sidebar's unread dot: set to ``True`` when a task finishes in
        a chat the user is not viewing, ``False`` when the chat is opened.
        Honors the thread-local persistence override, so a background loop
        thread can mark a chat in ITS OWN workspace unread without switching
        focus. No-op (no write) when the value is unchanged.
        """
        cid = str(chat_id or "").strip()
        if not cid:
            return
        target = bool(unread)
        changed = False
        try:
            with self._active_chat_state_lock():
                chats = self._active_chat_state().get("chats")
                if not isinstance(chats, list):
                    return
                for chat in chats:
                    if not isinstance(chat, dict):
                        continue
                    if str(chat.get("id") or "") == cid:
                        if bool(chat.get("has_unread", False)) != target:
                            chat["has_unread"] = target
                            changed = True
                        break
        except Exception:
            return
        if not changed:
            return
        try:
            try:
                logger.debug(
                    "set_chat_unread chat=%s -> %s (workspace=%s records_dir=%s)",
                    cid, target,
                    getattr(self._agent, "workspace_id", "?"),
                    self.chat_records_dir(),
                )
            except Exception:
                pass
            self.mark_chat_dirty(cid)
            self.save_chat_state()
        except Exception:
            pass

    def save_chat_state(self) -> None:
        # Serialize all writers under the agent's reentrant chat-state lock.
        # When several chat loops run concurrently they each persist their own
        # chat; without this guard two saves could interleave and tear the
        # shared index/records, or the stale-record sweep below could race a
        # sibling save. The lock is an RLock, so callers that already hold it
        # (activate_chat, sync_active_chat_messages, ...) are unaffected.
        lock = self._active_chat_state_lock()
        if lock is None:
            return self._save_chat_state_locked()
        with lock:
            return self._save_chat_state_locked()

    @staticmethod
    def _parse_record_timestamp(value: Any) -> float:
        """Parse a chat ``updated_at`` text into a comparable epoch float.

        Returns ``0.0`` for missing/unparseable values so a record that
        lacks a timestamp never "wins" a freshness comparison against one
        that has a real timestamp.
        """
        text = str(value or "").strip()
        if not text:
            return 0.0
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            return 0.0

    def _save_chat_state_locked(self) -> None:
        try:
            index_path = self.chat_state_path()
            records_dir = self.chat_records_dir()
            index_path.parent.mkdir(parents=True, exist_ok=True)
            records_dir.mkdir(parents=True, exist_ok=True)

            state = self._active_chat_state()
            chats = state.get("chats", [])
            if not isinstance(chats, list):
                chats = []

            active = str(state.get("active") or "").strip()
            # Chats this process is the authoritative writer for: the
            # currently-active chat plus any chat it owns a live runtime for.
            # For every OTHER chat we must avoid clobbering a record another
            # codewood process (e.g. a concurrent TUI/GUI) may have amended on
            # disk since we loaded it.
            #
            # The disk-newer-wins branch below ALSO guards the owned/active
            # chat: an owned chat is only force-overwritten when our in-memory
            # copy is at least as new as the disk record. When a peer has
            # written a strictly newer record for the active chat (e.g. the GUI
            # answered while the TUI sat idle on the same chat), we reload that
            # record instead of saving over it. Our own edits always bump
            # ``updated_at``, so a genuine local change keeps memory newer and
            # still wins. See tasks: "GUI switch chat wipes the message the TUI
            # just sent" and "TUI save should reload when the on-disk history is
            # newer than memory".
            # Chats with an in-flight turn must never defer to disk: their
            # in-memory state is being actively mutated and is authoritative.
            actively_running_ids: set = set()
            try:
                runtimes = getattr(self._agent, "_active_runtime_chat_ids", None)
                if callable(runtimes):
                    actively_running_ids |= {str(x) for x in (runtimes() or []) if str(x)}
            except Exception:
                pass
            try:
                if active and bool(getattr(self._agent, "_in_task_execution", False)):
                    actively_running_ids.add(active)
            except Exception:
                pass

            index_chats = []
            current_record_paths = set()
            index_dirty = False
            # Dirty/clean tracking: only chats whose record actually changed
            # get the expensive disk-read + serialize + write treatment.
            last_written = getattr(self._agent, "_last_saved_chat_updated_at", None)
            if not isinstance(last_written, dict):
                last_written = {}
                self._agent._last_saved_chat_updated_at = last_written
            dirty_keys = self._dirty_ids()
            dirty_scope = self._dirty_scope()

            def _index_entry() -> Dict[str, Any]:
                return {
                    "id": cid,
                    "name": str(chat.get("name") or "New Chat"),
                    "name_source": str(chat.get("name_source") or "default"),
                    "created_at": str(chat.get("created_at") or ""),
                    "updated_at": str(chat.get("updated_at") or ""),
                    "model_provider": str(chat.get("model_provider") or ""),
                    "model_name": str(chat.get("model_name") or ""),
                    "record_file": record_file,
                    "archived": bool(chat.get("archived", False)),
                    "first_user_message_at": str(chat.get("first_user_message_at") or ""),
                    "has_unread": bool(chat.get("has_unread", False)),
                }

            for chat in chats:
                if not isinstance(chat, dict):
                    continue
                cid = str(chat.get("id") or "").strip()
                if not cid:
                    continue
                record_file = self._chat_record_filename_for_chat(chat)
                record_path = self._resolve_chat_record_path(record_file)
                current_record_paths.add(record_path.resolve())
                record_path.parent.mkdir(parents=True, exist_ok=True)

                # Fast path: a chat we have loaded/written and not mutated since
                # needs no disk read, serialization, or write on this save. Only
                # chats that are active/running, explicitly dirty, missing on
                # disk, or whose ``updated_at`` moved since our last load/write
                # go through the full per-record handling below. This keeps a
                # save's cost proportional to the chats that actually changed
                # instead of the workspace's total chat history size.
                needs_full = (
                    cid in actively_running_ids
                    or cid == active
                    or f"{dirty_scope}::{cid}" in dirty_keys
                    or not record_path.exists()
                    or str(chat.get("updated_at") or "") != last_written.get(record_path.name, "")
                )
                if not needs_full:
                    # Cheap disk-newer-wins check for a clean chat: read only
                    # the record header (``updated_at`` is near the top) instead
                    # of parsing the whole JSON. If a peer process wrote a
                    # strictly newer record we refresh our in-memory copy the
                    # same way the full path does; otherwise there is nothing to
                    # write, so we skip the serialization and I/O entirely. This
                    # keeps a save's cost proportional to the chats that actually
                    # changed rather than the workspace's total history size.
                    _disk_ts = 0.0
                    _disk_upd = ""
                    try:
                        with open(record_path, "r", encoding="utf-8") as f:
                            _head = f.read(_UPDATED_AT_HEAD_BYTES)
                        _m = _UPDATED_AT_HEAD_RE.search(_head)
                        if _m:
                            _disk_upd = _m.group(1).strip()
                            _disk_ts = self._parse_record_timestamp(_disk_upd)
                    except Exception:
                        _disk_ts = 0.0
                        _disk_upd = ""
                    _mem_ts = self._parse_record_timestamp(chat.get("updated_at"))
                    if _disk_ts > _mem_ts:
                        try:
                            with open(record_path, "r", encoding="utf-8") as f:
                                _disk_raw = json.load(f)
                            if isinstance(_disk_raw, dict) and str(_disk_raw.get("id") or "") == cid:
                                _preserved_archived = bool(chat.get("archived", False))
                                _refreshed = self._validate_chat_entry(_disk_raw)
                                _refreshed["archived"] = _preserved_archived
                                _refreshed["_record_file"] = record_file
                                chat.clear()
                                chat.update(_refreshed)
                                last_written[record_path.name] = _disk_upd
                                dirty_keys.discard(f"{dirty_scope}::{cid}")
                        except Exception:
                            pass
                    index_chats.append(_index_entry())
                    continue

                # For a chat this process does not own, prefer a newer
                # on-disk record (written by a peer process) over our
                # possibly-stale in-memory copy. We read the disk record,
                # and if it is strictly newer we both keep it on disk
                # (skip the overwrite) and refresh our in-memory copy so
                # subsequent reads/saves stay consistent.
                write_record = True
                if cid not in actively_running_ids and record_path.exists():
                    try:
                        with open(record_path, "r", encoding="utf-8") as f:
                            disk_raw = json.load(f)
                    except Exception:
                        disk_raw = None
                    if isinstance(disk_raw, dict) and str(disk_raw.get("id") or "") == cid:
                        disk_ts = self._parse_record_timestamp(disk_raw.get("updated_at"))
                        mem_ts = self._parse_record_timestamp(chat.get("updated_at"))
                        if disk_ts > mem_ts:
                            try:
                                preserved_archived = bool(chat.get("archived", False))
                                refreshed = self._validate_chat_entry(disk_raw)
                                refreshed["archived"] = preserved_archived
                                refreshed["_record_file"] = record_file
                                chat.clear()
                                chat.update(refreshed)
                            except Exception:
                                pass
                            last_written[record_path.name] = str(chat.get("updated_at") or "")
                            dirty_keys.discard(f"{dirty_scope}::{cid}")
                            index_chats.append(_index_entry())
                            continue

                if write_record:
                    _record_ok = True
                    record_payload = {
                        k: v for k, v in chat.items()
                        if (not str(k).startswith("_") or k == "_tool_rounds_raw")
                        and k != "archived"
                    }
                    # Guard: the record payload's id must match the chat id from the
                    # index. A mismatch means the in-memory dict was cross-contaminated
                    # and writing it would permanently corrupt the record file.
                    if str(record_payload.get("id") or "").strip() != cid:
                        _record_ok = False
                        logger.error(
                            "save_chat_state: refusing to write %s — record id=%r != chat_id=%r. "
                            "Cross-contamination prevented; index entry preserved from memory. "
                            "chat=%s workspace=%s ws_root=%s records_dir=%s "
                            "active_chat_id=%s payload_msg_count=%d payload_name=%r\n%s",
                            record_path.name,
                            str(record_payload.get("id") or "").strip(),
                            cid,
                            getattr(self._agent, "workspace_id", "?"),
                            getattr(self._agent, "workspace_root", "?"),
                            self.chat_records_dir(),
                            getattr(self._agent, "active_chat_id", "?"),
                            len(record_payload.get("messages") or []),
                            str(record_payload.get("name") or ""),
                            "".join(traceback.format_stack()),
                        )
                    else:
                        # Guard: detect cross-workspace message contamination even
                        # when chat ids coincidentally match. The first user
                        # message's ``created_at`` is anchored on the chat entry
                        # as ``first_user_message_at``. If the messages being
                        # written carry a different first-user timestamp, they
                        # were loaded from another workspace's same-id chat.
                        write_allowed = True
                        anchored = str(chat.get("first_user_message_at") or "").strip()
                        if anchored:
                            msgs_for_check = record_payload.get("messages")
                            if isinstance(msgs_for_check, list):
                                first_user_at = ""
                                for m in msgs_for_check:
                                    if isinstance(m, dict) and str(m.get("role") or "").strip().lower() == "user":
                                        first_user_at = str(m.get("created_at") or "").strip()
                                        break
                                if first_user_at and first_user_at != anchored:
                                    _record_ok = False
                                    logger.error(
                                        "save_chat_state: refusing to write %s — "
                                        "first user msg timestamp (%s) != "
                                        "chat.first_user_message_at (%s). "
                                        "Cross-workspace contamination prevented. "
                                        "chat=%s active_chat_id=%s "
                                        "workspace=%s ws_root=%s records_dir=%s "
                                        "payload_msg_count=%d payload_name=%r\n%s",
                                        record_path.name, first_user_at, anchored,
                                        cid,
                                        getattr(self._agent, "active_chat_id", "?"),
                                        getattr(self._agent, "workspace_id", "?"),
                                        getattr(self._agent, "workspace_root", "?"),
                                        self.chat_records_dir(),
                                        len(msgs_for_check),
                                        str(record_payload.get("name") or ""),
                                        "".join(traceback.format_stack()),
                                    )
                                    write_allowed = False
                        if write_allowed:
                            # Compare with on-disk content; skip the write if unchanged.
                            # Avoids needless I/O and prevents rewriting identical records.
                            new_text = json.dumps(record_payload, ensure_ascii=False, indent=2) + "\n"
                            skip_write = False
                            if record_path.exists():
                                try:
                                    existing = record_path.read_text(encoding="utf-8")
                                    if existing == new_text:
                                        skip_write = True
                                except Exception:
                                    pass
                            if not skip_write:
                                tmp_path = record_path.with_name(record_path.name + ".tmp")
                                with open(tmp_path, "w", encoding="utf-8") as f:
                                    f.write(new_text)
                                _safe_replace(tmp_path, record_path)
                                index_dirty = True
                # The record now reflects the in-memory chat (written or already
                # identical on disk); mark it clean so the next save skips it.
                if _record_ok:
                    last_written[record_path.name] = str(chat.get("updated_at") or "")
                    dirty_keys.discard(f"{dirty_scope}::{cid}")
                index_chats.append(_index_entry())

            # Detect chat additions/deletions or active-chat changes even when
            # no record was rewritten: the index must be updated.
            if not index_dirty:
                last_count = getattr(self._agent, "_last_saved_index_count", None)
                if last_count is None or last_count != len(index_chats):
                    index_dirty = True
                last_active = getattr(self._agent, "_last_saved_active", None)
                if last_active is not None and last_active != active:
                    index_dirty = True

            # Only rewrite the index when something actually changed — avoids
            # needless I/O and reduces the risk window for index corruption.
            if index_dirty:
                index_payload = {
                    "version": CHAT_STATE_VERSION,
                    "active": active,
                    "chats": index_chats,
                }
                tmp_index = index_path.with_name(index_path.name + ".tmp")
                with open(tmp_index, "w", encoding="utf-8") as f:
                    json.dump(index_payload, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                _safe_replace(tmp_index, index_path)
                self._agent._last_saved_index_count = len(index_chats)
                self._agent._last_saved_active = active

                # Only sweep record files we know used to belong to this index
                # and are now gone from memory. A record on disk that this
                # process never had in memory may have been created by another
                # codewood process — deleting it would destroy a peer's chat,
                # so leave unknown records untouched.
                known_record_files = set()
                try:
                    known = getattr(self._agent, "_known_record_files_seen", None)
                    if isinstance(known, set):
                        known_record_files = known
                except Exception:
                    known_record_files = set()
                for stale in records_dir.glob("*.json"):
                    try:
                        if stale.resolve() == index_path.resolve():
                            continue
                        if stale.resolve() in current_record_paths:
                            continue
                        if stale.name not in known_record_files:
                            continue
                        logger.info(
                            "save_chat_state stale-sweep deleting record_file=%s "
                            "(not in current_index, known=%s)",
                            stale.name,
                            stale.name in known_record_files,
                        )
                        stale.unlink()
                        # Delete the chat's side-data directory alongside its record.
                        self.delete_chat_data(stale.name)
                    except Exception:
                        pass

                # Remember every record file we just wrote so a future save can
                # safely sweep one that genuinely disappears from this process's
                # memory (e.g. the user deleted the chat here).
                try:
                    seen = getattr(self._agent, "_known_record_files_seen", None)
                    if not isinstance(seen, set):
                        seen = set()
                        self._agent._known_record_files_seen = seen
                    for entry in index_chats:
                        rf = str(entry.get("record_file") or "").strip()
                        if rf:
                            seen.add(rf)
                except Exception:
                    pass
        except Exception as e:
            print(
                translate(
                    "warning.chat_state_save_failed",
                    getattr(self._agent, "display_language", "en") or "en",
                    error=e,
                )
            )

    def chat_entries(self) -> List[Dict[str, Any]]:
        state = self._active_chat_state()
        chats = state.get("chats", [])
        if not isinstance(chats, list):
            chats = []
            state["chats"] = chats
        return chats

    def find_chat_by_id(self, chat_id: str) -> Optional[Dict[str, Any]]:
        for c in self.chat_entries():
            if not isinstance(c, dict):
                continue
            if str(c.get("id") or "") == chat_id:
                return c
        return None

    def resolve_chat_selector(self, selector: str) -> Optional[Dict[str, Any]]:
        text = str(selector or "").strip()
        if not text:
            return None
        chats = self.chat_entries()
        if text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(chats):
                return chats[idx - 1]
        low = text.casefold()
        for c in chats:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or "")
            name = str(c.get("name") or "")
            if text == cid or low == name.casefold():
                return c
        return None

    def next_chat_id(self) -> str:
        existing = {
            str(c.get("id") or "")
            for c in self.chat_entries()
            if isinstance(c, dict)
        }
        i = 1
        while True:
            cid = f"chat-{i}"
            if cid not in existing:
                return cid
            i += 1

    def load_chat_state(self, create_default_chat: bool = True) -> None:
        p = self.chat_state_path()
        self._agent._startup_chat_state_warning = ""
        try:
            if not p.exists():
                self.reset_chat_dirty()
                if create_default_chat:
                    self._agent._chat_state = self.default_chat_state()
                    self._agent._last_saved_index_count = len(self._agent._chat_state.get("chats", []))
                    self._agent._last_saved_active = self._agent._chat_state.get("active", "")
                    self.activate_chat(
                        self._agent._chat_state["active"],
                        announce=False,
                        clear_screen=False,
                        print_history=False,
                        persist=True,
                    )
                else:
                    self._agent._chat_state = {"version": CHAT_STATE_VERSION, "active": "", "chats": []}
                    self._agent._last_saved_index_count = 0
                    self._agent._last_saved_active = ""
                    self._agent.active_chat_name = "New Chat"
                return
            with open(p, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("chat state root must be object")
            if int(loaded.get("version") or 0) != CHAT_STATE_VERSION:
                raise ValueError("chat state version mismatch")
            chats_raw = loaded.get("chats")
            if not isinstance(chats_raw, list):
                raise ValueError("chats must be list")
            chats = []
            for index_entry in chats_raw:
                if not isinstance(index_entry, dict):
                    continue
                cid = str(index_entry.get("id") or "").strip()
                if not cid:
                    raise ValueError("chat id required")
                record_file = str(index_entry.get("record_file") or "").strip()
                if not record_file:
                    raise ValueError("chat record_file required")
                try:
                    record_path = self._resolve_chat_record_path(record_file)
                    with open(record_path, "r", encoding="utf-8") as f:
                        chat_raw = json.load(f)
                    if not isinstance(chat_raw, dict):
                        raise ValueError("chat record root must be object")
                    if str(chat_raw.get("id") or "").strip() != cid:
                        self._backup_corrupted_record(record_path, cid,
                            f"record file id={str(chat_raw.get('id') or '').strip()!r} != index id={cid!r}")
                        raise ValueError("chat record id mismatch")
                    chat = self._validate_chat_entry(chat_raw)
                except Exception as ve:
                    logger.warning("load_chat_state: skipping chat %s: %s", cid, ve)
                    continue
                chat["_record_file"] = record_file
                # archived is stored only in the index (chats.json), not in the
                # record file; merge it into the in-memory entry so toggles are
                # preserved across reloads.
                archived = index_entry.get("archived")
                if isinstance(archived, bool):
                    chat["archived"] = archived
                first_user_msg_at = str(index_entry.get("first_user_message_at") or "").strip()
                if first_user_msg_at:
                    chat["first_user_message_at"] = first_user_msg_at
                # has_unread is stored in both the record file and the index;
                # merge the index copy so legacy records missing the field
                # still restore the flag across a restart.
                if "has_unread" in index_entry and isinstance(index_entry.get("has_unread"), bool):
                    chat["has_unread"] = index_entry["has_unread"]
                chats.append(chat)
            if not chats:
                # Empty chat list is valid (new workspace with no chats).
                # No ``activate_chat`` needed; the frontend will enter
                # draft mode when there is no active chat.
                self._agent._chat_state = {"version": CHAT_STATE_VERSION, "active": "", "chats": []}
                self._agent.active_chat_name = "New Chat"
                return
            # Seed the set of record files this process is aware of, so the
            # save-time stale sweep only ever deletes records that were
            # loaded here (and later removed locally) — never a record a
            # peer process created that this process simply hasn't seen.
            try:
                seen = getattr(self._agent, "_known_record_files_seen", None)
                if not isinstance(seen, set):
                    seen = set()
                    self._agent._known_record_files_seen = seen
                for c in chats:
                    rf = str(c.get("_record_file") or "").strip()
                    if rf:
                        seen.add(rf)
            except Exception:
                pass
            active = str(loaded.get("active") or "").strip()
            if not active or not any(str(c.get("id") or "") == active for c in chats):
                # The active chat from the index could not be loaded (its record
                # file was deleted, corrupted, or skipped by validation). Pick
                # the first available chat so we don't raise and trigger a full
                # state reset that deletes all other records via the stale sweep.
                logger.warning(
                    "load_chat_state: active=%r not in loaded chats (total=%d); "
                    "falling back to first available chat",
                    active, len(chats),
                )
                active = str(chats[0].get("id") or "")
            self._agent._chat_state = {"version": CHAT_STATE_VERSION, "active": active, "chats": chats}
            self._agent._last_saved_index_count = len(chats)
            self._agent._last_saved_active = active
            # Freshly loaded records match disk; seed clean-tracking so the next
            # save only touches the active/running chat.
            self.reset_chat_dirty()
            self._seed_last_written_from_chats(chats)
            # Drop any orphan chat side-data directories whose chat record is
            # gone (e.g. a chat deleted by a peer process) so pasted images and
            # preview sidecars never outlive their chat.
            self.cleanup_orphan_chat_data()
            self.activate_chat(
                active,
                announce=False,
                clear_screen=False,
                print_history=False,
                persist=False,
            )
            # The active chat is being displayed on load, so any unread flag it
            # carried over from a previous session no longer applies. Other
            # chats keep theirs (persistent blue dots until opened).
            try:
                self.set_chat_unread(active, False)
            except Exception:
                pass
        except Exception as e:
            logger.exception(
                "load_chat_state failed for %s; resetting chat state. total_chats=%d, active=%s, error=%s",
                p,
                len(chats_raw) if 'chats_raw' in dir() else -1,
                str(loaded.get("active", "N/A")) if 'loaded' in dir() else "N/A",
                e,
            )
            self._agent._startup_chat_state_warning = (
                f"⚠️ Failed to read chat state; it has been reset to the default session: {e}"
            )
            self.reset_chat_dirty()
            self._agent._chat_state = self.default_chat_state()
            self.activate_chat(
                self._agent._chat_state["active"],
                announce=False,
                clear_screen=False,
                print_history=False,
                persist=True,
            )

    def load_chat_state_snapshot(self, config_dir: Path) -> Dict[str, Any]:
        """Load a workspace's chat index+records into a standalone dict.

        Unlike :meth:`load_chat_state` this neither mutates the agent's global
        ``_chat_state`` nor activates a chat — it just returns an in-memory
        snapshot ``{"version", "active", "chats":[...]}`` for the workspace at
        ``config_dir``. Used to build a background loop's per-workspace
        persistence context so its turns persist to ITS OWN workspace while the
        agent's globals point at the focused workspace. Falls back to a default
        single-chat state if the index is missing/corrupt, so a background save
        never raises.
        """
        records_dir = Path(config_dir) / "chats"
        index_path = records_dir / self._chat_state_file
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("chat state root must be object")
            if int(loaded.get("version") or 0) != CHAT_STATE_VERSION:
                raise ValueError("chat state version mismatch")
            chats_raw = loaded.get("chats")
            if not isinstance(chats_raw, list):
                raise ValueError("chats must be list")
            chats: List[Dict[str, Any]] = []
            for index_entry in chats_raw:
                if not isinstance(index_entry, dict):
                    continue
                cid = str(index_entry.get("id") or "").strip()
                record_file = str(index_entry.get("record_file") or "").strip()
                if not cid or not record_file:
                    continue
                rel = Path(record_file)
                if rel.is_absolute() or rel.name != record_file:
                    continue
                record_path = (records_dir / rel).resolve()
                try:
                    record_path.relative_to(records_dir.resolve())
                except ValueError:
                    continue
                try:
                    with open(record_path, "r", encoding="utf-8") as f:
                        chat_raw = json.load(f)
                except Exception:
                    continue
                if not isinstance(chat_raw, dict):
                    continue
                if str(chat_raw.get("id") or "").strip() != cid:
                    continue
                chat = self._validate_chat_entry(chat_raw)
                chat["_record_file"] = record_file
                archived = index_entry.get("archived")
                if isinstance(archived, bool):
                    chat["archived"] = archived
                first_user_msg_at = str(index_entry.get("first_user_message_at") or "").strip()
                if first_user_msg_at:
                    chat["first_user_message_at"] = first_user_msg_at
                chats.append(chat)
            active = str(loaded.get("active") or "").strip()
            if chats and (
                not active or not any(str(c.get("id") or "") == active for c in chats)
            ):
                active = str(chats[0].get("id") or "")
            # Freshly loaded snapshot records match disk, so background-workspace
            # saves can skip them unless marked dirty.
            self._seed_last_written_from_chats(chats)
            return {
                "version": CHAT_STATE_VERSION,
                "active": active,
                "chats": chats,
            }
        except Exception:
            return self.default_chat_state()

    def refresh_chat_record_from_disk(self, chat_id: str) -> bool:
        """Re-read a single chat record from disk and merge it into memory.

        Codewood can run as several independent processes against the same
        workspace (e.g. a TUI session and a GUI window). Each process loads
        ``_chat_state`` once at startup; when another process amends a chat
        on disk (most importantly, persists ``pending_request_user_input`` while
        waiting on the user's selection) this process won't notice unless
        it explicitly re-reads the record. Call this just before surfacing
        a chat the local process does not own a live runtime for, so the
        GUI can render the prompt panel the TUI is currently driving (and
        replay any messages that landed since startup).

        Returns ``True`` when the record was successfully reloaded, else
        ``False`` (missing file, unknown chat, parse failure, etc.). The
        in-memory chat index is left untouched on failure.
        """
        cid = str(chat_id or "").strip()
        if not cid:
            return False
        agent = self._agent
        with agent._chat_state_lock:
            chat = self.find_chat_by_id(cid)
            if not chat:
                return False
            record_file = str(chat.get("_record_file") or "").strip()
            if not record_file:
                return False
            try:
                record_path = self._resolve_chat_record_path(record_file)
            except Exception:
                return False
            if not record_path.exists():
                return False
            try:
                with open(record_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:
                return False
            if not isinstance(raw, dict):
                return False
            if str(raw.get("id") or "").strip() != cid:
                return False
            try:
                refreshed = self._validate_chat_entry(raw)
            except Exception:
                return False
            refreshed["_record_file"] = record_file
            chats = self._agent._chat_state.get("chats")
            if not isinstance(chats, list):
                return False
            for idx, entry in enumerate(chats):
                if isinstance(entry, dict) and str(entry.get("id") or "") == cid:
                    # Preserve archived from the old entry (stored only in index)
                    refreshed["archived"] = bool(entry.get("archived", False))
                    # Legacy records may predate has_unread; keep the in-memory
                    # flag (restored from the index) if the disk record lacks it.
                    if "has_unread" not in refreshed:
                        refreshed["has_unread"] = bool(entry.get("has_unread", False))
                    chats[idx] = refreshed
                    # Memory now matches disk for this chat — clear its dirty
                    # flag and record the freshly-read timestamp so the next
                    # save skips it (unless it is active/running).
                    try:
                        last_written = getattr(self._agent, "_last_saved_chat_updated_at", None)
                        if not isinstance(last_written, dict):
                            last_written = {}
                            self._agent._last_saved_chat_updated_at = last_written
                        last_written[record_file] = str(refreshed.get("updated_at") or "")
                    except Exception:
                        pass
                    self.clear_chat_dirty(cid)
                    return True
        return False

    def sync_active_chat_messages(self) -> None:
        history = list(getattr(self._agent, "conversation_history", None) or [])
        msgs = []
        for m in history:
            if not isinstance(m, dict):
                continue
            if bool(m.get("persist_to_chat_state", True)) is False:
                continue
            role = str(m.get("role") or "").strip().lower()
            if role not in ("user", "assistant", "tool"):
                continue
            entry = {
                "role": role,
                "content": str(m.get("content") or ""),
                "created_at": str(m.get("created_at") or "").strip() or self._now_text(),
            }
            if role == "assistant":
                plan_items = _normalize_plan_items(m.get("plan"))
                if plan_items:
                    entry["plan"] = plan_items
                    entry["plan_explanation"] = str(m.get("plan_explanation") or "").strip()
                    entry["plan_updated_at"] = str(m.get("plan_updated_at") or "").strip()
            if bool(m.get("exclude_from_model_context", False)):
                entry["exclude_from_model_context"] = True
            if bool(m.get("_internal", False)):
                entry["_internal"] = True
            context_suffix = str(m.get("_context_suffix") or "").strip()
            if context_suffix:
                entry["_context_suffix"] = context_suffix
            api_content = str(m.get("_api_content") or "").strip()
            if api_content:
                entry["_api_content"] = api_content
            cache_stats = m.get("_cache_stats")
            if isinstance(cache_stats, dict) and cache_stats:
                entry["_cache_stats"] = cache_stats
            output_tokens = m.get("_output_tokens")
            if isinstance(output_tokens, int) and output_tokens > 0:
                entry["_output_tokens"] = output_tokens
            reasoning_tokens = m.get("_reasoning_tokens")
            if isinstance(reasoning_tokens, int):
                entry["_reasoning_tokens"] = reasoning_tokens
            token_count_includes_reasoning = m.get("_token_count_includes_reasoning")
            if isinstance(token_count_includes_reasoning, bool):
                entry["_token_count_includes_reasoning"] = token_count_includes_reasoning
            token_count = m.get("_token_count")
            if isinstance(token_count, (int, float)) and token_count > 0:
                entry["_token_count"] = int(token_count)
            model_name = str(m.get("_model") or "").strip()
            if model_name:
                entry["_model"] = model_name
            tool_calls = m.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                entry["tool_calls"] = tool_calls
            tool_rounds = m.get("tool_rounds")
            if isinstance(tool_rounds, list) and tool_rounds:
                entry["tool_rounds"] = tool_rounds
            if role == "assistant":
                reply_records = m.get("_reply_records")
                if isinstance(reply_records, list) and reply_records:
                    entry["_reply_records"] = reply_records
            raw_rounds = m.get("_tool_rounds_raw")
            if isinstance(raw_rounds, list) and raw_rounds:
                entry["_tool_rounds_raw"] = raw_rounds
            if role == "tool":
                tool_call_id = str(m.get("tool_call_id") or "").strip()
                if tool_call_id:
                    entry["tool_call_id"] = tool_call_id
                tool_name = str(m.get("name") or "").strip()
                if tool_name:
                    entry["name"] = tool_name
            pseudo_tool_call_text = str(m.get("pseudo_tool_call_text") or "").strip()
            if pseudo_tool_call_text:
                entry["pseudo_tool_call_text"] = pseudo_tool_call_text
                pseudo_tools = m.get("pseudo_tool_call_tools")
                if isinstance(pseudo_tools, list):
                    cleaned_tools = [
                        str(x).strip()
                        for x in pseudo_tools
                        if str(x).strip()
                    ]
                    if cleaned_tools:
                        entry["pseudo_tool_call_tools"] = cleaned_tools
            thinking = str(m.get("_thinking") or "").strip()
            if thinking:
                entry["_thinking"] = thinking
            if m.get("_thinking_from_content"):
                entry["_thinking_from_content"] = True
            msgs.append(entry)
        with self._active_chat_state_lock():
            chat = self.find_chat_by_id(self._agent.active_chat_id)
            try:
                from ..config.app_info import get_app_logger_root
                from ..core.logging.app_logging import get_logger

                get_logger(f"{get_app_logger_root()}.serve.wsswitch").info(
                    f"sync chat={self._agent.active_chat_id} found={bool(chat)} "
                    f"dir={self.chat_records_dir()} hist={len(history)}"
                )
            except Exception:
                pass
            if not chat:
                return
            prev_messages = list(chat.get("messages") or [])
            prev_count = len(prev_messages)
            new_count = len(msgs)
            # Detect message count anomalies that suggest cross-workspace
            # contamination: if the new message count is significantly larger
            # than the previous count in a single sync (not incremental growth
            # from a running turn), log a warning for diagnosis.
            if prev_count > 0 and new_count > prev_count + 50 and new_count > prev_count * 3:
                logger.warning(
                    "sync_active_chat_messages: suspicious message count jump "
                    "chat=%s prev=%d new=%d dir=%s",
                    getattr(self._agent, "active_chat_id", "?"),
                    prev_count, new_count,
                    self.chat_records_dir(),
                )
            chat["messages"] = msgs
            # Keep the first user message timestamp anchored on the chat entry
            # so the save-time guard can detect cross-workspace contamination
            # even when two chats share the same id. Updated on every sync so
            # edits to the first message are reflected.
            for m in msgs:
                if isinstance(m, dict) and str(m.get("role") or "").strip().lower() == "user":
                    chat["first_user_message_at"] = str(m.get("created_at") or "").strip()
                    break
            else:
                chat.pop("first_user_message_at", None)
            if prev_messages == msgs:
                return
            if msgs:
                chat["updated_at"] = self._now_text()
        self.mark_chat_dirty(self._agent.active_chat_id)
        self.save_chat_state()
        self._notify_gui_context_usage_changed()

    def persist_active_chat_usage_snapshot(self) -> None:
        # Context usage is no longer persisted on the chat record. The runtime
        # refresh (``_refresh_context_usage_snapshot_impl``) keeps the in-memory
        # ``_last_context_*`` snapshot current and calls this helper after each
        # update; its only remaining job is to surface the new value to the GUI.
        self._notify_gui_context_usage_changed()

    def save_pending_inputs(self, chat_id: str, inputs: List[str]) -> bool:
        cid = str(chat_id or "").strip()
        if not cid:
            return False
        lock = self._active_chat_state_lock()
        if lock is None:
            return False
        with lock:
            chat = self.find_chat_by_id(cid)
            if not chat:
                return False
            chat["pending_inputs"] = [str(x) for x in inputs if str(x).strip()]
            self.mark_chat_dirty(cid)
            self.save_chat_state()
            return True

    def clear_chat_context(self, chat_id: str) -> bool:
        cid = str(chat_id or "").strip()
        if not cid:
            return False
        with self._agent._chat_state_lock:
            chat = self.find_chat_by_id(cid)
            if not chat:
                return False
            chat["messages"] = []
            chat.pop("first_user_message_at", None)
            chat["updated_at"] = self._now_text()
            if cid == str(getattr(self._agent, "active_chat_id", "") or "").strip():
                self._agent._active_chat_plan = None
                self._agent._active_chat_plan_pending = False
                # Reset the in-memory usage snapshot so the GUI shows 0 until the
                # next runtime refresh recomputes it from the (now empty) history.
                self._agent._last_context_usage_percent = 0
                self._agent._last_context_input_tokens = 0
            self.mark_chat_dirty(cid)
            self.save_chat_state()
            self._notify_gui_context_usage_changed()
            return True

    @staticmethod
    def _latest_plan_snapshot_from_messages(
        messages: Any,
    ) -> Optional[Dict[str, Any]]:
        """Scan messages (most-recent first) for the last attached plan snapshot.

        Plans are stored in the message stream rather than on the chat root, so
        the "latest plan" is whatever the most recent assistant message recorded.
        """
        if not isinstance(messages, list):
            return None
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            items = _normalize_plan_items(msg.get("plan"))
            if items:
                return {
                    "plan": items,
                    "explanation": str(msg.get("plan_explanation") or ""),
                    "updated_at": str(msg.get("plan_updated_at") or ""),
                }
        return None

    def refresh_active_chat_plan_from_messages(self) -> None:
        """Reset the in-memory active plan from the active chat's message stream.

        Called when (re)activating a chat so the runtime loop's plan reminder
        reflects the loaded conversation's latest plan.
        """
        history = list(getattr(self._agent, "conversation_history", None) or [])
        snapshot = self._latest_plan_snapshot_from_messages(history)
        self._agent._active_chat_plan = snapshot
        self._agent._active_chat_plan_pending = False

    def active_chat_plan_mode(self) -> bool:
        """Return whether the active chat is recorded as being in Plan mode."""
        with self._agent._chat_state_lock:
            chat = self.find_chat_by_id(self._agent.active_chat_id)
            if not chat:
                return False
            return _chat_mode_is_plan(chat)

    def persist_active_chat_plan_mode(self, enabled: bool) -> bool:
        """Record the Plan-mode flag on the active chat's record root.

        Kept separate from the in-memory ``_plan_mode_sticky`` session flag:
        callers flip the session flag (so the running loop reacts immediately)
        and then call this so the choice survives a restart / chat reload.
        Returns True when there is an active chat to write to.
        """
        with self._agent._chat_state_lock:
            chat = self.find_chat_by_id(self._agent.active_chat_id)
            if not chat:
                return False
            new_mode = CHAT_MODE_PLAN if bool(enabled) else CHAT_MODE_AGENT
            if _read_chat_mode(chat) == new_mode:
                return True
            chat["mode"] = new_mode
            chat["updated_at"] = self._now_text()
            self.mark_chat_dirty(cid)
            try:
                self.save_chat_state()
            except Exception:
                pass
        return True

    def restore_active_chat_plan_mode(self) -> None:
        """Sync the session ``_plan_mode_sticky`` flag from the active chat.

        Called when (re)activating a chat so the loaded conversation resumes in
        whatever mode it was last left in. Best-effort; never raises.
        """
        try:
            self._agent._plan_mode_sticky = self.active_chat_plan_mode()
        except Exception:
            pass

    def active_chat_plan(self) -> Optional[Dict[str, Any]]:
        """Return a copy of the active chat's latest plan, or None if unset."""
        with self._agent._chat_state_lock:
            if not self.find_chat_by_id(self._agent.active_chat_id):
                return None
            snapshot = getattr(self._agent, "_active_chat_plan", None)
            if not isinstance(snapshot, dict):
                return None
            return {
                "plan": [dict(item) for item in (snapshot.get("plan") or []) if isinstance(item, dict)],
                "explanation": str(snapshot.get("explanation") or ""),
                "updated_at": str(snapshot.get("updated_at") or ""),
            }

    def persist_active_chat_plan(
        self,
        plan_items: List[Dict[str, str]],
        explanation: str = "",
    ) -> bool:
        """Stage the given plan as the active chat's latest plan.

        The plan is not written to the chat root; instead it is held in memory
        and stamped onto the next recorded assistant message (see
        ``attach_pending_plan_to_message``). Returns True if there is an active
        chat to attach the plan to. The caller is expected to provide
        already-validated items (e.g. via UpdatePlanTool); we still re-normalize
        defensively.
        """
        normalized = _normalize_plan_items(plan_items)
        with self._agent._chat_state_lock:
            chat = self.find_chat_by_id(self._agent.active_chat_id)
            if not chat:
                return False
            snapshot = {
                "plan": normalized,
                "explanation": str(explanation or "").strip(),
                "updated_at": self._now_text(),
            }
            self._agent._active_chat_plan = snapshot
            self._agent._active_chat_plan_pending = True
            # Also stamp the latest existing assistant message and persist now so
            # the plan survives a restart even when ``update_plan`` is the final
            # action of a turn (no later assistant message to carry it).
            self._stamp_plan_on_latest_assistant_message(snapshot)
        return True

    def _stamp_plan_on_latest_assistant_message(
        self,
        snapshot: Dict[str, Any],
    ) -> None:
        """Write the plan snapshot onto the most recent assistant message in the
        active conversation and persist the chat state. Best-effort.
        """
        items = _normalize_plan_items(snapshot.get("plan"))
        if not items:
            return
        history = getattr(self._agent, "conversation_history", None)
        if not isinstance(history, list):
            return
        for msg in reversed(history):
            if not isinstance(msg, dict):
                continue
            if str(msg.get("role") or "").strip().lower() != "assistant":
                continue
            # Only stamp the latest assistant message if it does NOT already
            # carry a plan. A message that already has a plan belongs to a
            # finalized prior turn; retroactively overwriting it would erase
            # that turn's plan snapshot. However, within the same turn the plan
            # evolves through multiple ``update_plan`` calls; the final plan
            # MUST replace any earlier stamp so it survives restart.
            msg["plan"] = items
            msg["plan_explanation"] = str(snapshot.get("explanation") or "").strip()
            msg["plan_updated_at"] = str(snapshot.get("updated_at") or "").strip()
            try:
                self.sync_active_chat_messages()
                self.save_chat_state()
            except Exception:
                pass
            return

    def attach_pending_plan_to_message(self, message: Dict[str, Any]) -> None:
        """Stamp the staged plan snapshot onto an assistant message in place.

        Does nothing unless (a) the message is an assistant message and (b) a
        plan update is pending since the last recorded message. After stamping,
        the pending flag is cleared so subsequent unchanged messages do not
        re-record the same plan.
        """
        if not isinstance(message, dict):
            return
        if str(message.get("role") or "").strip().lower() != "assistant":
            return
        if not getattr(self._agent, "_active_chat_plan_pending", False):
            return
        self._agent._active_chat_plan_pending = False
        snapshot = getattr(self._agent, "_active_chat_plan", None)
        if not isinstance(snapshot, dict):
            return
        items = _normalize_plan_items(snapshot.get("plan"))
        if not items:
            return
        message["plan"] = items
        message["plan_explanation"] = str(snapshot.get("explanation") or "").strip()
        message["plan_updated_at"] = str(snapshot.get("updated_at") or "").strip()

    def activate_chat(
        self,
        chat_id: str,
        announce: bool = True,
        clear_screen: bool = False,
        print_history: bool = False,
        persist: bool = True,
    ) -> str:
        logger.info(
            "activate_chat enter chat_id=%s prev_active=%s persist=%s",
            chat_id,
            str(getattr(self._agent, "active_chat_id", "") or ""),
            persist,
        )
        with self._agent._chat_state_lock:
            prev_active_chat_id = str(getattr(self._agent, "active_chat_id", "") or "").strip()
            prev_operation_results = list(getattr(self._agent, "operation_results", None) or [])
            chat = self.find_chat_by_id(chat_id)
            if not chat:
                return f"❌ Chat not found: {chat_id}"
            # Bind the calling thread to this chat's session so every
            # per-session assignment below (conversation_history, plan,
            # usage, ...) targets the right SessionState when multiple chat
            # loops run concurrently.
            bind = getattr(self._agent, "_bind_session", None)
            if callable(bind):
                bind(chat_id)
            self._agent._chat_state["active"] = chat_id
            self._agent.active_chat_id = chat_id
            self._agent.active_chat_name = str(chat.get("name") or "New Chat")
            self._agent.conversation_history = list(chat.get("messages") or [])
            hist_len = len(self._agent.conversation_history)
            # Reconcile session injection tracking when restoring history.
            # When switching to a different chat, clear cross-chat contamination
            # first, then rebuild tracking sets from restored history messages
            # so that skill/MCP prompt content already present in context is
            # not re-injected on subsequent forced references.
            if chat_id != prev_active_chat_id:
                self._agent._session_injected_skills = set()
                self._agent._session_injected_mcp_prompts = set()
                logger.info(
                    "activate_chat cleared session injection tracking: "
                    "switched from %s to %s, hist_len=%d",
                    prev_active_chat_id, chat_id, hist_len,
                )
            self._reconcile_session_injected_from_history()
            self.refresh_active_chat_plan_from_messages()
            # Resume the sticky Plan/Agent mode this chat was last left in.
            self.restore_active_chat_plan_mode()
            # Keep in-memory tool outcomes when reloading the same chat so
            # history replay can preserve failed/success visual markers.
            if chat_id == prev_active_chat_id:
                self._agent.operation_results = prev_operation_results
            else:
                self._agent.operation_results = []
            self._agent._session_summary_llm = ""
            self._agent._session_summary_rolling = ""
            self._agent._last_llm_summary_pair_count = 0
            try:
                self._agent._apply_chat_model_from_entry(chat, persist_if_missing=True)
            except Exception:
                pass
            # Always pin this chat's model onto the (now bound) session, even
            # when _apply_chat_model_from_entry short-circuited because the
            # selector already matched the globals: a concurrent chat could
            # have left the globals pointing at a different model.
            try:
                pin = getattr(self._agent, "_pin_session_model", None)
                if callable(pin):
                    pin()
            except Exception:
                pass
            self._apply_chat_usage_snapshot(chat)
            self._notify_gui_context_usage_changed()
            try:
                svc = getattr(self._agent, "session_memory_service", None)
                schedule_refresh = getattr(svc, "schedule_context_usage_refresh_async", None)
                if callable(schedule_refresh):
                    try:
                        schedule_refresh(
                            expected_chat_id=str(getattr(self._agent, "active_chat_id", "") or "").strip(),
                            context_hint="chat activated",
                        )
                    except TypeError:
                        schedule_refresh(context_hint="chat activated")
            except Exception:
                pass
            try:
                remember = getattr(self._agent, "_remember_active_chat_history_first_visible_index", None)
                if callable(remember):
                    remember(0 if print_history else len(list(self._agent.conversation_history or [])))
            except Exception:
                pass
            if persist:
                self.save_chat_state()
        if clear_screen:
            os.system("cls" if os.name == "nt" else "clear")
        if print_history:
            self._agent._print_chat_history()
        if announce:
            return f"✅ Switched to Chat: [{self._agent.active_chat_name}]"
        return ""

    def _reconcile_session_injected_from_history(self) -> None:
        """Scan restored conversation_history and rebuild session injection
        tracking sets so that content already present in context is not
        re-injected on subsequent forced references.

        This method MUST never raise — it runs inside ``activate_chat`` which is
        called from ``load_chat_state`` whose except-path resets the entire chat
        state and deletes orphaned record files via the stale-record sweep in
        ``save_chat_state``.
        """
        try:
            agent = self._agent
            history = list(getattr(agent, "conversation_history", None) or [])
            # Rebuild from scratch so the sets reflect *only* what the current
            # (possibly edited/truncated) history actually contains. On a
            # same-chat reload (e.g. after ``/edit`` rewinds history), the stale
            # in-memory set would otherwise retain skills that were injected by
            # the now-removed tail, preventing their prompt from being
            # re-injected when the edited message is re-sent.
            agent._session_injected_skills = set()
            agent._session_injected_mcp_prompts = set()
            if not history:
                return
            skill_pattern = re.compile(
                r"----- BEGIN SKILL PROMPT \(skill_id=([^)]+)\) -----"
            )
            mcp_pattern = re.compile(
                r"----- BEGIN MCP PROMPT \(server=([^,]+),\s*name=([^)]+)\) -----"
            )
            for msg in history:
                if not isinstance(msg, dict):
                    continue
                content = str(msg.get("content") or "")
                if not content:
                    continue
                for m in skill_pattern.finditer(content):
                    sid = m.group(1).strip()
                    if sid:
                        canon = getattr(agent, "_canonical_skill_id", None)
                        if callable(canon):
                            canon_sid = canon(sid)
                            if canon_sid:
                                agent._session_injected_skills.add(canon_sid)
                        else:
                            agent._session_injected_skills.add(sid.lower())
                for m in mcp_pattern.finditer(content):
                    srv = m.group(1).strip()
                    name = m.group(2).strip()
                    if srv and name:
                        agent._session_injected_mcp_prompts.add(f"{srv}/{name}")
        except Exception:
            logger.exception("_reconcile_session_injected_from_history failed")
