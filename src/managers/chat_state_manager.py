import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

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

    def chat_state_path(self) -> Path:
        return self.chat_records_dir() / self._chat_state_file

    def chat_records_dir(self) -> Path:
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

    def new_chat_entry(self, chat_id: str, name: str = "New Chat") -> Dict[str, Any]:
        now = self._now_text()
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
            "messages": [],
            "plan": [],
            "plan_explanation": "",
            "plan_updated_at": "",
            "context_usage_percent": usage_pct,
            "context_input_tokens": usage_tokens,
            "context_window": usage_window,
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

        plan_items = _normalize_plan_items(raw.get("plan"))
        plan_explanation = str(raw.get("plan_explanation") or "").strip()
        plan_updated_at = str(raw.get("plan_updated_at") or "").strip()

        return {
            "id": cid,
            "name": name,
            "name_source": source,
            "created_at": str(raw.get("created_at") or "").strip() or self._now_text(),
            "updated_at": str(raw.get("updated_at") or "").strip() or self._now_text(),
            "model_provider": str(raw.get("model_provider") or "").strip(),
            "model_name": str(raw.get("model_name") or "").strip(),
            "messages": messages,
            "plan": plan_items,
            "plan_explanation": plan_explanation,
            "plan_updated_at": plan_updated_at,
            "context_usage_percent": int(raw.get("context_usage_percent") or 0),
            "context_input_tokens": int(raw.get("context_input_tokens") or 0),
            "context_window": int(raw.get("context_window") or 0),
        }

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
        try:
            index_path = self.chat_state_path()
            records_dir = self.chat_records_dir()
            index_path.parent.mkdir(parents=True, exist_ok=True)
            records_dir.mkdir(parents=True, exist_ok=True)

            state = self._agent._chat_state if isinstance(self._agent._chat_state, dict) else {}
            chats = state.get("chats", [])
            if not isinstance(chats, list):
                chats = []

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

            active = str(state.get("active") or "").strip()
            index_payload = {
                "version": CHAT_STATE_VERSION,
                "active": active,
                "chats": index_chats,
            }
            with open(index_path, "w", encoding="utf-8") as f:
                json.dump(index_payload, f, ensure_ascii=False, indent=2)

            for stale in records_dir.glob("*.json"):
                try:
                    if stale.resolve() == index_path.resolve():
                        continue
                    if stale.resolve() not in current_record_paths:
                        stale.unlink()
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
        chats = self._agent._chat_state.get("chats", [])
        if not isinstance(chats, list):
            chats = []
            self._agent._chat_state["chats"] = chats
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

    def sync_active_chat_messages(self) -> None:
        with self._agent._chat_state_lock:
            chat = self.find_chat_by_id(self._agent.active_chat_id)
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
        with self._agent._chat_state_lock:
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
            chat["plan"] = []
            chat["plan_explanation"] = ""
            chat["plan_updated_at"] = ""
            chat["context_usage_percent"] = 0
            chat["context_input_tokens"] = 0
            chat["context_window"] = int(
                getattr(self._agent, "_last_context_window", 0) or 0
            )
            chat["updated_at"] = self._now_text()
            self.save_chat_state()
            return True

    def active_chat_plan(self) -> Optional[Dict[str, Any]]:
        """Return a copy of the active chat's plan record, or None if no active chat."""
        with self._agent._chat_state_lock:
            chat = self.find_chat_by_id(self._agent.active_chat_id)
            if not chat:
                return None
            return {
                "plan": [dict(item) for item in (chat.get("plan") or []) if isinstance(item, dict)],
                "explanation": str(chat.get("plan_explanation") or ""),
                "updated_at": str(chat.get("plan_updated_at") or ""),
            }

    def persist_active_chat_plan(
        self,
        plan_items: List[Dict[str, str]],
        explanation: str = "",
    ) -> bool:
        """Replace the active chat's plan with the given items and persist.

        Returns True if a chat was found and updated. The caller is expected to
        provide already-validated items (e.g. via UpdatePlanTool); we still
        re-normalize defensively before saving.
        """
        normalized = _normalize_plan_items(plan_items)
        with self._agent._chat_state_lock:
            chat = self.find_chat_by_id(self._agent.active_chat_id)
            if not chat:
                return False
            chat["plan"] = normalized
            chat["plan_explanation"] = str(explanation or "").strip()
            chat["plan_updated_at"] = self._now_text()
            chat["updated_at"] = self._now_text()
            self.save_chat_state()
            return True

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
            self._agent._chat_state["active"] = chat_id
            self._agent.active_chat_id = chat_id
            self._agent.active_chat_name = str(chat.get("name") or "New Chat")
            self._agent.conversation_history = list(chat.get("messages") or [])
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
