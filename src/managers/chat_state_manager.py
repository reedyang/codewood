import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.localization import translate


CHAT_STATE_VERSION = 1

_PLAN_STATUSES = ("pending", "in_progress", "completed")
_PLAN_MAX_ITEMS = 32
_PLAN_MAX_STEP_CHARS = 200


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
                get_logger(f"{get_app_logger_root()}.serve.wsswitch").info(
                    f"override ACTIVE ctx_ws={ctx_wsid} focused={focused} dir={cfg}"
                )
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
        usage_pct = int(getattr(self._agent, "_last_context_usage_percent", 0) or 0)
        usage_tokens = int(getattr(self._agent, "_last_context_input_tokens", 0) or 0)
        usage_window = int(getattr(self._agent, "_last_context_window", 0) or 0)
        return {
            "id": chat_id,
            "name": name,
            "name_source": "default",
            "created_at": now,
            "updated_at": now,
            "model_provider": provider,
            "model_name": model_name,
            "reasoning_level": "",
            "messages": [],
            "context_usage_percent": usage_pct,
            "context_input_tokens": usage_tokens,
            "context_window": usage_window,
            "plan_mode": False,
        }

    def _normalize_message(
        self,
        raw: Dict[str, Any],
    ) -> Dict[str, Any]:
        role = str(raw.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            raise ValueError("invalid role")
        content = str(raw.get("content") or "")
        created_at = str(raw.get("created_at") or "").strip() or self._now_text()
        out = {
            "role": role,
            "content": content,
            "created_at": created_at,
        }
        if role == "assistant":
            plan_items = _normalize_plan_items(raw.get("plan"))
            if plan_items:
                out["plan"] = plan_items
                out["plan_explanation"] = str(raw.get("plan_explanation") or "").strip()
                out["plan_updated_at"] = str(raw.get("plan_updated_at") or "").strip()
        if bool(raw.get("exclude_from_model_context", False)):
            out["exclude_from_model_context"] = True
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
            "messages": messages,
            "context_usage_percent": int(raw.get("context_usage_percent") or 0),
            "context_input_tokens": int(raw.get("context_input_tokens") or 0),
            "context_window": int(raw.get("context_window") or 0),
            # Whether this chat is in Plan mode. Recorded on the chat record
            # root so reloading the chat (TUI or GUI) restores the sticky mode
            # the user last left it in. Older records lack the field and
            # default to Agent mode (False).
            "plan_mode": bool(raw.get("plan_mode", False)),
        }
        # Preserve cross-process clarifying-prompt state. Another codewood
        # process (typically the TUI) writes ``pending_ask_more_info`` onto
        # the chat record while it waits for the user's selection; this
        # process needs to surface the same panel when it focuses the chat,
        # so keep the field as-is rather than dropping it during validation.
        pending = raw.get("pending_ask_more_info")
        if isinstance(pending, dict):
            entry["pending_ask_more_info"] = dict(pending)
        return entry

    def default_chat_state(self) -> Dict[str, Any]:
        default_chat = self.new_chat_entry("chat-1")
        return {"version": CHAT_STATE_VERSION, "active": "chat-1", "chats": [default_chat]}

    def _apply_chat_usage_snapshot(self, chat: Dict[str, Any]) -> None:
        try:
            self._agent._last_context_usage_percent = int(chat.get("context_usage_percent") or 0)
        except Exception:
            self._agent._last_context_usage_percent = 0
        try:
            self._agent._last_context_input_tokens = int(chat.get("context_input_tokens") or 0)
        except Exception:
            self._agent._last_context_input_tokens = 0
        try:
            self._agent._last_context_window = int(chat.get("context_window") or 0)
        except Exception:
            self._agent._last_context_window = 0

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

                # For a chat this process does not own, prefer a newer
                # on-disk record (written by a peer process) over our
                # possibly-stale in-memory copy. We read the disk record,
                # and if it is strictly newer we both keep it on disk
                # (skip the overwrite) and refresh our in-memory copy so
                # subsequent reads/saves stay consistent.
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
                                refreshed = self._validate_chat_entry(disk_raw)
                                refreshed["_record_file"] = record_file
                                chat.clear()
                                chat.update(refreshed)
                            except Exception:
                                pass
                            index_chats.append(
                                {
                                    "id": cid,
                                    "name": str(chat.get("name") or "New Chat"),
                                    "name_source": str(chat.get("name_source") or "default"),
                                    "created_at": str(chat.get("created_at") or ""),
                                    "updated_at": str(chat.get("updated_at") or ""),
                                    "model_provider": str(chat.get("model_provider") or ""),
                                    "model_name": str(chat.get("model_name") or ""),
                                    "record_file": record_file,
                                }
                            )
                            continue

                record_payload = {
                    k: v for k, v in chat.items() if not str(k).startswith("_")
                }
                with open(record_path, "w", encoding="utf-8") as f:
                    json.dump(record_payload, f, ensure_ascii=False, indent=2)
                index_chats.append(
                    {
                        "id": cid,
                        "name": str(chat.get("name") or "New Chat"),
                        "name_source": str(chat.get("name_source") or "default"),
                        "created_at": str(chat.get("created_at") or ""),
                        "updated_at": str(chat.get("updated_at") or ""),
                        "model_provider": str(chat.get("model_provider") or ""),
                        "model_name": str(chat.get("model_name") or ""),
                        "record_file": record_file,
                    }
                )

            index_payload = {
                "version": CHAT_STATE_VERSION,
                "active": active,
                "chats": index_chats,
            }
            with open(index_path, "w", encoding="utf-8") as f:
                json.dump(index_payload, f, ensure_ascii=False, indent=2)

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
                        # Unknown to this process: assume a peer owns it.
                        continue
                    stale.unlink()
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

    def load_chat_state(self) -> None:
        p = self.chat_state_path()
        self._agent._startup_chat_state_warning = ""
        try:
            if not p.exists():
                self._agent._chat_state = self.default_chat_state()
                self.activate_chat(
                    self._agent._chat_state["active"],
                    announce=False,
                    clear_screen=False,
                    print_history=False,
                    persist=True,
                )
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
                record_path = self._resolve_chat_record_path(record_file)
                with open(record_path, "r", encoding="utf-8") as f:
                    chat_raw = json.load(f)
                if not isinstance(chat_raw, dict):
                    raise ValueError("chat record root must be object")
                if str(chat_raw.get("id") or "").strip() != cid:
                    raise ValueError("chat record id mismatch")
                chat = self._validate_chat_entry(chat_raw)
                chat["_record_file"] = record_file
                chats.append(chat)
            if not chats:
                raise ValueError("chats empty")
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
                raise ValueError("active chat invalid")
            self._agent._chat_state = {"version": CHAT_STATE_VERSION, "active": active, "chats": chats}
            self.activate_chat(
                active,
                announce=False,
                clear_screen=False,
                print_history=False,
                persist=False,
            )
        except Exception as e:
            self._agent._startup_chat_state_warning = (
                f"⚠️ Failed to read chat state; it has been reset to the default session: {e}"
            )
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
                chats.append(chat)
            active = str(loaded.get("active") or "").strip()
            if chats and (
                not active or not any(str(c.get("id") or "") == active for c in chats)
            ):
                active = str(chats[0].get("id") or "")
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
        on disk (most importantly, persists ``pending_ask_more_info`` while
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
                    chats[idx] = refreshed
                    return True
        return False

    def sync_active_chat_messages(self) -> None:
        with self._active_chat_state_lock():
            chat = self.find_chat_by_id(self._agent.active_chat_id)
            try:
                from ..config.app_info import get_app_logger_root
                from ..core.logging.app_logging import get_logger

                get_logger(f"{get_app_logger_root()}.serve.wsswitch").info(
                    f"sync chat={self._agent.active_chat_id} found={bool(chat)} "
                    f"dir={self.chat_records_dir()} hist={len(list(getattr(self._agent,'conversation_history',None) or []))}"
                )
            except Exception:
                pass
            if not chat:
                return
            msgs = []
            for m in list(self._agent.conversation_history):
                if not isinstance(m, dict):
                    continue
                if bool(m.get("persist_to_chat_state", True)) is False:
                    continue
                role = str(m.get("role") or "").strip().lower()
                if role not in ("user", "assistant"):
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
                msgs.append(entry)
            chat["messages"] = msgs
            chat["context_usage_percent"] = int(
                getattr(self._agent, "_last_context_usage_percent", 0) or 0
            )
            chat["context_input_tokens"] = int(
                getattr(self._agent, "_last_context_input_tokens", 0) or 0
            )
            chat["context_window"] = int(
                getattr(self._agent, "_last_context_window", 0) or 0
            )
            chat["updated_at"] = self._now_text()
            self.save_chat_state()

    def persist_active_chat_usage_snapshot(self) -> None:
        with self._active_chat_state_lock():
            chat = self.find_chat_by_id(self._agent.active_chat_id)
            if not chat:
                return
            chat["context_usage_percent"] = int(
                getattr(self._agent, "_last_context_usage_percent", 0) or 0
            )
            chat["context_input_tokens"] = int(
                getattr(self._agent, "_last_context_input_tokens", 0) or 0
            )
            chat["context_window"] = int(
                getattr(self._agent, "_last_context_window", 0) or 0
            )
            chat["updated_at"] = self._now_text()
            self.save_chat_state()

    def clear_chat_context(self, chat_id: str) -> bool:
        cid = str(chat_id or "").strip()
        if not cid:
            return False
        with self._agent._chat_state_lock:
            chat = self.find_chat_by_id(cid)
            if not chat:
                return False
            chat["messages"] = []
            chat["context_usage_percent"] = 0
            chat["context_input_tokens"] = 0
            chat["context_window"] = int(
                getattr(self._agent, "_last_context_window", 0) or 0
            )
            chat["updated_at"] = self._now_text()
            if cid == str(getattr(self._agent, "active_chat_id", "") or "").strip():
                self._agent._active_chat_plan = None
                self._agent._active_chat_plan_pending = False
            self.save_chat_state()
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
        snapshot = self._latest_plan_snapshot_from_messages(
            list(getattr(self._agent, "conversation_history", None) or [])
        )
        self._agent._active_chat_plan = snapshot
        self._agent._active_chat_plan_pending = False

    def active_chat_plan_mode(self) -> bool:
        """Return whether the active chat is recorded as being in Plan mode."""
        with self._agent._chat_state_lock:
            chat = self.find_chat_by_id(self._agent.active_chat_id)
            if not chat:
                return False
            return bool(chat.get("plan_mode", False))

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
            if bool(chat.get("plan_mode", False)) == bool(enabled):
                return True
            chat["plan_mode"] = bool(enabled)
            chat["updated_at"] = self._now_text()
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
            # that turn's plan snapshot (each message must keep the plan as
            # it stood at that message). When the latest assistant message is
            # already stamped, the new plan stays pending and attaches to the
            # next recorded assistant message via
            # ``attach_pending_plan_to_message``.
            if msg.get("plan"):
                return
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
            try:
                remember = getattr(self._agent, "_remember_active_chat_history_first_visible_index", None)
                if callable(remember):
                    remember(0 if print_history else len(list(self._agent.conversation_history or [])))
            except Exception:
                pass
            if persist:
                self.save_chat_state()
        try:
            refresh_usage = getattr(self._agent, "_refresh_status_context_usage_snapshot", None)
            if callable(refresh_usage):
                refresh_usage()
        except Exception:
            pass
        try:
            svc = getattr(self._agent, "session_memory_service", None)
            schedule_refresh = getattr(svc, "schedule_context_usage_refresh_async", None)
            if callable(schedule_refresh):
                schedule_refresh(context_hint="chat activated")
        except Exception:
            pass
        if clear_screen:
            os.system("cls" if os.name == "nt" else "clear")
        if print_history:
            self._agent._print_chat_history()
        if announce:
            return f"✅ Switched to Chat: [{self._agent.active_chat_name}]"
        return ""
