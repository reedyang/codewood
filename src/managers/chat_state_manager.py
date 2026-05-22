import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


CHAT_STATE_VERSION = 2
TASK_STATUS_OPEN = "open"
TASK_STATUS_DONE = "done"
TASK_STATUS_CANCELLED = "cancelled"
TASK_STATUS_SWITCHED = "switched"
TASK_STATUSES = {
    TASK_STATUS_OPEN,
    TASK_STATUS_DONE,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_SWITCHED,
}


class ChatStateManager:
    """Encapsulates chat state persistence and active-chat switching logic."""

    def __init__(self, agent: Any, chat_state_file: str) -> None:
        self._agent = agent
        self._chat_state_file = chat_state_file

    @staticmethod
    def _now_text() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def chat_state_path(self) -> Path:
        return self._agent.ai_workspace_dir / self._chat_state_file

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
            "tasks": [],
            "active_task_id": "",
            "context_usage_percent": usage_pct,
            "context_input_tokens": usage_tokens,
            "context_window": usage_window,
        }

    def new_task_entry(
        self,
        task_id: str,
        root_user_input: str,
        domains: List[str],
        classifier: Optional[Dict[str, Any]] = None,
        switched_from_task_id: str = "",
    ) -> Dict[str, Any]:
        now = self._now_text()
        dvals = [str(x).strip() for x in (domains or []) if str(x).strip()]
        unique_domains: List[str] = []
        for d in dvals:
            if d not in unique_domains:
                unique_domains.append(d)
        if not unique_domains:
            unique_domains = ["general_other"]
        out: Dict[str, Any] = {
            "id": task_id,
            "status": TASK_STATUS_OPEN,
            "root_user_input": str(root_user_input or "").strip(),
            "domains": unique_domains,
            "domain_scores": {},
            "classifier": classifier if isinstance(classifier, dict) else {},
            "created_at": now,
            "updated_at": now,
            "closed_at": "",
            "switched_from_task_id": str(switched_from_task_id or "").strip(),
        }
        if isinstance(classifier, dict):
            primary = str(classifier.get("primary_domain") or "").strip()
            secondary_raw = classifier.get("secondary_domains")
            secondary = secondary_raw if isinstance(secondary_raw, list) else []
            if primary:
                out["domain_scores"][primary] = float(classifier.get("confidence") or 0.0)
            for dom in secondary:
                d = str(dom).strip()
                if d and d not in out["domain_scores"]:
                    out["domain_scores"][d] = float(classifier.get("confidence") or 0.0)
        return out

    def _normalize_message(
        self,
        raw: Dict[str, Any],
        fallback_task_id: str,
    ) -> Dict[str, Any]:
        role = str(raw.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            raise ValueError("invalid role")
        content = str(raw.get("content") or "")
        task_id = str(raw.get("task_id") or "").strip() or fallback_task_id
        created_at = str(raw.get("created_at") or "").strip() or self._now_text()
        return {
            "role": role,
            "content": content,
            "task_id": task_id,
            "created_at": created_at,
        }

    def _validate_task(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        tid = str(raw.get("id") or "").strip()
        if not tid:
            raise ValueError("task id required")
        status = str(raw.get("status") or "").strip().lower()
        if status not in TASK_STATUSES:
            raise ValueError("invalid task status")
        root_user_input = str(raw.get("root_user_input") or "").strip()
        domains_raw = raw.get("domains")
        if not isinstance(domains_raw, list):
            raise ValueError("domains must be list")
        domains = []
        for d in domains_raw:
            s = str(d).strip()
            if s and s not in domains:
                domains.append(s)
        if not domains:
            domains = ["general_other"]
        classifier = raw.get("classifier")
        if not isinstance(classifier, dict):
            classifier = {}
        domain_scores = raw.get("domain_scores")
        if not isinstance(domain_scores, dict):
            domain_scores = {}
        normalized_scores: Dict[str, float] = {}
        for k, v in domain_scores.items():
            key = str(k).strip()
            if not key:
                continue
            try:
                normalized_scores[key] = float(v)
            except Exception:
                normalized_scores[key] = 0.0
        return {
            "id": tid,
            "status": status,
            "root_user_input": root_user_input,
            "domains": domains,
            "domain_scores": normalized_scores,
            "classifier": classifier,
            "created_at": str(raw.get("created_at") or "").strip() or self._now_text(),
            "updated_at": str(raw.get("updated_at") or "").strip() or self._now_text(),
            "closed_at": str(raw.get("closed_at") or "").strip(),
            "switched_from_task_id": str(raw.get("switched_from_task_id") or "").strip(),
        }

    def _validate_chat_entry(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(raw.get("id") or "").strip()
        if not cid:
            raise ValueError("chat id required")
        name = str(raw.get("name") or "").strip() or "New Chat"
        source = str(raw.get("name_source") or "default").strip().lower()
        if source not in ("default", "auto", "manual"):
            source = "default"

        tasks_raw = raw.get("tasks")
        if not isinstance(tasks_raw, list):
            raise ValueError("tasks must be list")
        tasks = [self._validate_task(t) for t in tasks_raw if isinstance(t, dict)]
        task_ids = {str(t.get("id") or "") for t in tasks}

        active_task_id = str(raw.get("active_task_id") or "").strip()
        if active_task_id and active_task_id not in task_ids:
            raise ValueError("active_task_id not found in tasks")

        messages_raw = raw.get("messages")
        if not isinstance(messages_raw, list):
            raise ValueError("messages must be list")
        fallback_task_id = active_task_id or (tasks[0]["id"] if tasks else "")
        messages = []
        for item in messages_raw:
            if not isinstance(item, dict):
                raise ValueError("message item must be object")
            msg = self._normalize_message(item, fallback_task_id=fallback_task_id)
            if task_ids and msg["task_id"] not in task_ids:
                raise ValueError("message task_id missing in tasks")
            messages.append(msg)

        return {
            "id": cid,
            "name": name,
            "name_source": source,
            "created_at": str(raw.get("created_at") or "").strip() or self._now_text(),
            "updated_at": str(raw.get("updated_at") or "").strip() or self._now_text(),
            "model_provider": str(raw.get("model_provider") or "").strip(),
            "model_name": str(raw.get("model_name") or "").strip(),
            "messages": messages,
            "tasks": tasks,
            "active_task_id": active_task_id,
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
            p = self.chat_state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(self._agent._chat_state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 保存 chat 状态失败: {e}")

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

    @staticmethod
    def _task_entries(chat: Dict[str, Any]) -> List[Dict[str, Any]]:
        items = chat.get("tasks", [])
        if not isinstance(items, list):
            items = []
            chat["tasks"] = items
        return items

    @staticmethod
    def _find_task_by_id(chat: Dict[str, Any], task_id: str) -> Optional[Dict[str, Any]]:
        for t in ChatStateManager._task_entries(chat):
            if not isinstance(t, dict):
                continue
            if str(t.get("id") or "") == task_id:
                return t
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

    @staticmethod
    def next_task_id(chat: Dict[str, Any]) -> str:
        existing = {
            str(t.get("id") or "")
            for t in ChatStateManager._task_entries(chat)
            if isinstance(t, dict)
        }
        i = 1
        while True:
            tid = f"task-{i}"
            if tid not in existing:
                return tid
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
            chats = [self._validate_chat_entry(c) for c in chats_raw if isinstance(c, dict)]
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
                f"⚠️ 读取 chat 状态失败，已清空并重置默认会话: {e}"
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
            fallback_task_id = (
                str(getattr(self._agent, "_active_runtime_task_id", "") or "").strip()
                or str(chat.get("active_task_id") or "").strip()
            )
            msgs = []
            for m in list(self._agent.conversation_history):
                if not isinstance(m, dict):
                    continue
                role = str(m.get("role") or "").strip().lower()
                if role not in ("user", "assistant"):
                    continue
                entry = {
                    "role": role,
                    "content": str(m.get("content") or ""),
                    "task_id": str(m.get("task_id") or "").strip() or fallback_task_id,
                    "created_at": str(m.get("created_at") or "").strip() or self._now_text(),
                }
                msgs.append(entry)
            chat["messages"] = msgs
            chat["active_task_id"] = fallback_task_id
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

    def start_task(
        self,
        chat_id: str,
        root_user_input: str,
        domains: List[str],
        classifier: Optional[Dict[str, Any]] = None,
        switched_from_task_id: str = "",
    ) -> Optional[str]:
        with self._agent._chat_state_lock:
            chat = self.find_chat_by_id(chat_id)
            if not chat:
                return None
            task_id = self.next_task_id(chat)
            entry = self.new_task_entry(
                task_id=task_id,
                root_user_input=root_user_input,
                domains=domains,
                classifier=classifier,
                switched_from_task_id=switched_from_task_id,
            )
            self._task_entries(chat).append(entry)
            chat["active_task_id"] = task_id
            chat["updated_at"] = self._now_text()
            self.save_chat_state()
            return task_id

    def close_task(
        self,
        chat_id: str,
        task_id: str,
        status: str,
    ) -> bool:
        want = str(status or "").strip().lower()
        if want not in TASK_STATUSES:
            return False
        with self._agent._chat_state_lock:
            chat = self.find_chat_by_id(chat_id)
            if not chat:
                return False
            task = self._find_task_by_id(chat, task_id)
            if not task:
                return False
            task["status"] = want
            task["updated_at"] = self._now_text()
            if want != TASK_STATUS_OPEN:
                task["closed_at"] = self._now_text()
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
            chat = self.find_chat_by_id(chat_id)
            if not chat:
                return f"❌ 未找到 chat: {chat_id}"
            self._agent._chat_state["active"] = chat_id
            self._agent.active_chat_id = chat_id
            self._agent.active_chat_name = str(chat.get("name") or "New Chat")
            self._agent.conversation_history = list(chat.get("messages") or [])
            self._agent.operation_results = []
            self._agent._session_summary_llm = ""
            self._agent._session_summary_rolling = ""
            self._agent._last_llm_summary_pair_count = 0
            self._agent._active_runtime_task_id = str(chat.get("active_task_id") or "").strip()
            current_domains: List[str] = []
            if self._agent._active_runtime_task_id:
                task = self._find_task_by_id(chat, self._agent._active_runtime_task_id)
                if isinstance(task, dict):
                    vals = task.get("domains")
                    if isinstance(vals, list):
                        current_domains = [str(x).strip() for x in vals if str(x).strip()]
            self._agent._active_runtime_task_domains = current_domains
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
        if clear_screen:
            os.system("cls" if os.name == "nt" else "clear")
        if print_history:
            self._agent._print_chat_history()
        if announce:
            return f"✅ 已切换到 Chat: [{self._agent.active_chat_name}]"
        return ""
