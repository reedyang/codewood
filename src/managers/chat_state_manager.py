import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


class ChatStateManager:
    """Encapsulates chat state persistence and active-chat switching logic."""

    def __init__(self, agent: Any, chat_state_file: str) -> None:
        self._agent = agent
        self._chat_state_file = chat_state_file

    def chat_state_path(self) -> Path:
        return self._agent.ai_workspace_dir / self._chat_state_file

    def new_chat_entry(self, chat_id: str, name: str = "New Chat") -> Dict[str, Any]:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
            "context_usage_percent": usage_pct,
            "context_input_tokens": usage_tokens,
            "context_window": usage_window,
        }

    def sanitize_persisted_chat_message(self, role: str, content: str) -> Optional[str]:
        r = str(role or "").strip().lower()
        c = str(content or "")
        if r != "user":
            return c
        marker = "\n\n【首轮回复硬性要求（必须遵守）】"
        if marker in c:
            c = c.split(marker, 1)[0]
        if c.startswith("【用户原始需求】\n"):
            return None
        c = c.strip()
        if not c:
            return None
        return c

    def compact_redundant_user_turns(self, messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        # Keep persisted user turns intact, even when adjacent user texts are identical.
        # Collapsing these turns may hide meaningful repetition after `/chat reload`.
        return list(messages or [])

    def default_chat_state(self) -> Dict[str, Any]:
        default_chat = self.new_chat_entry("chat-1")
        return {"version": 1, "active": "chat-1", "chats": [default_chat]}

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
        raw: Dict[str, Any] = {}
        p = self.chat_state_path()
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    raw = loaded
            except Exception as e:
                print(f"⚠️ 读取 chat 状态失败，使用默认会话: {e}")
        chats_raw = raw.get("chats", [])
        chats: List[Dict[str, Any]] = []
        if isinstance(chats_raw, list):
            for c in chats_raw:
                if not isinstance(c, dict):
                    continue
                cid = str(c.get("id") or "").strip()
                if not cid:
                    cid = self.next_chat_id()
                name = str(c.get("name") or "New Chat").strip() or "New Chat"
                source = str(c.get("name_source") or "default").strip().lower()
                if source not in ("default", "auto", "manual"):
                    source = "default"
                messages = c.get("messages", [])
                if not isinstance(messages, list):
                    messages = []
                msgs = []
                for m in messages:
                    if not isinstance(m, dict):
                        continue
                    role = str(m.get("role") or "").strip().lower()
                    content = str(m.get("content") or "")
                    if role in ("user", "assistant"):
                        clean = self.sanitize_persisted_chat_message(role, content)
                        if clean is None:
                            continue
                        msgs.append({"role": role, "content": clean})
                chats.append(
                    {
                        "id": cid,
                        "name": name,
                        "name_source": source,
                        "created_at": str(c.get("created_at") or ""),
                        "updated_at": str(c.get("updated_at") or ""),
                        "model_provider": str(c.get("model_provider") or "").strip(),
                        "model_name": str(c.get("model_name") or "").strip(),
                        "messages": self.compact_redundant_user_turns(msgs),
                        "context_usage_percent": int(c.get("context_usage_percent") or 0),
                        "context_input_tokens": int(c.get("context_input_tokens") or 0),
                        "context_window": int(c.get("context_window") or 0),
                    }
                )
        if not chats:
            self._agent._chat_state = self.default_chat_state()
            chats = self.chat_entries()
        active = str(raw.get("active") or self._agent._chat_state.get("active") or "").strip()
        if not active or not any(str(c.get("id")) == active for c in chats):
            active = str(chats[0].get("id") or "chat-1")
        self._agent._chat_state = {"version": 1, "active": active, "chats": chats}
        self.save_chat_state()
        self.activate_chat(active, announce=False, clear_screen=False, print_history=False)

    def sync_active_chat_messages(self) -> None:
        with self._agent._chat_state_lock:
            chat = self.find_chat_by_id(self._agent.active_chat_id)
            if not chat:
                return
            chat["messages"] = list(self._agent.conversation_history)
            chat["context_usage_percent"] = int(
                getattr(self._agent, "_last_context_usage_percent", 0) or 0
            )
            chat["context_input_tokens"] = int(
                getattr(self._agent, "_last_context_input_tokens", 0) or 0
            )
            chat["context_window"] = int(
                getattr(self._agent, "_last_context_window", 0) or 0
            )
            chat["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.save_chat_state()

    def activate_chat(
        self,
        chat_id: str,
        announce: bool = True,
        clear_screen: bool = False,
        print_history: bool = False,
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
            try:
                self._agent._apply_chat_model_from_entry(chat, persist_if_missing=True)
            except Exception:
                pass
            self._apply_chat_usage_snapshot(chat)
            self.save_chat_state()
        if clear_screen:
            os.system("cls" if os.name == "nt" else "clear")
        if print_history:
            self._agent._print_chat_history()
        if announce:
            return f"✅ 已切换到 Chat: [{self._agent.active_chat_name}]"
        return ""
